from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harel.enrich.linker import EntityLinker
from harel.models import RawItem


def item(title: str, summary: str = "", source: str = "google_news",
         seed=None, seed_relation="DIRECT", meta=None) -> RawItem:
    return RawItem(
        source=source, source_kind="rss", external_id=title[:40], title=title,
        url="https://example.com/x", summary=summary,
        published_at=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        seed_tickers=seed or [], seed_relation=seed_relation, meta=meta or {},
    )


@pytest.fixture(scope="module")
def linker(request):
    from harel.config import load_config
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return EntityLinker(load_config(root / "config"))


def rel(links, ticker):
    for link in links:
        if link.ticker == ticker:
            return link.relation
    return None


def test_company_name_in_headline_is_direct(linker):
    links = linker.link(item("Teva Pharmaceutical Industries raises 2026 guidance"))
    assert rel(links, "TEVA") == "DIRECT"


def test_competitor_name_links_as_peer_not_direct(linker):
    """The whole point of the relation model: Viatris news is not Teva news."""
    links = linker.link(item("Viatris cuts full-year guidance on generics pricing"))
    assert rel(links, "TEVA") == "PEER"
    assert rel(links, "VTRS") is None  # peers are not in our universe


def test_rival_product_links_as_product_rival(linker):
    links = linker.link(
        item("Roche reports tiragolumab Phase 3 failure in SKYSCRAPER-01")
    )
    assert rel(links, "CGEN") == "PRODUCT_RIVAL"
    # The same headline reached KMDA at DIRECT 0.82 - ALERT, 75.2 - because
    # "Phase 3" was in its INHALED_AAT product list. Roche's TIGIT readout is
    # not Kamada's own news at any confidence.
    assert rel(links, "KMDA") != "DIRECT"


def test_our_own_product_links_direct_even_without_the_company_name(linker):
    links = linker.link(item("AUSTEDO XR prescriptions accelerate in the second quarter"))
    assert rel(links, "TEVA") == "DIRECT"


def test_ambiguous_ticker_is_not_matched_bare(linker):
    """'ICL' and 'ORA' are ordinary strings; matching them bare would flood the feed."""
    links = linker.link(item("The ICL protocol in ORA studies uses a KEN framework"))
    assert rel(links, "ICL") is None
    assert rel(links, "ORA") is None
    assert rel(links, "KEN") is None


def test_ambiguous_ticker_matches_with_exchange_prefix(linker):
    links = linker.link(item("NYSE: ICL announces potash contract with China"))
    assert rel(links, "ICL") == "DIRECT"


def test_customer_capex_links_as_customer(linker):
    links = linker.link(item("TSMC raises 2026 capex guidance on CoWoS demand"))
    assert rel(links, "CAMT") in ("CUSTOMER", "PEER", "SECTOR_THEME")
    assert rel(links, "CAMT") is not None


def test_collector_seed_wins_when_stronger(linker):
    links = linker.link(
        item("Some filing", source="sec_edgar_submissions", seed=["NVMI"],
             seed_relation="DIRECT")
    )
    assert rel(links, "NVMI") == "DIRECT"


def test_headline_match_scores_higher_confidence_than_body(linker):
    in_title = linker.link(item("Camtek wins large advanced packaging order"))
    in_body = linker.link(item("Chip inspection orders rise", summary="Camtek was named."))
    c_title = next(l for l in in_title if l.ticker == "CAMT")
    c_body = next(l for l in in_body if l.ticker == "CAMT")
    assert c_title.confidence > c_body.confidence


def test_regulator_document_links_whole_sector(linker):
    links = linker.link(
        item(
            "Implementation of Additional Export Controls on semiconductor "
            "export controls and entity list additions",
            source="federal_register",
            summary="BIS amends the EAR.",
        )
    )
    semis = {l.ticker for l in links if l.relation in ("SECTOR_REG", "SECTOR_THEME")}
    assert {"TSEM", "NVMI", "CAMT"} & semis


def test_unresolved_ticker_never_gets_linked(config_with_unresolved):
    unresolved_linker = EntityLinker(config_with_unresolved)
    links = unresolved_linker.link(item("ZZTEST reports record quarter"))
    assert rel(links, "ZZTEST") is None


