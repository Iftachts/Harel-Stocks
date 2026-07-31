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
