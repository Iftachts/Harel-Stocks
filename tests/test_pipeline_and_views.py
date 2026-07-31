"""End-to-end: fixtures in, ranked agent-ready JSON out - with no network."""

from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone

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


def test_every_name_has_a_sector_benchmark(config):
    """A move only means something relative to its group. Without a benchmark
    every mover reads as unexplained, which is how a day when the semis index
    ran 8% produced sixteen rows of "no matching news"."""
    for ticker in config.active_tickers:
        sector = config.ticker(ticker).sector
        assert config.benchmark_for(sector), f"{ticker} ({sector}) has no benchmark"
    assert config.benchmark_symbols, "nothing to fetch"


def test_movers_report_the_move_left_after_the_sector(ran, db):
    """CAMT +8.1% on a day SOXX rose 8.5% is a -0.4pp stock-specific move, not
    an 8% one - the difference between a company event and being carried."""
    report, views = ran
    from harel.models import PriceSnapshot

    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="SOXX", asof=now, last=100.0,
                                prev_close=92.17, change_pct=8.5))
    db.save_price(PriceSnapshot(ticker="CAMT", asof=now, last=100.0,
                                prev_close=92.51, change_pct=8.1))

    camt = next((m for m in views.whats_moving(min_abs_pct=1.0)["movers"]
                 if m["ticker"] == "CAMT"), None)
    assert camt is not None, "CAMT should appear as a mover"
    assert camt["benchmark"] == "SOXX"
    assert camt["benchmark_pct"] == pytest.approx(8.5, abs=0.1)
    assert camt["relative_pct"] == pytest.approx(-0.4, abs=0.1)


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


# --------------------------------------------------------------------------- #
# Transparency: the trader has to be able to audit any line on the screen.
# A ranking that cannot be interrogated is a ranking that has to be taken on
# faith, and this user is a day trader who checks his own work.
# --------------------------------------------------------------------------- #
def _any_uid(views) -> str:
    items = views.feed(min_score=0, hours=LOOKBACK, limit=5)["items"]
    assert items, "the fixture run produced no items to explain"
    return items[0]["uid"]


def test_explain_answers_every_question_a_trader_would_ask(ran):
    """Where did this come from, when exactly, why is it tagged that symbol,
    how did it get that number, who else has it, and where do I go to check."""
    _, views = ran
    data = views.explain(_any_uid(views))
    assert "error" not in data

    origin = data["where_it_came_from"]
    assert origin["source"] and origin["source_label"]
    assert origin["trust_means"], "a trust number without its meaning is noise"
    assert origin["found_by"], "must name the query or feed that pulled it in"

    when = data["when"]
    assert "ET" in when["published_et"] and "IL" in when["published_israel"]
    assert "close" in when["vs_last_close"], "must place the item against the bell"

    assert data["who_it_is_about"], "an item with no link should never be stored"
    for link in data["who_it_is_about"]:
        assert link["why"], f"{link['ticker']} link has no stated reason"
        assert link["relation_means"], f"{link['relation']} is undocumented"

    assert data["how_it_scored"]["trace"]["item"], "no scoring trace"
    assert data["who_else_carried_it"]["corroboration"] >= 1
    for check in data["check_it_yourself"]:
        assert check["url"].startswith("http"), check
        assert check["checks"], "a verification link must say what it verifies"


def test_the_score_trace_is_unpacked_and_never_edited(ran, db):
    """The drill-down regroups the stored trace. If it can silently drop a step,
    the page becomes a nicer-looking lie than the number it explains."""
    _, views = ran
    uid = _any_uid(views)
    stored = db.item(uid)["reasons"]
    trace = views.explain(uid)["how_it_scored"]["trace"]

    shown = [s["step"] for s in trace["item"]]
    for ticker, steps in trace["per_ticker"].items():
        shown.extend(f"[{ticker}] {s['step']}" for s in steps)
    assert sorted(shown) == sorted(stored), "the trace was altered on the way out"