def test_link_why_is_populated_for_every_link(linker):
    links = linker.link(item("Elbit Systems wins $200 million contract in Europe"))
    assert links
    for link in links:
        assert link.why, "every link must be explainable to the agent"


def test_synthetic_tape_alert_does_not_read_across_to_peers(linker):
    """'NVMI up 10%' is about NVMI. Camtek listing NVMI as a peer must not make
    it Camtek news."""
    links = linker.link(item(
        "[TAPE] NVMI up 10.0% with no matching news",
        source="prices_stooq", seed=["NVMI"], meta={"synthetic": True},
    ))
    assert [l.ticker for l in links] == ["NVMI"]


def test_panw_peer_readacross(linker):
    """Security platforms trade as a cohort: a peer's billings miss de-rates the
    group, so it must reach PANW as PEER - and must not read as PANW's own news."""
    links = linker.link(item("CrowdStrike cuts full-year net-new ARR guidance"))
    assert rel(links, "PANW") == "PEER"


def test_panw_direct_on_its_own_metrics(linker):
    links = linker.link(item("Palo Alto Networks reports NGS ARR growth and raises guidance"))
    assert rel(links, "PANW") == "DIRECT"


def test_check_point_links_to_both_israeli_security_names(linker):
    """CHKP is a named competitor of both PANW and ALLT."""
    links = linker.link(item("Check Point raises full-year revenue guidance"))
    assert rel(links, "PANW") == "PEER"
    assert rel(links, "ALLT") == "PEER"


def test_a_uk_gilt_is_not_gilat_satellite(config):
    """The drill-down page caught this: "REG - FTSE Russell - 0 1/8%
    Index-linked Treasury Gilt 2041" was tagged DIRECT for GILT and reached
    `harel brief GILT`, which does not filter by score. A gilt is a bond."""
    from harel.collect.rss import _is_wordlike
    from harel.enrich.linker import AMBIGUOUS_TICKERS

    assert _is_wordlike("GILT"), 'the news query must not ask for "GILT" stock'
    assert "GILT" in AMBIGUOUS_TICKERS

    bond = item("REG - FTSE Russell - 0 1/8% Index-linked Treasury Gilt 2041")
    links = EntityLinker(config).link(bond)
    assert "GILT" not in {ln.ticker for ln in links}, links


# --------------------------------------------------------------------------- #
# What a `products:` entry has to be before it can say "this story is about us"
# --------------------------------------------------------------------------- #

def test_a_generic_clinical_phrase_is_not_a_company_product(linker):
    """"Phase 3" sat in Kamada's INHALED_AAT list, so every Phase 3 readout on
    earth was Kamada's own news: this one scored ALERT 75.2 - base 90 for a
    clinical readout, times DIRECT 1.00, times the micro-float 1.25."""
    from harel.enrich.linker import causal_eligible

    links = linker.link(
        item("Merck's Phase 3 KEYNOTE-671 trial met its primary endpoint")
    )
    assert rel(links, "KMDA") != "DIRECT"
    assert not causal_eligible(rel(links, "KMDA"))


def test_an_ordinary_english_word_is_not_a_company_product(linker):
    """"transplant" was CYTOGAM's watch term. CMV immune globulin is given to
    transplant patients; a pig-to-human heart transplant is not Kamada news."""
    links = linker.link(
        item("Surgeons perform first pig-to-human heart transplant in Europe")
    )
    assert rel(links, "KMDA") != "DIRECT"


def test_a_partner_company_name_is_not_a_company_product(linker):
    """"Sanofi" was listed under Teva's DUVAKITUG - it is the partner on the
    program, not the program. Corporate context cannot catch this one: the
    sentence is impeccable corporate context and still not a Teva story."""
    links = linker.link(item("Sanofi agrees to acquire Blueprint Medicines"))
    assert rel(links, "TEVA") != "DIRECT"


