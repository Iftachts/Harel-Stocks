from __future__ import annotations


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