def test_a_uid_prefix_is_enough_and_a_wrong_one_says_so(ran):
    """Nobody retypes 40 hex characters off a screen."""
    _, views = ran
    uid = _any_uid(views)
    assert views.explain(uid[:10])["uid"] == uid
    missing = views.explain("ffffffffffffffff")
    assert "error" in missing and missing.get("hint")


def test_a_source_that_finds_nothing_keeps_its_last_success(config, db):
    """Recording "ok" only when items came back - and writing last_ok_at=None
    otherwise, which set_source_state merges in and so erases - made a quiet
    source indistinguishable from a dead one. openFDA is quiet for days."""
    client = FakeHttpClient({"api.fda.gov/drug/enforcement.json": {"results": []}})
    report = Pipeline(config=config, db=db, lookback_hours=LOOKBACK,
                      client=client).run(only=["fda_enforcement"])
    assert report.by_source.get("fda_enforcement") == 0, "fixture must return nothing"

    state = db.get_source_state("fda_enforcement")
    assert state.get("last_ok_at"), "a pass that finds nothing is still a pass"
    assert not state.get("last_error")
    assert state.get("items_last_run") == 0

    row = next(s for s in Views(db=db, config=config).sources_report()["sources"]
               if s["source"] == "fda_enforcement")
    assert row["last_ok_at"] and row["items_last_run"] == 0


def test_a_price_carries_its_provider_and_its_arithmetic(config, db):
    """-4.2% is a claim. A trader reconciling it against their broker needs to
    know it is a delayed Yahoo print and what it was divided by."""
    from harel.models import PriceSnapshot

    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="TEVA", asof=now, last=18.0, prev_close=20.0,
                                change_pct=-10.0, session="regular", provider="yahoo"))
    quote = Views(db=db, config=config).quote("TEVA")
    assert quote["provider"] == "yahoo"
    assert "delayed" in quote["provider_note"]
    assert "20.0" in quote["math"] and "-10.00%" in quote["math"]
    assert quote["age_minutes"] is not None

    mover = next(m for m in Views(db=db, config=config).whats_moving(1.0)["movers"]
                 if m["ticker"] == "TEVA")
    assert mover["quote"]["provider"] == "yahoo"


def test_every_relation_the_linker_can_emit_is_documented():
    """A relation with no plain-language meaning reaches the screen as a bare
    label, and the agent is told to explain what it cannot name."""
    from harel.enrich.linker import RELATION_RANK
    from harel.views import RELATION_MEANING

    assert set(RELATION_RANK) <= set(RELATION_MEANING), (
        set(RELATION_RANK) - set(RELATION_MEANING)
    )


def test_the_drill_down_page_renders_and_points_outward(ran):
    from harel.serve.terminal import render_item, render_sources, render_terminal

    _, views = ran
    uid = _any_uid(views)

    page = render_item(views, uid)
    assert page.startswith("<!doctype html>")
    assert "<script" not in page.lower()
    assert "Check it yourself" in page
    assert "How the score was built" in page
    assert "news.google.com/search" in page, "no outside verification link"

    # Every headline the terminal prints has to reach its own evidence. The
    # fixture corpus is older than the terminal's own 24h window, so render the
    # row builder against the items directly rather than the whole page.
    from harel.serve.terminal import _section

    rows = _section("Feed", views.feed(min_score=0, hours=LOOKBACK, limit=5)["items"])
    assert f"/item/{uid}" in rows, "the feed must link to the evidence"
    assert render_terminal(views).startswith("<!doctype html>")

    assert "Every source" in render_sources(views)


