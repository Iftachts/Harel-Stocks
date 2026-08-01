from __future__ import annotations

import shutil
from pathlib import Path

from harel.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def edited_config(tmp_path, *edits: tuple[str, str], file: str = "scoring.yaml"):
    """The shipped config with substitutions applied - the trader's own edit."""
    cdir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", cdir)
    text = (cdir / file).read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"{file} no longer contains {old!r} - fix this test"
        text = text.replace(old, new)
    (cdir / file).write_text(text, encoding="utf-8")
    return load_config(cdir)


def test_universe_loads_and_is_complete(config):
    assert len(config.active_tickers) == 22
    assert "TEVA" in config.active_tickers
    assert "PANW" in config.active_tickers


def test_the_shipped_universe_has_no_unresolved_tickers(config):
    """A symbol that resolves to nothing collects nothing. Ship none."""
    assert config.unresolved_tickers == []


def test_unresolved_tickers_are_excluded_from_collection(config_with_unresolved):
    cfg = config_with_unresolved
    assert "ZZTEST" in cfg.unresolved_tickers
    assert "ZZTEST" not in cfg.active_tickers
    assert cfg.universe["ZZTEST"].resolution_hint


def test_every_active_ticker_has_a_known_sector(config):
    for ticker in config.active_tickers:
        tc = config.ticker(ticker)
        assert tc.sector in config.sectors, f"{ticker} points at unknown sector {tc.sector}"
        assert tc.sector != "unknown"


def test_every_active_ticker_has_peers_and_themes(config):
    """Indirect coverage is only as good as the peer/theme graph."""
    for ticker in config.active_tickers:
        tc = config.ticker(ticker)
        assert tc.peer_names, f"{ticker} has no peer_names - no read-across possible"
        assert tc.themes, f"{ticker} has no themes"


def test_every_active_ticker_can_do_cross_read(config):
    """The RUNBOOK is explicit: peer_names, competitor_products and themes are
    what decide indirect coverage - "בלעדיהם השם ייאסף אבל לא תקבל עליו קריאה
    צולבת". peer_names and themes were already enforced; competitor_products was
    not, and 17 of 22 names shipped without it, which made PRODUCT_RIVAL
    read-across impossible for them by construction.

    What has to hold is that a cross-read QUERY exists for every name - not that
    it comes from one particular field. Demanding competitor_products from every
    name is how KEN, a merchant power holding company with no product rivals,
    ended up listing its peer companies as "products" and scoring an NRG
    earnings preview as a Kenon product-rival event.
    """
    from harel.collect.rss import _cross_read_terms

    missing = []
    for ticker in config.active_tickers:
        tc = config.ticker(ticker)
        relation = "PRODUCT_RIVAL" if tc.competitor_products else "PEER"
        if not _cross_read_terms(tc, relation):
            missing.append(ticker)
    assert missing == [], f"no cross-read query possible for {missing}"


def test_peer_companies_are_not_declared_as_products(config):
    """`competitor_products` means a rival PRODUCT - a molecule, a design socket,
    a named platform - because it is tagged PRODUCT_RIVAL at 0.85, close to
    DIRECT. Bare peer company names in that field are the same claim as PEER at
    twice the weight, and a peer's routine quarterly then outranks real news."""
    for ticker in config.active_tickers:
        tc = config.ticker(ticker)
        if not tc.competitor_products:
            continue
        peers = {p.lower() for p in tc.peer_names}
        bare = [p for p in tc.competitor_products if p.lower() in peers]
        # A device brand that is also the company name (NeuroStar, Magstim) is a
        # legitimate product term, so allow a minority overlap and catch the
        # case where the field is simply a copy of peer_names.
        assert len(bare) < len(tc.competitor_products) * 0.6, (
            f"{ticker}: competitor_products is mostly peer company names {bare}"
        )


def test_holdings_filler_is_noise_but_real_news_is_not(config):
    """Automated "Fund X Buys N Shares of Y" pieces are generated in bulk from
    13F filings up to 45 days stale, and they were ranking third in the feed,
    above Teva's own earnings release. The patterns have to catch them without
    catching "Raises Outlook", which is a real guidance change."""
    noise = [
        "Arrowstreet Capital Limited Partnership Buys 44,869 Shares of Kenon Holdings",
        "Arkadios Wealth Advisors Raises Stock Position in Palo Alto Networks",
        "Sei Investments Co. Has $26.63 Million Stock Position in Nova Ltd.",
        "[FR] Self-Regulatory Organizations; The Nasdaq Stock Market LLC",
    ]
    real = [
        "Teva Delivers Strong Q2 Results and Raises Outlook for All Three Key Brands",
        "[4] LIVEPERSON INC - open-market SELL - CFO and COO",
        "Fortinet introduces the FortiGate 1200G with FortiSASE Outpost",
        "Teva (TEVA) Lifts UZEDY Outlook After Record Sales",
    ]
    def caps(title):
        return [n.cap for n in config.scoring.noise_title_patterns if n.pattern.search(title)]

    for title in noise:
        assert caps(title), f"should be capped as noise: {title!r}"
        assert min(caps(title)) <= 15
    for title in real:
        assert not caps(title), f"real news wrongly capped: {title!r}"


