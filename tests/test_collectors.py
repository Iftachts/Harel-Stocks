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


# ------------------------------------------- issuer feeds and the calendar -- #
def _issuer_routes(published_days_ago: int = 32, report_in_days: int = 20):
    """An ORA-shaped title-only feed plus the release page behind it.

    Dates are rendered relative to today rather than recorded, because both ends
    of what is under test are relative: the entry has to be older than the news
    window, and the date it announces has to be inside the 120-day horizon.
    """
    published = datetime.now(timezone.utc) - timedelta(days=published_days_ago)
    report = datetime.now(timezone.utc) + timedelta(days=report_in_days)
    call = report + timedelta(days=1)
    prior = published - timedelta(days=60)
    # %-d and %-m are not portable to Windows; build the day number by hand.
    page = fixture_text("ir_release_aspx.html").replace(
        "{PRIOR_DATE}", f"{prior:%B} {prior.day}, {prior.year}"
    ).replace(
        "{RELEASE_DAY}", f"{published.month}/{published.day}/{published.year}"
    ).replace(
        "{RELEASE_LONG}", f"{published:%B} {published.day}, {published.year}"
    ).replace(
        "{REPORT_DATE}", f"{report:%A}, {report:%B} {report.day}, {report.year}"
    ).replace(
        "{CALL_DATE}", f"{call:%A}, {call:%B} {call.day}, {call.year}"
    )
    feed = fixture_text("ir_feed_title_only.xml").replace(
        "{PUBLISHED}", published.strftime("%a, %d %b %Y %H:%M:%S +0000"))
    return {"investor.ormat.com/rss": feed,
            "investor.ormat.com/news-events": page}, report.date().isoformat()


def _ir_collector(config, db, routes, lookback_hours=12.0):
    """The `watch` window - twelve hours - which is where this went wrong."""
    return RssCollector(
        config.sources["company_ir_rss"],
        CollectorContext(config=config, client=FakeHttpClient(routes), db=db,
                         lookback_hours=lookback_hours),
    )


def test_an_issuer_is_read_back_far_enough_to_see_its_own_reporting_date(config, db):
    """Ormat announced on 1 July that it would report on 5 August, then its feed
    went quiet. Every pass in between ran a 12- or 72-hour news window, so the
    one item carrying the date was always too old to collect and the calendar
    had no entry for ORA at all - while the issuer had published it five weeks
    earlier."""
    from harel.pipeline import _earnings_date

    routes, expected = _issuer_routes()
    collector = _ir_collector(config, db, routes)
    items = [i for i in collector.collect() if "ORA" in i.seed_tickers]

    assert len(items) == 1, [i.title for i in items]
    announcement = items[0]
    assert announcement.meta["calendar_backfill"] is True
    assert _earnings_date(announcement) == (expected, "Q2 results")


def test_only_a_future_date_survives_the_longer_issuer_window(config, db):
    """The wider window is for one thing: a date that has not happened yet. The
    same feed's storage-facility release is exactly as old and stays dropped -
    otherwise "read further back" quietly means "re-collect stale news"."""
    routes, _ = _issuer_routes()
    items = list(_ir_collector(config, db, routes).collect())
    titles = " ".join(i.title for i in items)
    assert "Shirk Energy Storage" not in titles

    # And an announcement whose date has already passed is not a schedule.
    stale, _ = _issuer_routes(published_days_ago=200, report_in_days=-170)
    assert [i for i in _ir_collector(config, db, stale).collect()
            if "ORA" in i.seed_tickers] == []


def test_the_release_body_is_fetched_because_the_feed_withholds_it(config, db):
    """TEVA, ORA, ICL and CGEN all publish through `pressrelease.aspx`, which
    emits a headline and an empty description. The date is in the page - and the
    page is an .aspx, so it wraps its whole body in one <form>. Stripping that
    as chrome left 123 characters of <title> and no date at all."""
    routes, expected = _issuer_routes()
    collector = _ir_collector(config, db, routes)
    item = next(i for i in collector.collect() if "ORA" in i.seed_tickers)

    assert item.summary == "", "the fixture must keep the feed's empty description"
    assert "after the market closes" in item.body
    assert expected.split("-")[0] in item.body

    # The decoy - the replay of the quarter just gone - survives into the body,
    # so it is the extractor and not the scraper that has to reject it.
    prior = datetime.now(timezone.utc) - timedelta(days=92)
    assert f"{prior:%B} {prior.day}, {prior.year}" in item.body
    fetched = [c for c in collector.client.calls if "news-events" in c]
    assert len(fetched) == 1, "one fetch per announcement, not one per entry"