def test_detection_lag_measures_speed_not_the_backfill(db):
    """Windowing on collection put every item of the first two-week backfill in
    the sample - each "late" by up to fourteen days purely because it predated
    the install - and reported four-day medians for sources that are minutes
    behind. The window has to be on publication."""
    now = datetime.now(timezone.utc)

    def add(uid: str, published_h: float, collected_h: float) -> None:
        db.conn.execute(
            "INSERT INTO items (uid, source, source_kind, external_id, title, "
            "published_at, collected_at) VALUES (?,?,?,?,?,?,?)",
            (uid, "google_news", "rss", uid, "t",
             (now - timedelta(hours=published_h)).isoformat(),
             (now - timedelta(hours=collected_h)).isoformat()),
        )

    add("fresh1", published_h=2.0, collected_h=1.9)     # 6 min late
    add("fresh2", published_h=1.0, collected_h=0.9)     # 6 min late
    add("backfill", published_h=200.0, collected_h=1.0)  # 8 days "late"
    add("future", published_h=-1.0, collected_h=0.5)     # upstream clock is wrong
    db.conn.commit()

    stats = db.detection_lag(hours=6)["google_news"]
    assert stats["items"] == 2, "backfilled and future-stamped items must not count"
    assert stats["median_minutes"] == pytest.approx(6, abs=1)


# --------------------------------------------------------------------------- #
# Content classification: an effect is not a cause, and a marketing page is not
# news. Every case below came off the live terminal.
# --------------------------------------------------------------------------- #
def test_a_story_published_midsession_is_not_after_the_bell(ran, db):
    """The cutoff was always the last completed close, but the price on screen
    is whatever the snapshot holds. Mid-session that is *today's* live move, so
    a story published at 13:40 ET today - hours before the print it is compared
    with - was stamped "after the bell, not a cause". The bell had not rung."""
    from harel.models import PriceSnapshot

    _, views = ran
    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="TSEM", asof=now, last=100.0, prev_close=94.0,
                                change_pct=6.4, session="regular", provider="yahoo"))
    db.conn.execute(
        "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, title, "
        "url, published_at, collected_at, score, tier) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("midsession", "globes", "rss", "midsession", "Tower wins a foundry order",
         "https://example.com/x", (now - timedelta(hours=1)).isoformat(),
         now.isoformat(), 60.0, "HIGH"),
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, why, "
        "score) VALUES (?,?,?,?,?,?)",
        ("midsession", "TSEM", "DIRECT", 0.95, "test", 60.0),
    )
    db.conn.commit()

    tsem = next(m for m in views.whats_moving(min_abs_pct=1.0)["movers"]
                if m["ticker"] == "TSEM")
    titles = [d["title"] for d in tsem["drivers"]]
    assert "Tower wins a foundry order" in titles, tsem
    assert not tsem["after_the_bell"], "nothing is after a bell that has not rung"


def test_post_move_commentary_is_never_offered_as_the_cause(ran, db):
    """Quiver and MarketBeat generate an article BECAUSE the stock moved, quote
    the move back at you, then guess at reasons. Offering one as the driver of
    the move it describes is circular."""
    from harel.models import PriceSnapshot

    _, views = ran
    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="NICE", asof=now, last=100.0, prev_close=94.0,
                                change_pct=6.4, session="regular", provider="yahoo"))
    db.conn.execute(
        "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, title, "
        "url, published_at, collected_at, score, tier) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("recap", "google_news", "rss", "recap",
         "NICE Shares Gap Up - What's Next for Investors?",
         "https://example.com/r", (now - timedelta(hours=1)).isoformat(),
         now.isoformat(), 40.0, "NORMAL"),
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, why, "
        "score) VALUES (?,?,?,?,?,?)",
        ("recap", "NICE", "DIRECT", 0.95, "test", 40.0),
    )
    db.conn.commit()

    nice = next(m for m in views.whats_moving(min_abs_pct=1.0)["movers"]
                if m["ticker"] == "NICE")
    assert not any("Gap Up" in d["title"] for d in nice["drivers"])
    assert any("Gap Up" in c["title"] for c in nice["post_move_commentary"])
    assert nice["explained"] is False, "a recap must not mark a move as explained"