@pytest.mark.parametrize("headline", [
    "Phase 3 trial of an experimental antibody meets its primary endpoint",
    "Power management chips are in short supply across the industry",
    "Silicon photonics shipments double as optical speeds rise",
    "Rabies immune globulin remains in shortage in several states",
    "Landing gear overhaul turnaround times lengthen across the fleet",
    "Deep packet inspection is being written out of the 5G core",
    "Session border controller demand tracks UCaaS seat growth",
    "The next-generation firewall market grew 9% last year",
    "Geothermal power plant permitting speeds up on federal land",
    "A pig-to-human heart transplant is performed in Europe",
])
def test_no_product_term_turns_an_industry_story_into_a_companys_own_news(
        linker, headline):
    """The guard rail, not the instances. Every one of these sentences is the
    ordinary vocabulary of an industry we cover; none of them names an issuer.
    A `products:` entry that lets one of them mint DIRECT is the same defect as
    "Phase 3", and the TSEM POWER comment shows it survives being fixed one
    YAML key at a time."""
    direct = [l for l in linker.link(item(headline)) if l.relation == "DIRECT"]
    assert not direct, direct


@pytest.mark.parametrize("term,identifies", [
    # A development code or our own name in the term: nobody else's.
    ("TEV-48574", True), ("COM701", True), ("ORMD-0801", True),
    ("Camtek Eagle", True), ("Nova PRISM", True), ("Ormat Energy Converter", True),
    # A coined single word - real, but so is a partner's name, so it has to be
    # used as a product before it counts.
    ("AUSTEDO", False), ("deutetrabenazine", False), ("Sanofi", False),
    ("transplant", False),
    # Descriptions of an industry. Case in YAML is not evidence - every matcher
    # here is case-insensitive, which is how "POWER MANAGEMENT" matched the
    # words "power management" in any document body.
    ("Phase 3", False), ("POWER MANAGEMENT", False), ("silicon photonics", False),
    ("alpha-1 antitrypsin", False),
])
def test_only_a_code_or_our_own_name_identifies_us_without_further_evidence(
        config, term, identifies):
    from harel.enrich.linker import _has_code_token, _names_the_company

    own = [n for n in config.ticker("CAMT").match_names] + [
        "Nova", "Ormat", "Ormat Technologies"]
    assert (_has_code_token(term) or _names_the_company(term, own)) is identifies


# --------------------------------------------------------------------------- #
# A rival COMPANY is not a rival PRODUCT
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ticker,headline", [
    ("TATT", "AAR Corp. reports fourth quarter results"),
    ("TATT", "Collins Aerospace wins a widebody retrofit contract"),
    ("TSEM", "Vanguard International Semiconductor raises 2026 capex"),
    ("BWAY", "Neuronetics reports third quarter revenue"),
    ("BWAY", "MagVenture appoints a new chief executive"),
    ("ICL", "Belaruskali resumes potash exports through Russian ports"),
    ("ICL", "Compass Minerals cuts its full-year outlook"),
    ("GILT", "ST Engineering iDirect launches a defence modem line"),
    ("ALLT", "Nokia guides to lower mobile networks revenue"),
    ("NICE", "Amazon reports AWS revenue growth of 20%"),
])
def test_a_competitors_own_news_links_as_peer_not_as_a_rival_program(
        linker, ticker, headline):
    """PRODUCT_RIVAL outranks PEER, so a peer company sitting in
    competitor_products promoted a competitor's earnings to 'rival program' at
    0.78 - about 31% over what the same story earns as the PEER it is. The KEN
    block in universe.yaml records this being fixed by emptying one list, which
    is why the other eight names kept doing it."""
    assert rel(linker.link(item(headline)), ticker) == "PEER"


def test_no_competitor_product_in_the_universe_is_one_of_our_own_peers(config):
    from harel.enrich.linker import _norm

    offenders = {
        t: [c for c in config.ticker(t).competitor_products
            if _norm(c) in {_norm(p) for p in config.ticker(t).peer_names}]
        for t in config.active_tickers
    }
    assert not {t: c for t, c in offenders.items() if c}


# --------------------------------------------------------------------------- #
# A competitor is never a demand driver
# --------------------------------------------------------------------------- #

def test_a_peer_named_in_peer_events_does_not_become_a_customer(linker):
    """`peer_events_that_matter` mixes demand drivers with competitors whose
    results de-rate the group. The guard compared whole strings, but only the
    capitalised head is extracted, so "Microsoft" never equalled the peer names
    "Microsoft Defender" / "Microsoft Security" and PANW took a CUSTOMER link -
    a demand driver, above PEER in the precedence order - off its competitor."""
    assert rel(linker.link(item("Microsoft raises quarterly dividend")),
               "PANW") != "CUSTOMER"


