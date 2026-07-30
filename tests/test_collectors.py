from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeHttpClient, fixture_json, fixture_text
from harel.collect.base import CollectorContext
from harel.collect.clinicaltrials import ClinicalTrialsCollector
from harel.collect.edgar import EdgarFullTextCollector, EdgarSubmissionsCollector
from harel.collect.fda import OpenFdaCollector
from harel.collect.federal_register import FederalRegisterCollector
from harel.collect.maya import MayaCollector
from harel.collect.prices import PriceCollector
from harel.collect.rss import RssCollector

# The fixtures are stamped 2026-07-29/30, so the lookback must reach them.
LOOKBACK_HOURS = (
    datetime.now(timezone.utc) - datetime(2026, 7, 20, tzinfo=timezone.utc)
).total_seconds() / 3600


def ctx(config, db, routes):
    return CollectorContext(
        config=config, client=FakeHttpClient(routes), db=db,
        lookback_hours=max(LOOKBACK_HOURS, 72),
    )


# ------------------------------------------------------------------ EDGAR -- #
def test_edgar_submissions_parses_filings(config, db):
    routes = {"submissions/CIK0000818686": fixture_json("edgar_teva_submissions.json")}
    collector = EdgarSubmissionsCollector(
        config.sources["sec_edgar_submissions"], ctx(config, db, routes)
    )
    items = [i for i in collector.collect() if "TEVA" in i.seed_tickers]

    forms = {i.meta["form_type"] for i in items}
    assert {"8-K", "424B5", "S-8"} <= forms

    eight_k = next(i for i in items if i.meta["form_type"] == "8-K")
    assert eight_k.meta["items"] == ["2.02", "9.01"]
    assert eight_k.meta["item_severity"] == "high"
    assert "Results of operations" in eight_k.title
    assert eight_k.url.startswith("https://www.sec.gov/Archives/edgar/data/818686/")
    assert eight_k.seed_relation == "DIRECT"


def test_edgar_submissions_survives_a_missing_cik(config, db):
    """Every other issuer must still be collected when one 404s."""
    collector = EdgarSubmissionsCollector(
        config.sources["sec_edgar_submissions"], ctx(config, db, {})
    )
    items = list(collector.collect())
    assert items == []
    assert collector.warnings, "a failure must be reported, not swallowed silently"


def test_edgar_full_text_excludes_the_companys_own_filings(config, db):
    """The point of full-text search is OTHER issuers mentioning us."""
    collector = EdgarFullTextCollector(
        config.sources["sec_edgar_full_text"], ctx(config, db, {"efts.sec.gov": fixture_json("edgar_fts.json")})
    )
    items = [i for i in collector.collect() if "TEVA" in i.seed_tickers]
    filers = {i.meta["filer"] for i in items}
    assert any("VIATRIS" in f for f in filers)
    assert not any("TEVA PHARMACEUTICAL" in f for f in filers), \
        "Teva's own filing is the submissions collector's job, not this one"
    assert all(i.seed_relation == "PEER" for i in items)


# --------------------------------------------------------- FEDERAL REGISTER -- #
def test_federal_register_maps_bis_rule_to_semi_names(config, db):
    routes = {"federalregister.gov/api/v1/documents.json": fixture_json("federal_register.json")}
    collector = FederalRegisterCollector(
        config.sources["federal_register"], ctx(config, db, routes)
    )
    items = list(collector.collect())
    assert items
    semis = {t for i in items for t in i.seed_tickers}
    assert {"TSEM", "NVMI", "CAMT"} & semis
    doc = items[0]
    assert doc.meta["doc_type"] == "Rule"
    assert doc.seed_relation == "SECTOR_REG"
    assert doc.title.startswith("[FR]")


# --------------------------------------------------------- CLINICALTRIALS -- #
def test_clinicaltrials_emits_only_meaningful_phases(config, db):
    routes = {"clinicaltrials.gov/api/v2/studies": fixture_json("clinicaltrials.json")}
    collector = ClinicalTrialsCollector(
        config.sources["clinicaltrials"], ctx(config, db, routes)
    )
    items = list(collector.collect())
    ncts = {i.meta["nct_id"] for i in items}
    assert "NCT05555555" in ncts, "a Phase 3 rival readout must be collected"
    assert "NCT01111111" not in ncts, "Phase 1 dose escalation is not tradeable"


