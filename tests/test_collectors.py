from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeHttpClient, fixture_json, fixture_text
from harel.collect.base import CollectorContext
from harel.collect.clinicaltrials import ClinicalTrialsCollector
from harel.collect.edgar import EdgarFullTextCollector, EdgarSubmissionsCollector
from harel.collect.fda import OpenFdaCollector
from harel.collect.federal_register import (
    FederalRegisterCollector,
    FederalRegisterPublicInspectionCollector,
)
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
    """The BIS rule still reaches the semiconductor names - but through the
    linker reading the document, not through the collector asserting it.

    The collector used to seed every ticker in the queried sector at 0.92 on the
    strength of the API having returned the document at all. `conditions[term]`
    searches the FULL document text, so that promoted a passing mention to a
    high-confidence link: a Family Violence Prevention rule became LPSN and NICE
    news. The evidence now has to be in the title or abstract.
    """
    from harel.enrich.linker import EntityLinker

    routes = {"federalregister.gov/api/v1/documents.json": fixture_json("federal_register.json")}
    collector = FederalRegisterCollector(
        config.sources["federal_register"], ctx(config, db, routes)
    )
    items = list(collector.collect())
    assert items
    assert not any(i.seed_tickers for i in items), \
        "the collector must not assert who a regulator document is about"

    linker = EntityLinker(config)
    semis = {link.ticker for i in items for link in linker.link(i)}
    assert {"TSEM", "NVMI", "CAMT"} & semis

    doc = items[0]
    assert doc.meta["doc_type"] == "Rule"
    assert doc.title.startswith("[FR]")
    assert doc.meta["queried_for"], "keep why we fetched it, for the drill-down"


def test_a_regulator_document_that_names_nobody_links_to_nobody(config, db):
    """A hospice wage index is not a BrainsWay catalyst and a family-violence
    rule is not a LivePerson catalyst. Both were, at score 45, because the
    Federal Register full-text search returned them for a sector term buried
    somewhere in a 137-page document."""
    from harel.enrich.linker import EntityLinker

    routes = {"federalregister.gov/api/v1/documents.json": {"results": [{
        "document_number": "2026-15686",
        "title": "Medicare Program; FY 2027 Hospice Wage Index and Payment Rate Update",
        "abstract": "This rule updates the hospice wage index and payment rates.",
        "html_url": "https://www.federalregister.gov/documents/2026/08/03/2026-15686/x",
        "publication_date": "2026-08-03",
        "type": "Rule",
        "agencies": [{"name": "Centers for Medicare & Medicaid Services",
                      "slug": "centers-for-medicare-medicaid-services"}],
        "agency_names": ["Centers for Medicare & Medicaid Services"],
    }]}}
    collector = FederalRegisterCollector(
        config.sources["federal_register"], ctx(config, db, routes)
    )
    linker = EntityLinker(config)
    for item in collector.collect():
        assert not linker.link(item), \
            f"{item.title[:60]!r} named no company we track and must link to none"


def test_public_inspection_asks_only_for_fields_that_endpoint_has(config, db):
    """Public-inspection documents carry a smaller schema than published ones,
    and the API rejects the whole request rather than ignoring a field it does
    not recognise. Asking for the published-document field list returned
    `400 field 'abstract' not valid`, so the one source that buys lead time
    yielded nothing at all, silently, every pass."""
    routes = {"public-inspection-documents/current.json":
              fixture_json("federal_register_pi.json")}
    client = FakeHttpClient(routes)
    collector = FederalRegisterPublicInspectionCollector(
        config.sources["federal_register_public_inspection"],
        CollectorContext(config=config, client=client, db=db, lookback_hours=72),
    )
    items = list(collector.collect())

    invalid = {"abstract", "action", "docket_ids", "topics", "significant",
               "effective_on", "comments_close_on", "citation"}
    requested = {
        part.split("=", 1)[1]
        for call in client.calls for part in call.split("&")
        if part.startswith("fields[]=")
    }
    assert requested, "the collector must request fields"
    assert not (requested & invalid), \
        f"asked the PI endpoint for fields it rejects: {sorted(requested & invalid)}"
    assert items, "the fixture names agencies our sectors watch"
    assert items[0].title.startswith("[FR-EARLY]")