def test_a_demand_driver_head_is_not_truncated_to_an_ambiguous_word(linker):
    """"Sierra AI funding" produced a bare `Sierra` rule. Sierra Leone is not a
    NICE demand driver, and CUSTOMER is eligible to be quoted as the cause of a
    move."""
    links = linker.link(
        item("Sierra Leone declares national emergency over flooding")
    )
    assert rel(links, "NICE") is None


def test_an_acronym_head_survives_truncation(linker):
    """The fix must not cost TSMC: an acronym is a name on its own, which is
    what separates it from "Sierra"."""
    from harel.enrich.linker import _entity_candidates

    assert _entity_candidates("TSMC CoWoS capacity expansion") == [
        "TSMC", "TSMC CoWoS"]
    assert _entity_candidates("Sierra AI funding") == ["Sierra AI"]
    assert _entity_candidates("Samsung capex") == ["Samsung"]


# --------------------------------------------------------------------------- #
# A field boundary is not a space
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ticker,title,summary", [
    ("NICE", "Analysts turn cautious on Nice", "Results from the survey are due"),
    ("NVMI", "Semiconductor selloff drags down Nova", "Q2 earnings season starts"),
])
def test_a_term_cannot_be_satisfied_across_the_title_summary_boundary(
        linker, ticker, title, summary):
    """`RawItem.text` is title, summary and body glued together, and `\\s+`
    matches a newline, so the corporate context an ordinary-word name needs was
    being met by a word in a different field - "Nice\\nResults", "Nova\\nQ2" -
    at DIRECT 0.90, the highest confidence the linker can assign. There is no
    sentence there. Google News RSS produces this shape all day."""
    assert rel(linker.link(item(title, summary)), ticker) is None


def test_a_multi_word_term_still_matches_across_a_wrapped_line(linker):
    """The other half of the requirement: a body wraps mid-phrase and the match
    has to survive it."""
    wrapped = item("Chip inspection orders rise")
    wrapped.body = "Onto Innovation\nDragonfly platform shipped in volume."
    assert rel(linker.link(wrapped), "CAMT") == "PRODUCT_RIVAL"


# --------------------------------------------------------------------------- #
# One pattern cache, two Configs
# --------------------------------------------------------------------------- #

def test_direct_evidence_does_not_serve_one_configs_pattern_to_another():
    """`get_config` is lru_cache(maxsize=4) and HAREL_CONFIG_DIR is
    environment-configurable, so one process can hold two Configs that both
    define a symbol. Keyed on the symbol alone, whichever compiled first
    answered for both - and both call sites DISCARD the item when this is
    False, so a stale pattern silently drops real company news."""
    from harel.config import TickerConfig
    from harel.enrich.linker import direct_evidence

    a = TickerConfig(ticker="ZZZ", name="Acme Widgets", sector="x")
    b = TickerConfig(ticker="ZZZ", name="Zenith Robotics", sector="x")

    assert direct_evidence(a, "Acme Widgets beats estimates") is True
    assert direct_evidence(b, "Zenith Robotics beats estimates") is True
    assert direct_evidence(b, "Acme Widgets beats estimates") is False


# --------------------------------------------------------------------------- #
# entity_hits: the same rules, for collectors holding a bare string
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    # openFDA gives a `recalling_firm` and a `product_description`, not an
    # article. These are the shapes a collector actually holds.
    ("Nova Ltd", "DIRECT"),
    ("Nova Measuring Instruments Ltd", "DIRECT"),
    # A separate matcher that rebuilt these rules by hand had no ordinary-word
    # guard, so 24 Nova Biomedical device recalls and 8 Nova Products drug
    # recalls were filed as Nova Ltd's own company news.
    ("Nova Biomedical Corporation Nova StatStrip Glucose Hospital Meter System", None),
    ("Nova Products, Inc. dietary supplement capsules", None),
])
def test_entity_hits_guards_an_ordinary_word_name_the_way_link_does(
        config, text, expected):
    from harel.enrich.linker import entity_hits

    found = {ticker: relation for ticker, relation, _ in entity_hits(config, text)}
    assert found.get("NVMI") == expected, entity_hits(config, text)


def test_entity_hits_needs_no_rawitem_and_explains_itself(config):
    from harel.enrich.linker import entity_hits

    hits = entity_hits(config, "Kamada Ltd KEDRAB rabies immune globulin (human)")
    assert [(t, r) for t, r, _ in hits] == [("KMDA", "DIRECT")]
    assert all(why for _, _, why in hits), "a collector has to be able to quote this"


