from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeHttpClient, fixture_json, fixture_text
from harel.collect.base import CollectorContext
from harel.collect.clinicaltrials import ClinicalTrialsCollector
from harel.collect.edgar import EdgarFullTextCollector, EdgarSubmissionsCollector
from harel.collect.fda import OpenFdaCollector
from harel.collect.federal_register import FederalRegisterCollector
from harel.collect.maya import MayaCollector, MayaScheduleCollector
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


def _maya_v2(config, db, monkeypatch, issuer_id=629, routes=None):
    """Official-mode MAYA collector. `tase_issuer_id` is patched in rather than
    written to universe.yaml - the real issuer numbers are still unknown."""
    monkeypatch.setenv("TASE_API_KEY", "test-key")
    if issuer_id is None:
        monkeypatch.delitem(config.ticker("TEVA").raw, "tase_issuer_id", raising=False)
    else:
        monkeypatch.setitem(config.ticker("TEVA").raw, "tase_issuer_id", issuer_id)
    if routes is None:
        routes = {"datawise.tase.co.il": fixture_json("maya_v2_disclosures.json")}
    client = FakeHttpClient(routes)
    collector = MayaCollector(
        config.sources["maya_tase"],
        CollectorContext(config=config, client=client, db=db, lookback_hours=LOOKBACK_HOURS),
    )
    return collector, client


def test_maya_v2_publication_date_is_utc_and_not_shifted(config, db, monkeypatch):
    """v2 documents publicationDate as UTC ('...Z'). The public endpoint returns
    naive Israel local time, so the parser subtracts the Israel offset - doing
    that to a UTC stamp would move every report 3 hours early and corrupt both
    the recency decay and the lookback cutoff."""
    collector, _ = _maya_v2(config, db, monkeypatch)
    items = [i for i in collector.collect() if "TEVA" in i.seed_tickers]
    stamped = {i.meta["maya_report_id"]: i.published_at for i in items}

    assert stamped[1662001].hour == 6 and stamped[1662001].minute == 5, (
        "06:05Z must stay 06:05 UTC, not become 03:05"
    )
    assert stamped[1662002].hour == 5


def test_maya_public_endpoint_still_reads_naive_time_as_israel_local(config, db, monkeypatch):
    """The v2 fix must not regress the undocumented public channel."""
    monkeypatch.delenv("TASE_API_KEY", raising=False)
    collector = MayaCollector(
        config.sources["maya_tase"],
        ctx(config, db, {"mayaapi.tase.co.il": fixture_json("maya_reports.json")}),
    )
    item = next(i for i in collector.collect() if i.meta.get("maya_report_id") == 1552211)
    # 09:05 Israel local in July (UTC+3) -> 06:05 UTC
    assert item.published_at.hour == 6 and item.published_at.minute == 5


def test_maya_v2_sends_issuer_id_and_api_key_header(config, db, monkeypatch):
    collector, client = _maya_v2(config, db, monkeypatch)
    list(collector.collect())

    call = next(c for c in client.calls if "companies-disclosures/by-issuer" in c)
    assert "datawise.tase.co.il" in call, "openapi.tase.co.il is the portal, not the API"
    assert "IssuerId=629" in call and "FromDate=" in call and "ToDate=" in call

    headers = client.headers_seen[0]
    assert headers.get("apiKey") == "test-key"
    assert headers.get("Accept-Language") == "he-IL", "required by the v2 spec"
    assert "X-Maya-With" not in headers, "public-endpoint spoofing headers do not belong here"


def test_maya_v2_keeps_priority_correction_and_event_taxonomy(config, db, monkeypatch):
    """v2 renamed isPriority -> isPriorityReport. Losing it would silently drop
    the flag that marks the high-signal reports."""
    collector, _ = _maya_v2(config, db, monkeypatch)
    by_id = {i.meta["maya_report_id"]: i for i in collector.collect()}

    assert by_id[1662001].meta["is_priority"] is True
    assert by_id[1662001].meta["tase_events"] == ["דוחות כספיים"]
    assert by_id[1662002].meta["is_priority"] is False
    assert by_id[1662002].meta["is_correction"] is True


def test_maya_v2_refuses_to_query_a_security_id_as_an_issuer_id(config, db, monkeypatch):
    """tase_id is a security number; IssuerId is a different registry. Sending
    the wrong one would return nothing and read exactly like 'no news'."""
    collector, client = _maya_v2(config, db, monkeypatch, issuer_id=None)
    list(collector.collect())

    assert not any("629014" in c for c in client.calls), \
        "must never send the security id as IssuerId"
    assert any("tase_issuer_id" in w and "TEVA" in w for w in collector.warnings), \
        "skipping a name silently is the failure mode this source already has"