def test_a_feed_that_hands_over_a_summary_is_not_fetched_again(config, db):
    """The page fetch is a repair for a feed that withholds the release, not a
    routine second request. CGEN's fixture carries its own summaries."""
    routes = {"ir.cgen.com": fixture_text("ir_feed.xml")}
    collector = _ir_collector(config, db, routes, lookback_hours=max(LOOKBACK_HOURS, 72))
    list(collector.collect())
    assert not [c for c in collector.client.calls if "news-details" in c]


def test_a_strangers_feed_gets_no_calendar_window(config, db):
    """The longer read-back is the issuer's alone. A regulator or aggregator
    feed re-reading five weeks of entries every pass would republish old news as
    today's, which is the failure `_entry_datetime` already exists to prevent."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%a, %d %b %Y %H:%M:%S +0000")
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <title>FDA</title><link>https://fda.gov</link><description>x</description>
      <item><title>FDA to Report Second Quarter 2026 Financial Results</title>
      <link>https://www.fda.gov/x</link><description>on August 5, 2026</description>
      <pubDate>{old}</pubDate></item></channel></rss>"""
    collector = RssCollector(
        config.sources["fda_press"],
        CollectorContext(config=config, client=FakeHttpClient({"fda.gov": feed}),
                         db=db, lookback_hours=12.0),
    )
    assert list(collector.collect()) == []


def test_an_announcement_that_never_names_the_quarter_still_carries_its_date():
    """AudioCodes has no reachable IR feed, so its 4 August date arrived only as
    "Earnings Preview: AudioCodes to Report Financial Results Pre-market on
    August 04" - an announcing verb, a day of the month, and not one word saying
    which quarter. The subject leg demanded a quarter and threw the date away."""
    from harel.models import RawItem
    from harel.pipeline import _earnings_date

    ahead = datetime.now(timezone.utc) + timedelta(days=20)

    def item(title):
        return RawItem(source="google_news", source_kind="rss", external_id=title,
                       title=title, url="", published_at=datetime.now(timezone.utc))

    got = _earnings_date(item(
        f"Earnings Preview: AudioCodes to Report Financial Results Pre-market "
        f"on {ahead:%B} {ahead.day}"))
    assert got and got[0] == ahead.date().isoformat(), got

    # Still three legs. A results report is not an announcement of one, and a
    # conference appearance is not about results at all.
    assert _earnings_date(item(
        f"AudioCodes Reports Financial Results for the Period Ended "
        f"{ahead:%B} {ahead.day}")) is None
    assert _earnings_date(item(
        f"AudioCodes to Present at an Investor Conference on {ahead:%B} "
        f"{ahead.day}")) is None


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
    # Document *searches* only. The collector also fetches public-inspection
    # filing times by document number, and a lookup keyed by the number a search
    # already returned has no agency to bind.
    faa_searches = [c for c in faa_client.calls if "api/v1/documents.json" in c]
    assert faa_searches, "faa_ads must still issue queries"
    for call in faa_searches:
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


def test_maya_refusing_every_name_is_not_a_quiet_day(config, db):
    """Twenty HTTP 403s in a row left `harel doctor` reporting the source
    healthy, with a fresh last_ok_at and no error.

    The sibling fix covered a schema break - `_find_records` matching nothing -
    but an HTTP failure took a different branch that only warned, and warnings
    live in the run report while doctor reads source_state. The 403s here are
    known and documented (the MAYA 2.0.0 feed is pending approval), which is
    exactly why this mattered: the panel was only reporting the failures
    somebody had already thought to look for.
    """
    from harel.collect.maya import MayaCollector
    from harel.http import Response

    class RefusingClient(FakeHttpClient):
        def get(self, url, **kwargs):
            self.calls.append(url)
            return Response(403, "", b"", {}, url)

    collector = MayaCollector(
        config.sources["maya_tase"],
        CollectorContext(config=config, client=RefusingClient({}), db=db,
                         lookback_hours=72),
    )
    assert list(collector.collect()) == []

    state = db.get_source_state("maya_tase")
    assert state.get("last_error"), \
        "a source that refused every request must not read as a quiet day"
    assert "403" in state["last_error"]

    # And a warning a human actually sees, not only a state column.
    assert any("403" in w for w in collector.warnings)