def test_an_unexplained_move_clear_of_its_sector_raises_an_alert(ran, db):
    """The feed is built from stories, so a day with no story produced no alert
    however violently the tape moved. TSEM +6.2% against SOXX +1.2%, nothing
    behind it, is exactly what has to be raised - and "0 alerts" was wrong."""
    from harel.models import PriceSnapshot

    _, views = ran
    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="SOXX", asof=now, last=100.0, prev_close=98.8,
                                change_pct=1.2, session="regular", provider="yahoo"))
    db.save_price(PriceSnapshot(ticker="TSEM", asof=now, last=100.0, prev_close=94.2,
                                change_pct=6.2, volume_multiple=0.6,
                                session="regular", provider="yahoo"))
    db.conn.commit()

    alerts = views.whats_moving(min_abs_pct=1.0)["unexplained"]
    tsem = next((a for a in alerts if a["ticker"] == "TSEM"), None)
    assert tsem is not None, "an unexplained 5pp outperformance must raise an alert"
    assert "UNEXPLAINED RELATIVE MOVE" in tsem["headline"]
    assert "SOXX" in tsem["headline"] and "+5.0pp" in tsem["headline"]
    assert tsem["checked"], "must say what was looked for and not found"
    assert views.morning_brief(hours=24)["unexplained_moves"], "must reach the brief"


def test_an_undated_feed_entry_cannot_masquerade_as_breaking_news(config, db):
    """BrainsWay's site feed put an entry literally titled "Title" into the feed
    as one-minute-old news at issuer trust, because an undated entry was stamped
    "now" and nothing recorded that we had invented the timestamp."""
    from harel.enrich.materiality import MaterialityScorer
    from harel.models import Link, RawItem

    scorer = MaterialityScorer(config)
    now = datetime.now(timezone.utc)
    item = RawItem(
        source="company_ir_rss", source_kind="rss", external_id="undated",
        title="Connecting Millions of People in Mexico", url="https://example.com/c",
        summary="", published_at=now, meta={"undated": True},
    )
    scored = scorer.score(item, [Link("GILT", "DIRECT", 0.95, "test")], now=now)
    assert scored.score <= MaterialityScorer.UNDATED_CAP, scored.reasons
    assert scored.tier == "NOISE"


def test_a_passive_13g_is_not_insider_activity(config):
    """A 13G is an index fund crossing 5%; a 13D is an activist. EDGAR labels
    them "SCHEDULE 13G", not "SC 13G", so the cap never fired and fourteen of
    them scored 40 as insider moves."""
    from harel.enrich.materiality import MaterialityScorer
    from harel.models import Link, RawItem

    scorer = MaterialityScorer(config)
    now = datetime.now(timezone.utc)
    for form in ("SCHEDULE 13G", "SCHEDULE 13G/A"):
        item = RawItem(
            source="sec_edgar_submissions", source_kind="edgar_submissions",
            external_id=form, title=f"[{form}] ORMAT TECHNOLOGIES, INC.",
            url="https://example.com/13g", summary="", published_at=now,
            meta={"form_type": form},
        )
        scored = scorer.score(item, [Link("ORA", "DIRECT", 0.97, "test")], now=now)
        assert scored.score <= 20, f"{form} scored {scored.score}: {scored.reasons}"

    activist = RawItem(
        source="sec_edgar_submissions", source_kind="edgar_submissions",
        external_id="13d", title="[SC 13D] ORMAT TECHNOLOGIES, INC.",
        url="https://example.com/13d", summary="", published_at=now,
        meta={"form_type": "SC 13D"},
    )
    scored = scorer.score(activist, [Link("ORA", "DIRECT", 0.97, "test")], now=now)
    assert scored.score > 25, "an activist stake IS material"