def test_no_product_term_is_a_generic_english_word(config):
    """Product terms become DIRECT match rules - the relation the MCP server
    tells the agent to treat as fact about the issuer. A term like "POWER"
    matches the word in any document body and mislabels unrelated filings as
    the company's own news, so terms must be as distinctive as a brand name.
    """
    generic = {
        "power", "optical", "memory", "vision", "signal", "sensor", "digital",
        "mobile", "secure", "access", "network", "energy", "system", "systems",
        "cloud", "platform", "storage", "control", "wireless", "battery",
        # Real trial-programme names that are also everyday words. Bare
        # "SKYSCRAPER" (Roche TIGIT) matched building stories; "GALAXIES" (GSK)
        # matched astronomy. Programme names must carry their number.
        "skyscraper", "galaxies", "horizon", "beacon", "compass", "summit",
    }
    for ticker in config.active_tickers:
        tc = config.ticker(ticker)
        for term in tc.product_terms:
            if len(term) < 5:
                continue  # too short to become a rule anyway
            assert term.strip().lower() not in generic, (
                f"{ticker} product term {term!r} is a generic word; it would tag "
                f"any document containing it as {ticker}'s own news"
            )
        # Rival products become PRODUCT_RIVAL rules on the same machinery, so a
        # generic word there mislabels unrelated news as a competitor's move.
        for term in tc.competitor_products:
            if len(term) < 5:
                continue
            assert term.strip().lower() not in generic, (
                f"{ticker} competitor product {term!r} is a generic word"
            )


def test_every_active_ticker_has_a_tase_issuer_id(config):
    """The MAYA v2 API keys on issuer number, not the security id in tase_id.
    A name without one is silently absent from the Israeli disclosure feed."""
    missing = [
        t for t in config.active_tickers
        if not config.ticker(t).raw.get("tase_issuer_id")
    ]
    assert missing == [], f"no tase_issuer_id for {missing}"


def test_scoring_regexes_all_compile(config):
    assert len(config.scoring.events) >= 20
    for rule in config.scoring.events:
        assert rule.patterns or rule.form_types
        assert 0 < rule.base <= 100


def test_event_rules_are_sorted_by_base_descending(config):
    bases = [r.base for r in config.scoring.events]
    assert bases == sorted(bases, reverse=True)


def test_relations_cover_every_relation_used_by_the_linker(config):
    from harel.enrich.linker import RELATION_RANK

    for relation in RELATION_RANK:
        assert relation in config.scoring.relations, f"{relation} missing from scoring.yaml"


def test_cik_padding(config):
    assert config.ticker("TEVA").cik10 == "0000818686"


def test_maya_runs_without_a_key_but_reports_itself_degraded(config, monkeypatch):
    monkeypatch.delenv("TASE_API_KEY", raising=False)
    source = config.sources["maya_tase"]
    assert source.available, "MAYA must still run on the public endpoints"
    assert source.degraded, "…but must declare that it is on an unofficial fallback"


def test_sector_regulators_reference_real_sources(config):
    known = set(config.sources)
    for key, sector in config.sectors.items():
        for regulator in sector.regulators:
            assert regulator in known, f"sector {key} references unknown source {regulator}"


def test_no_event_rule_claims_a_generic_form_as_standalone_evidence(config):
    """Regression guard: listing 8-K/6-K under an event's form_types once made
    every routine filing score as a listing-compliance event."""
    from harel.enrich.events import FORM_STANDALONE_RULES, GENERIC_FORMS

    for rule in config.scoring.events:
        if rule.key not in FORM_STANDALONE_RULES:
            continue
        overlap = set(rule.form_types) & GENERIC_FORMS
        assert not overlap, f"{rule.key} would fire on any {overlap}"


def test_noise_form_caps_do_not_suppress_genuinely_material_forms(config):
    caps = config.scoring.noise_form_types
    for form in ("8-K", "6-K", "424B5", "SC 13D"):
        assert form not in caps or caps[form] >= 35, f"{form} must not be capped as noise"


def test_a_noise_pattern_without_a_cap_takes_the_configured_default(tmp_path):
    """`noise.hard_cap_default` was declared in scoring.yaml and then hardcoded
    a second time as a literal 12 in the loader, so editing it moved nothing."""
    cfg = edited_config(
        tmp_path,
        ("  hard_cap_default: 12\n", "  hard_cap_default: 4\n"),
        ('    - pattern: "\\\\bSelf-Regulatory Organizations\\\\b"\n      cap: 10\n',
         '    - pattern: "\\\\bSelf-Regulatory Organizations\\\\b"\n'),
    )
    assert cfg.scoring.noise_hard_cap_default == 4
    capless = [n.cap for n in cfg.scoring.noise_title_patterns
               if n.pattern.pattern == "\\bSelf-Regulatory Organizations\\b"]
    assert capless == [4]


def test_scoring_overrides_are_keyed_by_uppercase_ticker(tmp_path):
    """Every ticker in this system is uppercase, and a lowercase YAML key used
    to apply half of its own block - keyword boosts under the uppercased key,
    the relation override under a key nothing ever looked up."""
    cfg = edited_config(tmp_path, ("\n  CGEN:\n", "\n  cgen:\n"))
    assert "CGEN" in cfg.scoring.overrides
    assert "cgen" not in cfg.scoring.overrides
    assert cfg.scoring.overrides["CGEN"]["relation_overrides"]["PRODUCT_RIVAL"] == 0.95


def test_every_real_sector_declares_the_read_across_it_is_scored_with(config):
    """`read_across` and `peer_read_across` are the relation multiplier for a
    sector's SECTOR_* and PEER links. A sector that omits one does not score
    zero - it silently falls back to the global default in scoring.yaml, which
    is the tuning quietly not happening."""
    for key, sector in config.sectors.items():
        if key == "unknown":
            continue
        assert 0 < sector.read_across <= 1, f"{key}: read_across={sector.read_across}"
        assert 0 < sector.peer_read_across <= 1, (
            f"{key}: peer_read_across={sector.peer_read_across}")