def test_public_inspection_is_dated_when_it_was_filed_not_when_it_will_publish(
        config, db):
    """`publication_date` on a PI document is the day it is *scheduled* to
    appear - days in the future. Read as the publication time it gives the item
    a negative age and a recency bonus it has not earned. `filed_at` is when it
    became public, which is where the lead time starts."""
    routes = {"public-inspection-documents/current.json":
              fixture_json("federal_register_pi.json")}
    collector = FederalRegisterPublicInspectionCollector(
        config.sources["federal_register_public_inspection"], ctx(config, db, routes)
    )
    items = list(collector.collect())
    assert items

    for item in items:
        assert item.published_at < datetime.now(timezone.utc), \
            f"{item.title[:50]!r} is dated in the future"
        scheduled = item.meta["scheduled_publication_date"]
        assert scheduled and item.published_at.date().isoformat() < scheduled, \
            "filed_at must precede the scheduled publication date"

    cms = next(i for i in items if "2026-15652" in i.meta["document_number"])
    assert cms.published_at == datetime(2026, 7, 30, 20, 15, tzinfo=timezone.utc)
    assert cms.meta["filing_type"] == "special"
    assert cms.meta["docket_ids"] == ["CMS-1830-F"]
    assert cms.summary, "no abstract in this schema - fall back to the TOC pair"


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


def test_a_completion_date_refined_to_a_day_is_not_a_delay(config, db):
    """The direction was decided by comparing the raw strings, and
    ClinicalTrials.gov returns this field at either month or day precision. So
    "2027-01" -> "2027-01-15", a sponsor doing nothing but naming a day inside
    the month it already stated, sorted later as a string and went out as
    "pushed out" - a delay, which this module's docstring calls usually a
    negative. Clearing the field sorted the other way and became fabricated good
    news."""
    import copy

    from harel.collect.clinicaltrials import _completion_change

    assert "pushed out" not in _completion_change("2027-01", "2027-01-15")
    assert "pulled in" not in _completion_change("2027-01", "2027-01-15")
    # A value that is gone, or one that was never there, is unknown - not early.
    assert "pulled in" not in _completion_change("2027-01-15", "")
    assert "pushed out" not in _completion_change("", "2027-01-15")
    # Real moves still read as moves, at either precision.
    assert "pushed out" in _completion_change("2027-01-15", "2027-06-30")
    assert "pulled in" in _completion_change("2027-06-30", "2027-01-15")
    assert "pushed out" in _completion_change("2027-01", "2027-06")

    # ...and the same through the collector, which is where it reaches a reader.
    source = config.sources["clinicaltrials"]
    month = fixture_json("clinicaltrials.json")
    month["studies"][0]["protocolSection"]["statusModule"][
        "primaryCompletionDateStruct"]["date"] = "2026-11"
    day = copy.deepcopy(month)
    day["studies"][0]["protocolSection"]["statusModule"][
        "primaryCompletionDateStruct"]["date"] = "2026-11-30"

    url = "clinicaltrials.gov/api/v2/studies"
    list(ClinicalTrialsCollector(source, ctx(config, db, {url: month})).collect())
    refined = [i for i in ClinicalTrialsCollector(source, ctx(config, db, {url: day})).collect()
               if i.meta["nct_id"] == "NCT05555555"]

    assert refined, "the fingerprint changed, so the item must still be emitted"
    change = refined[0].meta["change"]
    assert "pushed out" not in change and "pulled in" not in change, change


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


def test_a_four_letter_peer_name_cannot_file_a_strangers_recall_as_our_news(config, db):
    """`peer_names` was the only matcher loop here with no minimum-length guard,
    against the 26 peer names in universe.yaml shorter than five characters -
    Nova, KLA, GSK, OCP, SQM, AES, Wiz, SES, Ada, Bing. openFDA answers
    `recalling_firm:"Nova"` with 24 device records for Nova Biomedical and 8
    drug records for Nova Products, neither of which is Nova Ltd, so a
    blood-glucose-meter recall was emitted with relation PEER against Camtek - a
    semiconductor inspection company. The three sibling loops and
    rss._cross_read_terms already draw the line at five.

    Nor is it Nova Ltd's own news. The length guard alone left the DIRECT leg
    standing, because "Nova" IS Nova Ltd's name and `match_names` draws its line
    at four - so the recall simply changed which company it libelled. What
    actually separates the two is the word after the name, which is the guard
    `EntityLinker` has always applied to an ordinary-word name and this second,
    simpler matcher had never been told about.
    """
    from harel.collect.fda import _EntityMatcher

    routes = {"device/enforcement.json": fixture_json("openfda_device_recall_nova.json")}
    collector = OpenFdaCollector(config.sources["fda_enforcement"], ctx(config, db, routes))
    items = list(collector.collect())

    assert not any("CAMT" in i.seed_tickers for i in items), \
        "a glucose meter recall is not competitor news for a chip-inspection company"
    assert not any("NVMI" in i.seed_tickers for i in items), \
        "Nova Biomedical is not Nova Ltd; naming no company we track, it links to none"

    # The rule, not just this record: no PEER rule may be built from a name too
    # short to be distinctive.
    short = [why for _, _, relation, why in _EntityMatcher(collector).rules
             if relation == "PEER" and len(why.split(" ", 1)[1]) < 5]
    assert not short, f"peer rules built from names under five characters: {short}"

    # And the guard must not have simply switched the name off: the real company
    # still matches, because "Nova Ltd" and "Nova Measuring Instruments" read as
    # a company where "Nova Biomedical Corporation" reads as someone else.
    matcher = _EntityMatcher(collector)
    assert ("NVMI", "DIRECT") in {
        (t, rel) for t, rel, _ in matcher.match("Nova Ltd recalls a metrology tool")}
    assert ("NVMI", "DIRECT") in {
        (t, rel) for t, rel, _ in
        matcher.match("Nova Measuring Instruments Ltd optical metrology system")}
    assert matcher.match("Nova Biomedical Corporation glucose meter") == []