def test_earnings_dates_are_read_out_of_the_announcement(config, db):
    """LIMITATIONS gap #4 is "no official earnings calendar", priced at a paid
    feed. Every issuer publishes the date weeks ahead in plain English, and we
    were already collecting that release and throwing the date away."""
    from harel.pipeline import _earnings_date
    from harel.models import RawItem

    def item(title, summary=""):
        return RawItem(source="company_ir_rss", source_kind="rss", external_id=title,
                       title=title, url="", summary=summary,
                       published_at=datetime.now(timezone.utc))

    # %-d is not portable to Windows, so build the day number by hand.
    ahead = datetime.now(timezone.utc) + timedelta(days=20)
    when = f"{ahead:%B} {ahead.day}, {ahead.year}"

    got = _earnings_date(item(
        "Tower Semiconductor Announces Second Quarter 2026 Financial Results",
        f"Tower Semiconductor will issue its second quarter earnings release on {when}."))
    assert got and got[0] == ahead.date().isoformat(), got
    assert "Q2" in got[1]

    # Must not fire on the results themselves, nor on a conference appearance.
    assert _earnings_date(item(
        "Teva Delivers Strong Q2 Results and Raises Outlook",
        "Revenues of $4.2 billion in the second quarter.")) is None
    assert _earnings_date(item(
        f"Compugen to Participate in BTIG Biotechnology Conference on {when}")) is None
    # A date in the past is a report, not a schedule.
    assert _earnings_date(item(
        "Company Announces First Quarter 2020 Results Conference Call",
        "will host a call on May 4, 2020.")) is None


def test_a_sector_date_is_not_the_companys_next_catalyst(ran, db):
    """An airworthiness directive taking effect for Textron Aviation is a real
    date and a real sector link, but it was offered as TAT Technologies' "next
    known date", which is a different and false claim."""
    from harel.models import CalendarEntry, PriceSnapshot

    _, views = ran
    now = datetime.now(timezone.utc)
    soon = (now + timedelta(days=6)).date().isoformat()
    later = (now + timedelta(days=9)).date().isoformat()
    db.save_price(PriceSnapshot(ticker="ITA", asof=now, last=100.0, prev_close=99.2,
                                change_pct=0.8, session="regular", provider="yahoo"))
    db.save_price(PriceSnapshot(ticker="TATT", asof=now, last=100.0, prev_close=103.3,
                                change_pct=-3.2, session="regular", provider="yahoo"))
    db.save_calendar([CalendarEntry(
        ticker="TATT", kind="rule_effective", date=soon,
        label="Rule effective: Airworthiness Directives; Textron Aviation",
        source="federal_register", confidence=0.9, relation="SECTOR_REG")])
    db.conn.commit()

    alert = next(a for a in views.whats_moving(1.0)["unexplained"]
                 if a["ticker"] == "TATT")
    assert alert["next_catalyst"]["strength"] == "weak"
    assert "not this company" in alert["next_catalyst"]["caveat"]

    # A company date outranks it the moment one exists.
    db.save_calendar([CalendarEntry(
        ticker="TATT", kind="earnings", date=later, label="Q2 results",
        source="company_ir_rss", confidence=0.95, relation="DIRECT")])
    db.conn.commit()
    alert = next(a for a in views.whats_moving(1.0)["unexplained"]
                 if a["ticker"] == "TATT")
    assert alert["next_catalyst"]["strength"] == "company"
    assert alert["next_catalyst"]["date"] == later


def test_regional_mirrors_of_one_story_are_one_story():
    """Google News appends " - <publisher>", and the same wire copy is
    syndicated to regional mirrors, so one article arrived twice and counted as
    two independent sources."""
    from harel.dedupe import normalize_title

    base = "NRG Energy Gears Up to Report Q2 Earnings: Here's What to Expect"
    assert (normalize_title(f"{base} - Yahoo Finance")
            == normalize_title(f"{base} - Yahoo Finance Singapore"))
    assert (normalize_title("Teva Announces Q2 Results - Reuters")
            != normalize_title("Compugen Announces Phase 2 Data - Reuters"))