def test_entity_hits_keeps_the_strongest_relation_per_ticker(config):
    """Same precedence as `link`: a rival's product is PRODUCT_RIVAL, and the
    company that ships it is not us."""
    from harel.enrich.linker import entity_hits

    hits = dict((t, r) for t, r, _ in
                entity_hits(config, "Grifols Therapeutics LLC HyperRAB 300 IU/mL"))
    assert hits.get("KMDA") == "PRODUCT_RIVAL"


def test_entity_hits_leaves_out_group_level_relations_by_default(config):
    """A regulator document is *made of* sector vocabulary - "biosimilar", "drug
    shortage" - so themes would match nearly every row. The default is evidence
    about a company; a caller that wants the group can ask."""
    from harel.enrich.linker import RELATION_RANK, entity_hits

    text = "Guidance for industry on biosimilar interchangeability and drug shortage reporting"
    assert not [t for t, r, _ in entity_hits(config, text) if r == "SECTOR_THEME"]
    themed = entity_hits(config, text, relations=set(RELATION_RANK))
    assert [t for t, r, _ in themed if r == "SECTOR_THEME"]


def test_entity_hits_never_returns_a_disabled_or_unresolved_ticker(
        config_with_unresolved):
    from harel.enrich.linker import entity_hits

    hits = entity_hits(config_with_unresolved, "ZZTEST reports record quarter")
    assert "ZZTEST" not in {ticker for ticker, _, _ in hits}


def test_entity_hits_compiles_one_linker_per_config(config):
    """~1,000 regexes per build, and collectors call this per record over
    thousands of openFDA rows."""
    from harel.enrich.linker import _LINKER_CACHE, entity_hits

    entity_hits(config, "Teva Pharmaceutical Industries Ltd")
    first = _LINKER_CACHE[id(config)][1]
    entity_hits(config, "Camtek Ltd")
    assert _LINKER_CACHE[id(config)][1] is first
    # The entry holds the Config, which is what makes keying on id() safe.
    assert _LINKER_CACHE[id(config)][0] is config


def test_hits_and_link_read_the_same_rules(linker):
    """One rule construction, not two. A second matcher with its own copy of
    these loops is how the ordinary-word guard went missing in the first
    place."""
    from harel.enrich.linker import causal_eligible

    text = "Check Point raises full-year revenue guidance"
    from_link = {l.ticker: l.relation for l in linker.link(item(text))
                 if causal_eligible(l.relation)}
    assert from_link == {t: r for t, r, _ in linker.hits(text)}
    assert from_link


@pytest.mark.parametrize("ticker,headline,expected", [
    # Real stories that the first, stricter guard wrongly withdrew. Headlines
    # drop the "Ltd"; demanding a corporate suffix loses the news.
    ("ALLT", "Allot to Release Second Quarter 2026 Results", True),
    ("ALLT", "Allot's second-quarter results arrive before an Aug. 12 webcast", True),
    ("NICE", "NICE Price Target Cut to $111.00/Share From $130.00 by Morgan Stanley", True),
    ("NVMI", "Nova slides as semiconductor selloff outweighs recent momentum", True),
    ("ICL", "ICL Announces Second Quarter 2026 Earnings Call", True),
    # Ordinary-word uses. Google News returned every one of these from the
    # per-ticker query, and each was tagged DIRECT at 0.92 on the query alone.
    ("ALLT", "PH, US allot P42b for anti-TB, HIV drive", False),
    ("ALLT", "Orchid Pharma to allot shares to Dhanuka Labs shareholders", False),
    ("ALLT", "Supreme Court directs the state to allot adjacent land", False),
    ("ALLT", "Allot time for debates on Punjab's critical issues", False),
    ("NICE", "It was a nice day in Nice, France", False),
    ("NVMI", "Nova Scotia announces new energy plan", False),
    ("KEN", "Ken Griffin buys a stake in something", False),
])
def test_an_ordinary_word_is_only_a_company_when_it_reads_as_one(
        config, ticker, headline, expected):
    from harel.enrich.linker import direct_evidence

    assert direct_evidence(config.ticker(ticker), headline) is expected, headline