def test_maya_v2_collects_names_that_have_no_security_id(config, db, monkeypatch):
    """PANW and LPSN carry no tase_id, so the old "dual listed" gate skipped
    them. The official API keys on issuer number, which they do have - gating on
    tase_id would silently drop two names from the highest-value source."""
    assert config.ticker("PANW").tase_id is None
    assert config.ticker("LPSN").tase_id is None

    collector, client = _maya_v2(config, db, monkeypatch)
    list(collector.collect())

    queried = " ".join(client.calls)
    assert f"IssuerId={config.ticker('PANW').raw['tase_issuer_id']}" in queried
    assert f"IssuerId={config.ticker('LPSN').raw['tase_issuer_id']}" in queried


def test_maya_v2_reports_truncated_pages(config, db, monkeypatch):
    """meta.hasMore is set but the documented parameters carry no continuation
    field, so truncation has to surface rather than be silently accepted."""
    collector, _ = _maya_v2(config, db, monkeypatch)
    list(collector.collect())
    assert any("more disclosures" in w for w in collector.warnings)


def _maya_schedule(config, db, monkeypatch, issuer_id=629):
    lookups = fixture_json("maya_schedule_lookups.json")
    routes = {
        "financial-report-schedule/event-types": lookups["report_types"],
        "financial-report-schedule/period-types": lookups["period_types"],
        "financial-report-schedule/by-schedule-date": fixture_json("maya_schedule.json"),
    }
    monkeypatch.setenv("TASE_API_KEY", "test-key")
    if issuer_id is None:
        monkeypatch.delitem(config.ticker("TEVA").raw, "tase_issuer_id", raising=False)
    else:
        monkeypatch.setitem(config.ticker("TEVA").raw, "tase_issuer_id", issuer_id)
    client = FakeHttpClient(routes)
    collector = MayaScheduleCollector(
        config.sources["maya_schedule"],
        CollectorContext(config=config, client=client, db=db, lookback_hours=LOOKBACK_HOURS),
    )
    return collector, client


def test_maya_schedule_resolves_enum_ids_to_labels(config, db, monkeypatch):
    """A calendar row saying "type 1 period 3" is useless to a trader; the
    product ships lookup endpoints precisely so it can read properly."""
    collector, _ = _maya_schedule(config, db, monkeypatch)
    items = [i for i in collector.collect() if "TEVA" in i.seed_tickers]

    assert items
    titles = " | ".join(i.title for i in items)
    assert "דוח כספי" in titles and "רבעון 3" in titles
    assert "type 1" not in titles and "period 3" not in titles


def test_maya_schedule_becomes_a_calendar_row_not_a_headline(config, db, monkeypatch):
    """The deliverable is the calendar entry. The item itself must be capped out
    of the ranked feed - a date three months out is not intraday news."""
    from harel.enrich.materiality import MaterialityScorer
    from harel.enrich.linker import EntityLinker

    collector, _ = _maya_schedule(config, db, monkeypatch)
    item = next(i for i in collector.collect() if "TEVA" in i.seed_tickers)

    assert item.meta["scheduled_report_on"] == "2026-11-12"
    assert item.meta["scheduled_time"] == "09:30"

    links = EntityLinker(config).link(item)
    scored = MaterialityScorer(config).score(item, links)
    assert scored.score <= 10, (
        f"scheduled-date items must stay out of the feed, scored {scored.score}"
    )


def test_maya_schedule_needs_no_public_fallback_but_says_so(config, db, monkeypatch):
    monkeypatch.delenv("TASE_API_KEY", raising=False)
    collector = MayaScheduleCollector(
        config.sources["maya_schedule"],
        ctx(config, db, {}),
    )
    assert list(collector.collect()) == []
    assert any("TASE_API_KEY" in w for w in collector.warnings)


def test_maya_schedule_skips_names_without_an_issuer_number(config, db, monkeypatch):
    """Every other name still collects; only the unmapped one is skipped, and it
    is named rather than quietly dropped."""
    collector, client = _maya_schedule(config, db, monkeypatch, issuer_id=None)
    items = list(collector.collect())

    assert items, "the other names must still be collected"
    assert not any("TEVA" in i.title for i in items)
    assert not any("629014" in c for c in client.calls), "never send the security id"
    assert any("tase_issuer_id" in w and "TEVA" in w for w in collector.warnings)


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