def test_a_scraped_letter_is_dated_by_its_row_not_by_the_scrape(config, db):
    """Every scraped link was stamped with the collection time, while DATE_RE
    sat compiled and referenced nowhere beside the code that should have used
    it. A warning letter issued 2026-03-15 and first collected 2026-08-01 was
    filed as 2026-08-01 news - and since the href is the external_id, upsert
    re-stamped it on every later pass too, so its age stayed at zero and it
    could never fall out of a recency-ordered feed or a since_hours window."""
    from harel.collect.fda import HtmlListingCollector

    routes = {"fda.gov": fixture_text("fda_warning_letters.html")}
    collector = HtmlListingCollector(
        config.sources["fda_warning_letters"], ctx(config, db, routes)
    )
    items = {i.seed_tickers[0]: i for i in collector.collect()}

    # The row states the posted date first and the letter's own issue date
    # second; the letter date is the one next to the link.
    assert items["TEVA"].published_at.date().isoformat() == "2026-03-15"
    assert items["CGEN"].published_at.date().isoformat() == "2026-06-02"
    assert not items["TEVA"].meta.get("undated")
    assert items["TEVA"].meta["listing_date"] == "2026-03-15"


def test_a_dateless_listing_row_keeps_the_stamp_it_was_first_given(config, db):
    """The import-alert rows carry no date anywhere, so one has to be invented.
    Inventing it *again* on every pass is the bug: external_id is the href, so
    the uid is stable and upsert refreshes published_at, which kept the item
    permanently minutes old. Freeze the first stamp and mark it undated - the
    convention the feed already renders as "date unknown, seen <when>" and the
    scorer already caps."""
    from harel.collect.fda import HtmlListingCollector
    from harel.models import ScoredItem

    routes = {"fda.gov": fixture_text("fda_warning_letters.html")}
    source = config.sources["fda_warning_letters"]

    first = next(i for i in HtmlListingCollector(source, ctx(config, db, routes)).collect()
                 if i.meta.get("undated"))
    assert "KMDA" in first.seed_tickers
    db.upsert_item(
        ScoredItem(raw=first, links=[], events=[], score=5.0, per_ticker_score={},
                   tier="NOISE", reasons=[]),
        dedupe_key="d", cluster_id="c",
    )

    second = next(i for i in HtmlListingCollector(source, ctx(config, db, routes)).collect()
                  if i.meta.get("undated"))
    assert second.published_at == first.published_at, \
        "a second pass re-stamped it, so it could never age out of the feed"


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


def test_a_maya_schema_break_reaches_the_run_report(config, db, monkeypatch):
    """A rename in MAYA's schema is the failure this collector is built to
    survive, and it used to leave no trace anywhere a human looks: the zero-record
    path never called warn(), so nothing reached RunReport.warnings. Its only
    signal was source_state.last_error, which is not one - it is written under
    the source key so each name overwrote the last, and the generator finishes
    without raising, so the pipeline stamps a fresh last_ok_at and clears
    last_error at the end of the same pass. `harel doctor` showed the biggest
    structural edge in this basket as healthy-and-quiet while it returned
    nothing at all."""
    monkeypatch.delenv("TASE_API_KEY", raising=False)
    renamed = {"Data": [{"reportSubject": "דוח מיידי",
                         "whenPublished": "2026-07-30T09:05:00"}]}
    collector = MayaCollector(
        config.sources["maya_tase"], ctx(config, db, {"mayaapi.tase.co.il": renamed})
    )

    assert list(collector.collect()) == []
    assert any("no parseable records" in w for w in collector.warnings), \
        "the only channel a human reads is RunReport.warnings"
    assert any("TEVA" in w for w in collector.warnings), \
        "name the affected tickers - one overwritten message is not a report"
    assert "TEVA" in (db.get_source_state("maya_tase").get("last_error") or ""), \
        "source_state must carry every affected name, not just the last one"


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