def test_a_placeholder_headline_is_a_parser_fault_not_a_story():
    """BrainsWay's feed emitted an entry titled literally "Title". It linked to
    BWAY, scored 32, and sat in the feed as company news."""
    from harel.collect.rss import _is_placeholder_title

    for junk in ("Title", "title", "(No Title)", "Untitled", "", "  ", "news"):
        assert _is_placeholder_title(junk), junk
    for real in ("Teva Announces Q2 Results", "BrainsWay to Report Q2 Financials"):
        assert not _is_placeholder_title(real), real


def test_an_undated_item_reports_discovery_time_as_discovery_time(ran, db):
    """Showing an invented timestamp as a publication time is how an evergreen
    marketing page reads as six-hour-old news."""
    from harel.serve.terminal import _time_cell

    _, views = ran
    now = datetime.now(timezone.utc)
    db.conn.execute(
        "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, title, "
        "url, published_at, collected_at, score, tier, meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("nodate", "company_ir_rss", "rss", "nodate", "Connecting Millions in Mexico",
         "https://example.com/c", now.isoformat(), now.isoformat(), 8.0, "NOISE",
         '{"undated": true}'),
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, why, "
        "score) VALUES (?,?,?,?,?,?)", ("nodate", "GILT", "DIRECT", 0.95, "t", 8.0))
    db.conn.commit()

    item = next(i for i in views.feed(min_score=0, hours=LOOKBACK, limit=200)["items"]
                if i["uid"] == "nodate")
    assert item["published_unknown"] is True
    assert item["discovered_at"]
    assert "date unknown" in _time_cell(item)

    explained = views.explain("nodate")
    assert explained["when"]["published_utc"] is None
    assert "unknown" in explained["when"]["publication_date"]


def test_a_holding_companys_peers_read_across_at_a_discount(config):
    """NRG, Vistra and Talen are peers of OPC Energy and CPV - the assets - not
    of Kenon, whose price also carries holding-company discount and disposals."""
    ken = (config.scoring.overrides.get("KEN") or {}).get("relation_overrides") or {}
    assert ken.get("PEER", 1.0) < config.scoring.relations["PEER"]


def test_benchmarks_are_the_actual_sector(config):
    """QQQ is the whole Nasdaq: a PANW move measured against it reads as
    stock-specific when it is the security group moving."""
    assert config.benchmark_for("cybersecurity_platform") == "CIBR"
    assert config.benchmark_for("semiconductors_foundry") == "SOXX"


def test_rescore_purges_a_retired_feed_but_never_a_search_result(config, db):
    """The purge exists because retiring a feed in config did nothing to what it
    had already collected - Gilat's marketing stayed in the ranked feed at trust
    1.0. But Google News items carry their SEARCH URL as meta.feed, which is in
    no static list, so a naive "not configured => retired" rule reads every
    aggregator item as orphaned. It did, and deleted 978 rows."""
    now = datetime.now(timezone.utc)

    def add(uid, source, feed, title="A real headline about Teva"):
        db.conn.execute(
            "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, "
            "title, url, published_at, collected_at, score, tier, meta_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, source, "rss", uid, title, "https://example.com/x",
             now.isoformat(), now.isoformat(), 30.0, "NORMAL",
             json.dumps({"feed": feed})),
        )
        db.conn.execute(
            "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, "
            "why, score) VALUES (?,?,?,?,?,?)", (uid, "TEVA", "DIRECT", 0.9, "t", 30.0))

    live_ir = config.ticker("TEVA").ir_feeds[0]
    add("keep_ir", "company_ir_rss", live_ir)
    add("drop_ir", "company_ir_rss", "https://www.gilat.com/feed/")
    add("keep_query", "google_news",
        "https://news.google.com/rss/search?q=%22Teva%22&hl=en-US")
    add("drop_junk", "company_ir_rss", live_ir, title="Title")
    db.conn.commit()

    Pipeline(config=config, db=db, lookback_hours=48).rescore(since_hours=48)
    alive = {r["uid"] for r in db.conn.execute("SELECT uid FROM items").fetchall()}
    assert "keep_ir" in alive
    assert "keep_query" in alive, "a search result must never be read as orphaned"
    assert "drop_ir" not in alive, "a retired feed's items must not stand"
    assert "drop_junk" not in alive