def test_clinicaltrials_only_reports_actual_changes(config, db):
    routes = {"clinicaltrials.gov/api/v2/studies": fixture_json("clinicaltrials.json")}
    source = config.sources["clinicaltrials"]

    first = list(ClinicalTrialsCollector(source, ctx(config, db, routes)).collect())
    assert first

    second = list(ClinicalTrialsCollector(source, ctx(config, db, routes)).collect())
    assert second == [], "an unchanged registry snapshot is not news"


# ------------------------------------------------------------------- FDA -- #
def test_openfda_enforcement_matches_our_names_and_drops_the_rest(config, db):
    routes = {"api.fda.gov/drug/enforcement.json": fixture_json("openfda_enforcement.json")}
    collector = OpenFdaCollector(config.sources["fda_enforcement"], ctx(config, db, routes))
    items = list(collector.collect())

    assert len(items) == 1, "the unrelated firm's recall must be dropped"
    item = items[0]
    assert "TEVA" in item.seed_tickers
    assert item.meta["classification"] == "Class II"
    assert "FDA RECALL" in item.title


# ------------------------------------------------------------------ RSS -- #
def test_rss_reads_issuer_feed_and_tags_the_owner(config, db):
    routes = {"ir.cgen.com": fixture_text("ir_feed.xml"),
              "cgen.com/rss/pressrelease": fixture_text("ir_feed.xml")}
    collector = RssCollector(config.sources["company_ir_rss"], ctx(config, db, routes))
    items = [i for i in collector.collect() if "CGEN" in i.seed_tickers]
    assert len(items) == 2
    titles = " ".join(i.title for i in items)
    assert "Rilvegostomig" in titles
    assert all(i.seed_relation == "DIRECT" for i in items)


def test_rss_survives_a_dead_feed(config, db):
    collector = RssCollector(config.sources["fda_press"], ctx(config, db, {}))
    assert list(collector.collect()) == []
    assert collector.warnings


# ----------------------------------------------------------------- MAYA -- #
def test_maya_parses_hebrew_reports_and_converts_to_utc(config, db):
    routes = {"mayaapi.tase.co.il": fixture_json("maya_reports.json")}
    collector = MayaCollector(config.sources["maya_tase"], ctx(config, db, routes))
    items = list(collector.collect())
    assert items
    item = items[0]
    assert item.lang == "he"
    assert item.meta["israeli_hours"] is True
    assert item.title.startswith("[MAYA]")
    # 09:05 Israel time in July (UTC+3) is 06:05 UTC.
    assert item.published_at.hour == 6 and item.published_at.minute == 5


def test_maya_finds_records_in_an_unexpected_envelope(config, db):
    """Field renames must degrade, not crash."""
    wrapped = {"Result": {"Data": {"Reports": fixture_json("maya_reports.json")}}}
    routes = {"mayaapi.tase.co.il": wrapped}
    collector = MayaCollector(config.sources["maya_tase"], ctx(config, db, routes))
    assert list(collector.collect())


# --------------------------------------------------------------- PRICES -- #
def test_stooq_computes_adv_and_flags_an_unexplained_move(config, db):
    routes = {"stooq.com": fixture_text("stooq_teva.csv")}
    collector = PriceCollector(config.sources["prices_stooq"], ctx(config, db, routes))
    items = list(collector.collect())

    snap = db.latest_price("TEVA")
    assert snap is not None
    assert snap["change_pct"] == pytest.approx(10.0, abs=0.1)
    assert snap["vol_mult"] > 4

    alerts = [i for i in items if i.meta.get("kind") == "unexplained_move"]
    assert alerts, "a +10% move with no stored news must raise a tape alert"
    assert alerts[0].meta["forced_score"] >= 70


def test_edgar_full_text_link_cannot_be_upgraded_to_direct(config, db):
    """The synthesised headline contains our own name; the linker must not read
    it back out and call a competitor's filing our news."""
    from harel.enrich.linker import EntityLinker

    collector = EdgarFullTextCollector(
        config.sources["sec_edgar_full_text"],
        ctx(config, db, {"efts.sec.gov": fixture_json("edgar_fts.json")}),
    )
    item = next(i for i in collector.collect() if "TEVA" in i.seed_tickers)
    links = EntityLinker(config).link(item)
    teva = next(l for l in links if l.ticker == "TEVA")
    assert teva.relation == "PEER", links
