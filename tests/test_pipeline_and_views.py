"""End-to-end: fixtures in, ranked agent-ready JSON out - with no network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import FakeHttpClient, fixture_json, fixture_text
from harel.pipeline import Pipeline
from harel.views import Views

LOOKBACK = max(
    72,
    (datetime.now(timezone.utc) - datetime(2026, 7, 20, tzinfo=timezone.utc)).total_seconds()
    / 3600,
)

ROUTES = {
    "submissions/CIK0000818686": fixture_json("edgar_teva_submissions.json"),
    "efts.sec.gov": fixture_json("edgar_fts.json"),
    "federalregister.gov/api/v1/documents.json": fixture_json("federal_register.json"),
    "clinicaltrials.gov/api/v2/studies": fixture_json("clinicaltrials.json"),
    "api.fda.gov/drug/enforcement.json": fixture_json("openfda_enforcement.json"),
    "mayaapi.tase.co.il": fixture_json("maya_reports.json"),
    "stooq.com": fixture_text("stooq_teva.csv"),
    "ir.cgen.com": fixture_text("ir_feed.xml"),
}

SOURCES = [
    "sec_edgar_submissions", "sec_edgar_full_text", "federal_register",
    "clinicaltrials", "fda_enforcement", "maya_tase", "prices_stooq",
    "company_ir_rss",
]


@pytest.fixture
def ran(config, db):
    pipeline = Pipeline(config=config, db=db, lookback_hours=LOOKBACK,
                        client=FakeHttpClient(ROUTES))
    report = pipeline.run(only=SOURCES)
    return report, Views(db=db, config=config)


def test_pipeline_stores_items_from_every_working_source(ran):
    report, _ = ran
    assert report.stored > 0
    producing = {k for k, v in report.by_source.items() if v}
    assert {"sec_edgar_submissions", "federal_register", "maya_tase"} <= producing


def test_pipeline_never_crashes_on_unmapped_sources(ran):
    report, _ = ran
    # Sources with no fixture must warn, not raise.
    assert report.errors == [], report.errors


def test_feed_is_ranked_and_agent_shaped(ran):
    _, views = ran
    feed = views.feed(min_score=0, hours=LOOKBACK, limit=50)
    assert feed["items"]

    scores = [i["score"] for i in feed["items"]]
    assert scores == sorted(scores, reverse=True)

    for item in feed["items"]:
        assert item["uid"] and item["source"] and item["t"]
        assert item["ticker"] in views.config.active_tickers
        assert item["relation"]
        assert item["why"], "the agent must be able to justify every link"


def test_noise_is_filtered_out_of_the_default_feed(ran):
    _, views = ran
    default = views.feed(hours=LOOKBACK, limit=100)   # min_score=45
    titles = " ".join(i["title"] for i in default["items"])
    assert "S-8" not in titles, "registration paperwork must not reach the trader"


def test_indirect_news_is_labelled_as_indirect(ran):
    _, views = ran
    feed = views.feed(min_score=0, hours=LOOKBACK, limit=200)
    indirect = [i for i in feed["items"] if i["relation"] not in ("DIRECT", "SUBSIDIARY")]
    assert indirect, "the read-across channel produced nothing"
    for item in indirect:
        assert item["relation"] in (
            "PRODUCT_RIVAL", "PEER", "CUSTOMER", "SUPPLIER", "SECTOR_REG",
            "SECTOR_THEME", "MACRO",
        )


def test_ticker_brief_separates_direct_from_read_across(ran):
    _, views = ran
    brief = views.ticker_brief("TEVA", hours=LOOKBACK)
    assert brief["ticker"] == "TEVA"
    assert brief["direct_news"], brief
    for item in brief["direct_news"]:
        assert item["relation"] in ("DIRECT", "SUBSIDIARY")
    for item in brief["indirect_news"]:
        assert item["relation"] not in ("DIRECT", "SUBSIDIARY")


def test_ticker_brief_rejects_an_unresolved_ticker(ran):
    _, views = ran
    result = views.ticker_brief("PAMW")
    assert "error" in result and result["hint"]


def test_search_finds_hebrew_content(ran):
    _, views = ran
    result = views.search("דיווח", limit=10)
    assert result["count"] >= 1, result


def test_search_finds_english_content(ran):
    _, views = ran
    result = views.search("export controls", limit=10)
    assert result["count"] >= 1, result


def test_morning_brief_surfaces_coverage_warnings(ran):
    _, views = ran
    brief = views.morning_brief(hours=LOOKBACK)
    warnings = " ".join(brief["coverage_warnings"])
    assert "PAMW" in warnings, "the agent must know a ticker is not being collected"


def test_whats_moving_joins_price_with_news(ran):
    _, views = ran
    moving = views.whats_moving(min_abs_pct=2.0)
    teva = next((m for m in moving["movers"] if m["ticker"] == "TEVA"), None)
    assert teva is not None
    assert teva["change_pct"] == pytest.approx(10.0, abs=0.2)


def test_item_detail_exposes_the_full_scoring_trace(ran):
    _, views = ran
    feed = views.feed(min_score=0, hours=LOOKBACK, limit=1)
    detail = views.item(feed["items"][0]["uid"])
    assert detail["reasons"]
    assert detail["tickers"]


def test_health_reports_unresolved_tickers_and_missing_keys(ran):
    _, views = ran
    health = views.health()
    assert any(t["ticker"] == "PAMW" for t in health["unresolved_tickers"])
    assert health["db"]["items"] > 0


def test_rerunning_the_pipeline_is_idempotent(config, db):
    client = FakeHttpClient(ROUTES)
    first = Pipeline(config=config, db=db, lookback_hours=LOOKBACK, client=client).run(
        only=["sec_edgar_submissions", "federal_register"]
    )
    count_after_first = db.counts()["items"]
    Pipeline(config=config, db=db, lookback_hours=LOOKBACK, client=client).run(
        only=["sec_edgar_submissions", "federal_register"]
    )
    assert db.counts()["items"] == count_after_first
    assert first.stored > 0


def test_terminal_renders_without_error(ran):
    from harel.serve.terminal import render_terminal

    _, views = ran
    html = render_terminal(views)
    assert html.startswith("<!doctype html>")
    assert "HAREL" in html
    assert "<script" not in html.lower(), "the terminal must stay script-free"


def test_calendar_is_seeded_from_collected_dates(ran):
    """Free sources leak forward-looking dates; we harvest them into a calendar."""
    _, views = ran
    entries = views.calendar(days=3650)["entries"]
    kinds = {e["kind"] for e in entries}
    assert kinds, "no calendar entries were extracted"
    assert {"trial_completion", "rule_effective"} & kinds, kinds
    for entry in entries:
        assert entry["date"] > "2026-07-01"