# ===================================================================== #
# Federal Register: when a document FIRST became readable by anyone.
#
# The rendering tests live in this file rather than with the other
# presentation tests because they are the same change: a first-public
# moment the collector records and the terminal does not show buys
# nothing. Appended as one block - another agent appends here too.
# ===================================================================== #
def test_published_documents_learn_when_they_first_went_public(config, db):
    """A published document's `publication_date` is when it appeared in the
    Register; the same document number sat on public inspection days earlier,
    and that earlier moment is the one a reader is entitled to. documents.json
    cannot carry it - `filed_at` is an invalid field there - so it is joined
    from the per-document PI endpoint.
    """
    routes = {
        "federalregister.gov/api/v1/documents.json":
            fixture_json("federal_register.json"),
        "public-inspection-documents/2026-14321.json":
            fixture_json("federal_register_pi_lookup.json"),
    }
    client = FakeHttpClient(routes)
    collector = FederalRegisterCollector(
        config.sources["federal_register"],
        CollectorContext(config=config, client=client, db=db,
                         lookback_hours=max(LOOKBACK_HOURS, 72)),
    )
    items = list(collector.collect())
    assert items

    doc = next(i for i in items if i.meta["document_number"] == "2026-14321")
    # 08:45 ET on 2026-07-15, two weeks before it published on 2026-07-29.
    assert doc.meta["first_public_at"] == "2026-07-15T12:45:00+00:00"
    assert doc.meta["first_public_at"] < doc.published_at.isoformat()


def test_the_public_inspection_join_costs_one_request_for_the_whole_pass(config, db):
    """The endpoint takes a comma-separated batch. One request per document
    would add hundreds per pass to a collector that already runs one query per
    sector per term."""
    routes = {
        "federalregister.gov/api/v1/documents.json":
            fixture_json("federal_register.json"),
        "public-inspection-documents/":
            fixture_json("federal_register_pi_lookup.json"),
    }
    client = FakeHttpClient(routes)
    collector = FederalRegisterCollector(
        config.sources["federal_register"],
        CollectorContext(config=config, client=client, db=db,
                         lookback_hours=max(LOOKBACK_HOURS, 72)),
    )
    documents = list(collector.collect())
    assert documents

    lookups = [c for c in client.calls if "public-inspection-documents/" in c]
    assert len(lookups) == 1, f"expected one batched lookup, made {len(lookups)}"


def test_public_inspection_records_the_filing_as_the_first_public_moment(config, db):
    """On this path there is nothing to join: `filed_at` IS the moment the
    document became readable, and it is now stated rather than left implicit in
    `published_at`."""
    routes = {"public-inspection-documents/current.json":
              fixture_json("federal_register_pi.json")}
    collector = FederalRegisterPublicInspectionCollector(
        config.sources["federal_register_public_inspection"], ctx(config, db, routes)
    )
    items = list(collector.collect())
    assert items

    fda = next(i for i in items if i.meta["document_number"] == "2026-15701")
    assert fda.meta["first_public_at"] == "2026-07-31T12:45:00+00:00"
    assert fda.meta["first_public_at"] == fda.published_at.isoformat()


def test_a_document_never_on_public_inspection_gets_no_invented_timestamp(config, db):
    """Unknown document numbers come back under `errors.not_found`. A document
    with no earlier public moment must report none - deriving one from the
    publication date would be the same lie in a new field."""
    routes = {
        "federalregister.gov/api/v1/documents.json":
            fixture_json("federal_register.json"),
        "public-inspection-documents/":
            {"count": 0, "results": [], "errors": {"not_found": ["2026-14321"]}},
    }
    collector = FederalRegisterCollector(
        config.sources["federal_register"], ctx(config, db, routes)
    )
    items = list(collector.collect())
    assert items, "documents must survive a lookup that finds nothing"
    assert all(i.meta.get("first_public_at") is None for i in items)


def test_a_failing_public_inspection_lookup_does_not_lose_the_documents(config, db):
    """The join is an enrichment. Losing every Federal Register document because
    a secondary endpoint was down would be a far worse failure than missing the
    lead-time label."""
    routes = {"federalregister.gov/api/v1/documents.json":
              fixture_json("federal_register.json")}
    collector = FederalRegisterCollector(
        config.sources["federal_register"], ctx(config, db, routes)
    )
    items = list(collector.collect())
    assert items
    assert all(i.meta.get("first_public_at") is None for i in items)