def test_rescore_never_deletes_an_item_it_merely_failed_to_relink(config, db):
    """Re-linking is a re-derivation from LESS information than the collector
    had: the seed ("I polled TEVA's CIK", "this came off the rival query") is
    not in the text. Treating "no links now" as "should not exist" deleted 185
    items whose only tie to a ticker was the query that found them."""
    now = datetime.now(timezone.utc)
    db.conn.execute(
        "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, title, "
        "url, published_at, collected_at, score, tier, meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("seeded", "google_news", "rss", "seeded",
         "A headline that never names the company", "https://example.com/s",
         now.isoformat(), now.isoformat(), 25.0, "NORMAL",
         json.dumps({"feed": "https://news.google.com/rss/search?q=x",
                     "seed_tickers": ["TEVA"], "seed_relation": "DIRECT"})),
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, why, "
        "score) VALUES (?,?,?,?,?,?)", ("seeded", "TEVA", "DIRECT", 0.92, "t", 25.0))
    db.conn.commit()

    # An issuer feed is authoritative even when a post never spells the name
    # out, so its seed must survive re-linking.
    db.conn.execute(
        "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, title, "
        "url, published_at, collected_at, score, tier, meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("from_issuer", "company_ir_rss", "rss", "from_issuer",
         "A headline that never names the company", "https://example.com/i",
         now.isoformat(), now.isoformat(), 25.0, "NORMAL",
         json.dumps({"feed": config.ticker("TEVA").ir_feeds[0],
                     "seed_tickers": ["TEVA"], "seed_relation": "DIRECT"})),
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, why, "
        "score) VALUES (?,?,?,?,?,?)",
        ("from_issuer", "TEVA", "DIRECT", 0.92, "t", 25.0))
    db.conn.commit()

    Pipeline(config=config, db=db, lookback_hours=48).rescore(since_hours=48)

    assert db.conn.execute("SELECT uid FROM items WHERE uid='seeded'").fetchone(), \
        "rescore deleted an item it simply could not re-link"
    # The row stays; the unsupported CLAIM does not. A per-ticker search seeds
    # DIRECT on the strength of the query alone, which is how "PH, US allot P42b
    # for anti-TB drive" became news about Allot Communications.
    assert db.tickers_for("seeded") == [], "an unevidenced search seed must be withdrawn"
    assert [l["ticker"] for l in db.tickers_for("from_issuer")] == ["TEVA"], \
        "an issuer feed's own seed is authoritative and must survive"


def test_a_quiet_tape_never_renders_an_empty_feed_panel(ran, db):
    """"Nothing above score 20" reads as a threshold artefact and leaves the
    trader unable to tell a quiet tape from a broken pipeline - which is the one
    distinction this page exists to make. On a Friday after the close, with
    every recap correctly capped in the teens, the top of the basket genuinely
    sits below 20."""
    from harel.serve.terminal import render_terminal

    _, views = ran
    now = datetime.now(timezone.utc)
    db.conn.execute("UPDATE items SET score = 12.0, tier = 'NOISE'")
    db.conn.execute("UPDATE item_tickers SET score = 12.0")
    db.conn.execute("UPDATE items SET published_at = ?", (now.isoformat(),))
    db.conn.commit()

    assert not views.feed(min_score=20, hours=24, limit=60)["items"]
    page = render_terminal(views)
    assert "quiet tape, not a blind one" in page
    assert "Feed (below threshold)" in page
    assert "items were collected and linked" in page