def test_phrase_queries_drop_the_agency_filter_but_faa_keeps_it(config, db):
    """Once a term is an exact phrase, the phrase is the precision and the
    agency list only removes true positives. A source that forces its own
    agency (faa_ads) is the exception - there the agency IS the subject."""
    routes = {"federalregister.gov/api/v1/documents.json": fixture_json("federal_register.json")}

    client = FakeHttpClient(routes)
    list(FederalRegisterCollector(
        config.sources["federal_register"],
        CollectorContext(config=config, client=client, db=db, lookback_hours=72),
    ).collect())
    phrase_calls = [c for c in client.calls if "conditions%5Bterm%5D=%22" in c
                    or 'conditions[term]="' in c]
    assert phrase_calls, "the config must exercise at least one multi-word term"
    for call in phrase_calls:
        assert "conditions[agencies][]" not in call, \
            f"phrase query should not bind an agency: {call[:130]}"

    faa_client = FakeHttpClient(routes)
    list(FederalRegisterCollector(
        config.sources["faa_ads"],
        CollectorContext(config=config, client=faa_client, db=db, lookback_hours=72),
    ).collect())
    assert faa_client.calls, "faa_ads must still issue queries"
    for call in faa_client.calls:
        assert "federal-aviation-administration" in call, \
            "faa_ads must always bind its own agency"


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


def test_form4_separates_a_grant_from_an_open_market_trade(config, db):
    """Nothing parsed Form 4 transaction codes, so an RSU award to an SVP and an
    executive buying on the open market scored identically off the form type -
    and the awards, being far more numerous, took the top of the feed above real
    earnings. Codes P/S are the signal; A/M/F/G are compensation plumbing."""
    from harel.collect.edgar import EdgarSubmissionsCollector, ROUTINE_FORM4

    url = "https://www.sec.gov/Archives/edgar/data/1/000/xslF345X05/doc.xml"
    cases = {
        "form4_grant.xml": (ROUTINE_FORM4, False, "grant/award"),
        "form4_open_market.xml": ("4", True, "open-market SELL"),
    }
    for fixture, (want_form, want_signal, want_label) in cases.items():
        collector = EdgarSubmissionsCollector(
            config.sources["sec_edgar_submissions"],
            ctx(config, db, {"doc.xml": fixture_text(fixture)}),
        )
        detail = collector._form4_detail("4", url)
        assert detail is not None, f"{fixture} did not parse"
        assert detail["form_type"] == want_form
        assert detail["open_market"] is want_signal
        assert want_label in detail["label"]

    # The routine form_type must carry a hard cap, or the split changes nothing.
    assert ROUTINE_FORM4 in config.scoring.noise_form_types
    assert config.scoring.noise_form_types[ROUTINE_FORM4] <= 20


def test_form4_classification_survives_a_second_pass(config, db):
    """The submissions feed re-emits the same filings every pass, so the detail
    fetch is skipped the second time round. Skipping alone silently degraded the
    item: it was rebuilt without its classification and upsert overwrote the
    enriched row, so titles reverted to "FORM 4" and grants scored like trades
    again. The stored result has to be carried forward."""
    from harel.collect.edgar import EdgarSubmissionsCollector, ROUTINE_FORM4

    collector = EdgarSubmissionsCollector(
        config.sources["sec_edgar_submissions"],
        ctx(config, db, {"doc.xml": fixture_text("form4_grant.xml")}),
    )
    url = "https://www.sec.gov/Archives/edgar/data/1/000/xslF345X05/doc.xml"
    first = collector._form4_detail("4", url)
    assert first and first["form_type"] == ROUTINE_FORM4

    # Simulate the row already being stored, then rebuild it with no network.
    import json as _json
    db.conn.execute(
        "INSERT INTO items (uid, source, source_kind, external_id, title, url,"
        " published_at, collected_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        ("u1", "sec_edgar_submissions", "edgar", "acc:doc", "t", url,
         "2026-07-30T12:00:00+00:00", "2026-07-30T12:00:00+00:00",
         _json.dumps({"insider": first})),
    )
    db.conn.commit()

    carried = db.stored_meta("sec_edgar_submissions", "acc:doc")
    assert carried and carried["insider"]["form_type"] == ROUTINE_FORM4, \
        "a later pass must reuse the classification instead of dropping it"