# ------------------------------------------------------------- rendering --- #
def test_a_gap_is_spoken_as_a_gap_and_not_as_an_age():
    """`ago` says "before X"; a coverage gap is a span between two moments and
    must not borrow that word."""
    from harel.serve import hebrew as he

    assert he.duration(2355) == "39 שעות"
    assert he.duration(45) == "45 דק׳"
    assert he.duration(60) == "שעה"
    # The dual is not optional politeness, and carries no digit at all - so a
    # caller must not wrap it in a numeric LTR island.
    assert he.duration(120) == "שעתיים"
    assert he.duration(60 * 48) == "יומיים"
    assert he.duration(60 * 24 * 3) == "3 ימים"
    assert "לפני" not in he.duration(2355)
    # Hours run past 24: the incident is 39 hours, and a day rounds it away.
    assert he.duration(60 * 39) == "39 שעות"


def test_minutes_between_survives_what_the_feed_actually_carries():
    from harel.serve import hebrew as he

    assert he.minutes_between("2026-07-31T12:45:00+00:00",
                              "2026-08-02T04:00:00+00:00") == 2355
    assert he.minutes_between(None, "2026-08-02T04:00:00+00:00") is None
    assert he.minutes_between("2026-08-02T04:00:00+00:00", None) is None
    assert he.minutes_between("not a date", "2026-08-02T04:00:00+00:00") is None


def test_the_feed_cell_shows_the_coverage_gap_it_used_to_hide():
    """The UFLPA entity list was on public inspection from Friday 08:45 ET and
    was not collected until Sunday. The cell reported only our collection time,
    which showed the 5 hours and hid the 39."""
    from harel.serve.terminal import _time_cell

    cell = _time_cell({
        "t": "2026-07-31T12:45:00+00:00",
        "forthcoming": True,
        "publishes_on": "2026-08-03",
        "first_public_at": "2026-07-31T12:45:00+00:00",
        "discovered_at": "2026-08-02T04:00:00+00:00",
    })

    assert "זמין לציבור" in cell
    assert "ו׳ 08:45 ET" in cell, "Friday, on the exchange's own clock"
    assert "א׳ 00:00 ET" in cell, "collected Sunday, on the same clock"
    assert "פיגור גילוי" in cell
    assert "39 שעות" in cell
    # Still says when it publishes: the lead time is why we hold it at all.
    assert "מתפרסם" in cell
    # Latin runs stay isolated from the bidi algorithm, page-wide rule.
    assert "class='ltr'" in cell
    assert "<script" not in cell.lower()


def test_a_forthcoming_document_with_no_filing_time_keeps_the_old_line():
    """Not every forthcoming document has a public-inspection record. Where
    there is no better answer, when we found it is still the honest one."""
    from harel.serve.terminal import _time_cell

    cell = _time_cell({
        "t": "2026-08-02T04:00:00+00:00",
        "forthcoming": True,
        "publishes_on": "2026-08-03",
        "discovered_at": "2026-08-02T04:00:00+00:00",
    })
    assert "נודע לנו" in cell
    assert "זמין לציבור" not in cell


def test_an_already_published_copy_does_not_repeat_its_own_timestamp():
    """On a public-inspection item that has since published, `t` and the first
    public moment are the same instant - a public-availability line there
    restates the cell above it and says nothing."""
    from harel.serve.terminal import _time_cell

    public = "זמין לציבור"
    same = "2026-07-31T12:45:00+00:00"
    assert public not in _time_cell({"t": same, "first_public_at": same})

    # But a published document that was readable two weeks earlier must say so.
    cell = _time_cell({"t": "2026-07-29T00:00:00+00:00",
                       "first_public_at": "2026-07-15T12:45:00+00:00"})
    assert public in cell
    assert "ד׳ 08:45 ET" in cell


def test_the_honest_lag_is_measured_from_public_availability():
    """`_detection_lag_he` measures against `published_at`, which for a Federal
    Register document is a *scheduled* date - so a document sitting unfetched on
    public inspection since Friday still scored as lead time."""
    from harel.serve.terminal import _public_lag_he

    late = "באיחור"
    assert _public_lag_he(None) == "-"
    assert "39 שעות" in _public_lag_he(2355)
    assert late in _public_lag_he(2355)
    assert "2355" not in _public_lag_he(2355), "nobody reads a lag in minutes"
    assert late not in _public_lag_he(12)
    # Ahead of the document being public is not something this source can do,
    # but if the clocks disagree it must not read as a negative age.
    assert "זמן קדימה" in _public_lag_he(-90)
    assert "-90" not in _public_lag_he(-90)