def test_yahoo_move_is_measured_against_the_prior_session(config, db):
    """`chartPreviousClose` is relative to the requested range (5d), so using it
    reports a week's drift as an intraday move and fires false tape alerts on
    most of the basket. The prior session's close is `previousClose`."""
    payload = {"chart": {"result": [{"meta": {
        "regularMarketPrice": 35.39,
        "previousClose": 34.71,        # yesterday's close -> +2.0%
        "chartPreviousClose": 31.18,   # close 5 sessions back -> +13.5%
    }}]}}
    collector = PriceCollector(
        config.sources["prices_yahoo"],
        ctx(config, db, {"query1.finance.yahoo.com": payload}),
    )
    list(collector.collect())

    snap = db.latest_price("TEVA")
    assert snap is not None
    assert snap["change_pct"] == pytest.approx(2.0, abs=0.1), \
        "a +2% day must not be reported as +13.5% because the chart range is 5d"


def test_faa_source_only_runs_for_sectors_that_claim_it(config, db):
    """faa_ads forces agency=FAA, but the sector loop ran for every sector - so
    the FAA got queried with medical-device and geothermal terms and the results
    were linked to BrainsWay and Ormat as sector regulation."""
    routes = {"federalregister.gov/api/v1/documents.json": fixture_json("federal_register.json")}
    client = FakeHttpClient(routes)
    collector = FederalRegisterCollector(
        config.sources["faa_ads"],
        CollectorContext(config=config, client=client, db=db, lookback_hours=72),
    )
    planned_sectors = {sector for _, _, _, sector in collector._query_plan()}

    assert planned_sectors, "the FAA source must still run for aerospace"
    for sector_key in planned_sectors:
        assert "faa_ads" in config.sector(sector_key).regulators, \
            f"{sector_key} does not list faa_ads as a regulator"
    assert "medical_devices" not in planned_sectors
    assert "renewable_power" not in planned_sectors


def test_federal_register_searches_the_phrase_not_the_loose_words(config, db):
    """`conditions[term]` ANDs the words across the full document text, so an
    unquoted "entity list" matches any rule containing both words - which under
    commerce-department (NOAA's parent) dragged fisheries and marine-mammal
    rules in against the semiconductor names at confidence 0.9."""
    routes = {"federalregister.gov/api/v1/documents.json": fixture_json("federal_register.json")}
    client = FakeHttpClient(routes)
    collector = FederalRegisterCollector(
        config.sources["federal_register"],
        CollectorContext(config=config, client=client, db=db, lookback_hours=72),
    )
    list(collector.collect())

    terms = [
        call.split("conditions[term]=")[1].split("&")[0]
        for call in client.calls if "conditions[term]=" in call
    ]
    assert terms, "the collector must issue term queries"
    multiword = [t for t in terms if "%20" in t or " " in t]
    assert multiword, "the fixture config must exercise a multi-word term"
    for term in multiword:
        assert term.startswith('"') and term.endswith('"'), \
            f"multi-word term {term!r} must be phrase-quoted"


def test_edgar_acceptance_time_is_eastern_not_utc():
    """EDGAR stamps acceptanceDateTime with a trailing Z but the clock is
    Eastern. Reading it as UTC moved every filing 4-5 hours earlier, which put
    after-close filings back inside the session and let a Form 4 accepted at
    16:13 ET be read as the cause of that day's move."""
    from harel.collect.edgar import _parse_edgar_dt

    summer = _parse_edgar_dt("2026-07-30T16:13:33.000Z")      # EDT, UTC-4
    winter = _parse_edgar_dt("2026-01-15T16:13:33.000Z")      # EST, UTC-5
    assert summer.isoformat() == "2026-07-30T20:13:33+00:00"
    assert winter.isoformat() == "2026-01-15T21:13:33+00:00"

    # ...and that puts it after the 16:00 ET bell, where it belongs.
    from harel.views import last_session_close
    assert summer > last_session_close(summer)


def test_edgar_filing_date_keeps_its_eastern_calendar_day():
    from harel.collect.edgar import _parse_edgar_dt

    dt = _parse_edgar_dt("2026-07-30")
    assert dt.isoformat() == "2026-07-30T04:00:00+00:00", "ET midnight, not UTC midnight"


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