def test_form4_never_counts_the_same_shares_in_both_tables(config, db):
    """`<transactionShares>` appears in BOTH Form 4 tables and an option
    exercise is reported in both - once as the derivative exercised, once as the
    underlying stock acquired - so summing a flat regex over the document
    doubled the size. TEVA's four most recent Form 4s printed "option exercise
    43,478 sh" against a true 21,739, and 28,984 against a true 14,492.
    Doubling an insider's size is the one number in that headline anyone acts
    on."""
    from harel.collect.edgar import EdgarSubmissionsCollector

    url = "https://www.sec.gov/Archives/edgar/data/1/000/xslF345X05/doc.xml"
    collector = EdgarSubmissionsCollector(
        config.sources["sec_edgar_submissions"],
        ctx(config, db, {"doc.xml": fixture_text("form4_exercise.xml")}),
    )
    detail = collector._form4_detail("4", url)

    assert detail is not None
    assert detail["shares"] == 21739, \
        f"the same 21,739 shares were counted twice: {detail['label']}"
    assert "43,478" not in detail["label"]
    assert detail["codes"] == ["M"], "one exercise, reported in two tables"

    # A grant that exists only in Table I still reports its own figure, and a
    # Table II-only filing has no other number to give.
    grant = EdgarSubmissionsCollector(
        config.sources["sec_edgar_submissions"],
        ctx(config, db, {"doc.xml": fixture_text("form4_grant.xml")}),
    )._form4_detail("4", url)
    assert grant["shares"] == 24000


def test_form4_detail_survives_a_renderer_version_past_nine(config, db):
    """The renderer directory is versioned and the version is not always one
    digit. `/xslF345X0\\d/` matches what TEVA files today (X06) but not an
    xslF345X10 - which would leave the URL unchanged, drop the detail fetch, and
    silently downgrade EVERY Form 4 back to plain "4", scored like an
    open-market purchase rather than routine paperwork."""
    from harel.collect.edgar import EdgarSubmissionsCollector, ROUTINE_FORM4

    for version in ("xslF345X06", "xslF345X10"):
        collector = EdgarSubmissionsCollector(
            config.sources["sec_edgar_submissions"],
            ctx(config, db, {"doc.xml": fixture_text("form4_grant.xml")}),
        )
        detail = collector._form4_detail(
            "4", f"https://www.sec.gov/Archives/edgar/data/1/000/{version}/doc.xml"
        )
        assert detail is not None, f"{version} lost the classification"
        assert detail["form_type"] == ROUTINE_FORM4

    # A URL with no renderer directory is still refused - that is what the
    # equality guard is actually for.
    plain = EdgarSubmissionsCollector(
        config.sources["sec_edgar_submissions"],
        ctx(config, db, {"doc.xml": fixture_text("form4_grant.xml")}),
    )._form4_detail("4", "https://www.sec.gov/Archives/edgar/data/1/000/doc.xml")
    assert plain is None


def test_fulltext_query_keeps_the_suffix_when_the_name_is_a_common_word(config):
    """Dropping the legal suffix is right for a distinctive name, but "NICE Ltd"
    became "NICE" and "Allot Ltd" became "Allot", so any filing using the word
    "nice" or "allotment" was collected as a peer mention - 110 of ALLT's links
    came from sovereign bond prospectuses."""
    from harel.collect.edgar import _fulltext_name

    assert _fulltext_name("NICE Ltd") == "NICE Ltd"
    assert _fulltext_name("Allot Ltd") == "Allot Ltd"
    assert _fulltext_name("Nova Ltd") == "Nova Ltd"
    # Multi-token names stay stripped - the phrase is distinctive without it.
    assert _fulltext_name("Teva Pharmaceutical Industries Ltd") == "Teva Pharmaceutical Industries"
    assert _fulltext_name("Palo Alto Networks Inc") == "Palo Alto Networks"

    # No active name may reduce to a single bare token.
    for ticker in config.active_tickers:
        q = _fulltext_name(config.ticker(ticker).name)
        assert len(q.split()) >= 2, f"{ticker} full-text query {q!r} is one bare token"


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
