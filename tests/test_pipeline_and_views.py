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


def test_the_json_api_declares_utf8(tmp_path):
    """Bare `application/json` leaves the encoding unstated, and a client that
    falls back to ISO-8859-1 turns every Hebrew headline into mojibake - which
    is half of what this basket is for. The HTML terminal only looked fine
    because it carries its own <meta charset>."""
    pytest.importorskip("fastapi")
    from harel.serve.api import create_app

    app = create_app(str(tmp_path / "t.db"))
    media = app.router.default_response_class.media_type
    assert "charset=utf-8" in media.lower(), media


def test_a_source_switched_off_in_config_is_still_reported(ran):
    """A disabled source used to vanish from the coverage panel entirely, so
    "24/29 sources live" could not be reconciled with what was on screen. Off
    on purpose still has to be visible - silence and blindness must look
    different."""
    report, views = ran
    warnings = " | ".join(views._coverage_warnings())
    disabled = [s.key for s in views.config.sources.values() if not s.enabled]
    assert disabled, "the shipped config must have at least one disabled source"
    for key in disabled:
        assert key in warnings, f"disabled source {key} is invisible in the panel"


def test_stale_failure_counters_do_not_warn_for_feeds_we_no_longer_poll(ran, db):
    """Counters are keyed "<source>:<url>" and survive a config edit. A feed we
    have since fixed or switched off would otherwise report its old failures for
    ever, and a warning nobody can act on trains you to ignore the panel."""
    report, views = ran
    db.set_source_state("ema_news:https://www.ema.europa.eu/en/OLD-DEAD.xml",
                        consecutive_failures=42, last_error="HTTP 404")
    db.set_source_state("calcalist:https://www.calcalist.co.il/gone.xml",
                        consecutive_failures=42, last_error="HTTP 403")
    warnings = " | ".join(views._coverage_warnings())
    assert "OLD-DEAD.xml" not in warnings, "stale URL should not raise a failure warning"
    assert "has failed 42 times" not in warnings


def test_tape_markers_do_not_crowd_real_news_out_of_the_feed(ran):
    """"[TAPE] X up 7% with no matching news" is the absence of a story and
    carries a forced score of 70, so six of them outranked every real headline -
    the Teva earnings story sat underneath at 30. They belong in whats_moving.

    Also guards the limit: excluding them must not shrink the page, which a
    post-fetch filter did (limit=1 fetched only tape and returned nothing)."""
    report, views = ran

    default = views.feed(min_score=0, hours=LOOKBACK, limit=40)
    assert default["items"], "the default feed must not be empty"
    assert not any(i["title"].startswith("[TAPE]") for i in default["items"])

    for limit in (1, 3, 5):
        page = views.feed(min_score=0, hours=LOOKBACK, limit=limit)
        assert page["items"], f"limit={limit} returned nothing"
        assert len(page["items"]) <= limit

    with_tape = views.feed(min_score=0, hours=LOOKBACK, limit=40, include_tape=True)
    assert len(with_tape["items"]) >= len(default["items"])


def test_last_session_close_tracks_the_bell_and_skips_weekends():
    from harel.views import MARKET_TZ, last_session_close

    def et(y, m, d, hh, mm=0):
        return datetime(y, m, d, hh, mm, tzinfo=MARKET_TZ)

    # Mid-session: the relevant close is yesterday's, so today's news counts.
    assert last_session_close(et(2026, 7, 30, 11)).astimezone(MARKET_TZ).day == 29
    # After the bell: today's close, so post-close filings are excluded.
    assert last_session_close(et(2026, 7, 30, 17)).astimezone(MARKET_TZ).day == 30
    # Sunday rolls back to Friday, not Saturday.
    sunday = last_session_close(et(2026, 8, 2, 12)).astimezone(MARKET_TZ)
    assert sunday.weekday() == 4 and sunday.day == 31


def test_a_story_published_after_the_bell_is_not_offered_as_the_cause(ran):
    """A Form 4 accepted at 16:13 ET cannot have moved a print that finished at
    16:00. Keep it visible as the next session's setup, but never as a driver.
    """
    report, views = ran
    from harel.views import last_session_close

    cutoff = last_session_close()
    moving = views.whats_moving(min_abs_pct=0.0)
    def when(row):
        return datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))

    checked = 0
    for mover in moving["movers"]:
        for driver in mover["drivers"]:
            assert when(driver) <= cutoff, (
                f"{mover['ticker']}: driver {driver['title'][:60]!r} was published "
                f"after the close and cannot explain the move"
            )
            checked += 1
        for late in mover.get("after_the_bell", []):
            assert when(late) > cutoff
    assert checked or moving["movers"], "the fixture must produce movers to check"


def test_corroboration_counts_independent_sources_not_documents(ran):
    """Twelve Form 4s filed the same afternoon are one source repeating itself.
    The agent manifest tells the model to trust this number."""
    report, views = ran
    for item in views.feed(min_score=0, hours=LOOKBACK, limit=200)["items"]:
        corr = item.get("corroboration")
        if not corr:
            continue
        sources = {item["source"]} | {a["source"] for a in item.get("also", [])}
        assert corr <= len(sources), (
            f"{item['title'][:60]!r} claims corroboration={corr} from "
            f"{len(sources)} distinct source(s)"
        )


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


def test_ticker_brief_rejects_a_symbol_outside_the_universe(ran):
    _, views = ran
    result = views.ticker_brief("ZZZZ")
    assert "error" in result
    assert "TEVA" in result["universe"], "the agent needs to know what IS covered"


def test_ticker_brief_rejects_an_unresolved_ticker(db, config_with_unresolved):
    views = Views(db=db, config=config_with_unresolved)
    result = views.ticker_brief("ZZTEST")
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
    """Silence must be distinguishable from blindness: the agent has to be told
    which sources are off or degraded before it can say "there is no news"."""
    _, views = ran
    warnings = " ".join(views.morning_brief(hours=LOOKBACK)["coverage_warnings"])
    assert "maya_tase" in warnings, "the MAYA fallback must be declared"
    assert "COURTLISTENER_TOKEN" in warnings, "sources off for a missing key must be named"


def test_morning_brief_warns_about_uncollected_tickers(db, config_with_unresolved):
    views = Views(db=db, config=config_with_unresolved)
    warnings = " ".join(views.morning_brief()["coverage_warnings"])
    assert "ZZTEST" in warnings, "the agent must know a ticker is not being collected"


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


def test_health_reports_missing_keys_and_content(ran):
    _, views = ran
    health = views.health()
    assert health["db"]["items"] > 0
    assert any(k["env_var"] == "COURTLISTENER_TOKEN" for k in health["missing_api_keys"])
    assert any(k["env_var"] == "TASE_API_KEY" for k in health["running_degraded"])


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
