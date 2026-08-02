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
    # Same bars and the same +10% close as stooq_teva.csv, in Yahoo's shape.
    # The end-to-end test has to run the price path that production runs, and
    # prices_stooq is switched off: stooq answers the CSV endpoint with a
    # JavaScript browser challenge, so Yahoo is the only live price source.
    "query1.finance.yahoo.com": fixture_json("yahoo_teva.json"),
    "ir.cgen.com": fixture_text("ir_feed.xml"),
}

SOURCES = [
    "sec_edgar_submissions", "sec_edgar_full_text", "federal_register",
    "clinicaltrials", "fda_enforcement", "maya_tase", "prices_yahoo",
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
    # maya_schedule, not courtlistener: courtlistener has no collector at all,
    # so naming it as merely key-less was the panel describing the wrong fault.
    assert "TASE_API_KEY" in warnings, "sources off for a missing key must be named"
    assert "courtlistener" in warnings, "a source with no collector must still be declared"


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
    assert any(k["env_var"] == "TASE_API_KEY" for k in health["missing_api_keys"])
    assert any(k["env_var"] == "TASE_API_KEY" for k in health["running_degraded"])
    # courtlistener requires a token AND has no collector. It belongs in the
    # second list only: an API key cannot switch on code that does not exist.
    assert not any(k["source"] == "courtlistener" for k in health["missing_api_keys"])
    assert any(k["source"] == "courtlistener"
               for k in health["sources_without_a_collector"])


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
    assert "טרמינל הראל" in html
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


def test_an_error_a_collector_recorded_survives_the_pass_that_recorded_it(
        config, db, monkeypatch):
    """Quiet is a success; quiet-after-recording-a-fault is not. maya answers
    200, finds nothing it can parse, writes save_state(last_error="0 parseable
    records") and returns without raising - and the pipeline, seeing a generator
    that completed, wrote a fresh last_ok_at and a null error straight over the
    top. A MAYA schema break therefore read as healthy-and-quiet in `harel
    doctor`, which is the one thing source_state exists to prevent.

    Driven through openFDA rather than maya so it tests the pipeline's rule and
    not one collector's wording."""
    from harel.collect.fda import OpenFdaCollector

    def silently_broken(self):
        self.save_state(last_error="0 parseable records - the schema changed")
        return iter(())

    monkeypatch.setattr(OpenFdaCollector, "collect", silently_broken)
    Pipeline(config=config, db=db, lookback_hours=LOOKBACK,
             client=FakeHttpClient({})).run(only=["fda_enforcement"])

    state = db.get_source_state("fda_enforcement")
    assert state.get("last_error"), "the collector's own record of failure was erased"
    assert not state.get("last_ok_at"), "a pass that recorded a fault is not a success"
    assert state.get("consecutive_failures") == 1

    degraded = {s["source"] for s in
                Views(db=db, config=config).health()["degraded_sources"]}
    assert "fda_enforcement" in degraded, "a silent failure must reach `harel doctor`"


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
    assert "תבדוק בעצמך" in page
    assert "איך נבנה הציון" in page
    assert "news.google.com/search" in page, "no outside verification link"

    # Every headline the terminal prints has to reach its own evidence. The
    # fixture corpus is older than the terminal's own 24h window, so render the
    # row builder against the items directly rather than the whole page.
    from harel.serve.terminal import _section

    rows = _section("Feed", views.feed(min_score=0, hours=LOOKBACK, limit=5)["items"])
    assert f"/item/{uid}" in rows, "the feed must link to the evidence"
    assert render_terminal(views).startswith("<!doctype html>")

    assert "כל המקורות" in render_sources(views)


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


def test_a_september_date_is_extracted_like_any_other_month():
    """The month pattern was built as `sep(?:tember)?\\.?`, so against "Sept. 5"
    it matched "Sep", failed "tember" on "t. 5", matched `\\.?` empty and then
    handed the `\\s+` a "t" - no match at all. Every AP-dated Q3 announcement
    lost its date silently, and September is a quarter end. Exactly the hole
    "Aug. 5" fell through, one month later."""
    from harel.pipeline import _DATE_MDY, _DATE_DMY, _earnings_date, _month_index
    from harel.models import RawItem

    # Written out rather than derived from the pattern, which is the thing on
    # trial. Index 1 is the AP abbreviation - four letters for September only.
    forms = {
        1: ("January", "Jan."), 2: ("February", "Feb."), 3: ("March", "Mar."),
        4: ("April", "Apr."), 5: ("May",), 6: ("June", "Jun."),
        7: ("July", "Jul."), 8: ("August", "Aug."),
        9: ("September", "Sept.", "Sept", "Sep."),
        10: ("October", "Oct."), 11: ("November", "Nov."), 12: ("December", "Dec."),
    }
    for month, spellings in forms.items():
        for spelling in spellings:
            for text in (f"on {spelling} 5, 2026", f"on {spelling} 5"):
                match = _DATE_MDY.search(text)
                assert match, f"{text!r} did not parse at all"
                assert _month_index(match.group(1)) == month, text
            match = _DATE_DMY.search(f"on 5 {spelling} 2026")
            assert match and _month_index(match.group(2)) == month, spelling

    # And through the whole extractor, in whatever month is three weeks out.
    ahead = datetime.now(timezone.utc) + timedelta(days=20)
    spellings = forms[ahead.month]
    ap = spellings[1] if len(spellings) > 1 else spellings[0]
    got = _earnings_date(RawItem(
        source="company_ir_rss", source_kind="rss", external_id="q3",
        title="Gilat Schedules Third Quarter 2026 Results Conference Call", url="",
        summary=f"will host a conference call on {ap} {ahead.day}, {ahead.year}.",
        published_at=datetime.now(timezone.utc)))
    assert got and got[0] == ahead.date().isoformat(), got


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
    assert "תאריך לא ידוע" in _time_cell(item)

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
    assert "סל שקט, לא מערכת עיוורת" in page
    assert "פיד (מתחת לסף)" in page
    assert "פריטים נאספו וקושרו בחלון הזה" in page


# --------------------------------------------------------------------------- #
# Hebrew / RTL. The page direction is right-to-left but most of its content -
# symbols, prices, timestamps, source keys, half the headlines - is not, and an
# unmarked "+4.8%" renders as "%4.8+" inside an RTL block.
# --------------------------------------------------------------------------- #
def test_every_page_declares_hebrew_and_rtl(ran, db):
    from harel.serve.terminal import render_item, render_sources, render_terminal

    _, views = ran
    uid = _any_uid(views)
    for page in (render_terminal(views), render_sources(views),
                 render_item(views, uid)):
        assert "<html lang='he' dir='rtl'>" in page
        assert "charset='utf-8'" in page


def test_the_stylesheet_mirrors_instead_of_hard_coding_a_side(ran):
    """Physical properties do not flip with the page: a border-left accent
    lands on the wrong edge in RTL, and padding-right pushes text away from the
    margin it should hug."""
    from harel.serve.terminal import CSS

    for physical in ("border-left:", "border-right:", "margin-left:",
                     "margin-right:", "padding-left:", "padding-right:"):
        assert physical not in CSS.replace(" ", ""), physical
    assert "border-inline-start" in CSS
    assert "margin-inline-end" in CSS
    assert "text-align: start" in CSS


def test_numbers_and_symbols_are_isolated_from_the_bidi_algorithm(ran, db):
    """A percentage, a ticker and a timestamp are Latin runs inside Hebrew
    paragraphs. Without an isolate they reorder against neighbouring text."""
    from harel.models import PriceSnapshot
    from harel.serve.terminal import _ltr, _pct, render_terminal

    _, views = ran
    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="SOXX", asof=now, last=100.0, prev_close=99.0,
                                change_pct=1.0, session="regular", provider="yahoo"))
    db.save_price(PriceSnapshot(ticker="TSEM", asof=now, last=100.0, prev_close=94.0,
                                change_pct=6.4, volume_multiple=0.8,
                                session="regular", provider="yahoo"))
    db.conn.commit()

    assert _pct(4.8) == "<span class='ltr'>+4.8%</span>"
    assert _ltr("TSEM") == "<span class='ltr'>TSEM</span>"

    page = render_terminal(views)
    assert "class='ltr'" in page
    # The isolate has to be real CSS, not just a class name.
    assert "unicode-bidi: isolate" in page
    # Headlines can be either language, so the browser decides per string.
    assert "dir='auto'" in page


def test_the_hebrew_glossaries_cover_everything_the_linker_emits():
    """A relation with no Hebrew label reaches the screen as a bare English
    token, which is exactly the drift a second language invites."""
    from harel.enrich.linker import RELATION_RANK
    from harel.views import (RELATION_LABEL_HE, RELATION_MEANING_HE,
                             RELATION_MEANING)

    assert set(RELATION_MEANING) == set(RELATION_MEANING_HE)
    assert set(RELATION_RANK) <= set(RELATION_LABEL_HE), (
        set(RELATION_RANK) - set(RELATION_LABEL_HE))


def test_english_pipeline_strings_are_spoken_hebrew_on_screen():
    """`why` and the scoring trace arrive in English from the linker and the
    scorer. Both are generated from a small set of stable shapes, so they are
    rewritten rather than left as English islands in a Hebrew page."""
    from harel.serve import hebrew as he

    rendered, hit = he.why('names "Teva" in headline')
    assert hit and rendered == 'מזכיר "Teva" בכותרת'

    rendered, hit = he.why('competitor "Viatris" in body')
    assert hit and "מתחרה" in rendered and "בגוף הידיעה" in rendered

    # Multi-part reasons are joined with "; " and rewritten part by part.
    rendered, hit = he.why('found by our "TEVA news" search; names "Teva" in headline')
    assert hit and rendered.count(";") == 1

    rendered, hit = he.trace_step("source trust x0.60 (google_news)")
    assert hit and rendered == "אמון המקור ×0.60 (google_news)"

    # This one contains an arrow of its own; splitting on it first broke every
    # recency step in the corpus.
    rendered, hit = he.trace_step("age 81.1h -> recency x0.25")
    assert hit and "דעיכת זמן" in rendered

    # An unknown shape must survive legibly rather than be mangled.
    rendered, hit = he.why("something we have never seen")
    assert not hit and rendered == "something we have never seen"


def test_relative_time_reads_as_hebrew(ran):
    from harel.serve import hebrew as he

    now = datetime.now(timezone.utc)
    assert he.ago((now - timedelta(minutes=25)).isoformat()) == "לפני 25 דק׳"
    assert he.ago((now - timedelta(hours=1)).isoformat()) == "לפני שעה"
    assert he.ago((now - timedelta(hours=5)).isoformat()) == "לפני 5 שעות"
    assert he.ago((now - timedelta(days=1)).isoformat()) == "אתמול"
    assert he.ago((now - timedelta(days=4)).isoformat()) == "לפני 4 ימים"


def test_coverage_warnings_are_composed_in_hebrew_not_translated(ran):
    """The English sentences and the Hebrew ones are both built from the same
    structured entries, so neither can drift into describing the other's facts."""
    from harel.serve.terminal import _coverage_warning_he

    _, views = ran
    entries = views.coverage_warning_entries()
    assert entries, "the shipped config must produce at least one warning"
    assert len(entries) == len(views._coverage_warnings())
    for entry in entries:
        rendered = _coverage_warning_he(entry)
        # Composed prose, not a dict falling through to the page. (The word
        # "kind" itself appears legitimately: one config note reads
        # "no collector is implemented for kind=finnhub yet".)
        assert rendered and "'kind':" not in rendered
        # The source key stays Latin and isolated; the prose around it is Hebrew.
        if entry.get("source"):
            assert entry["source"] in rendered


def test_a_latin_unit_never_sits_outside_its_isolate(ran, db):
    """An isolate protects what is inside it. "NORMAL {island} · HIGH {island}"
    left the labels outside, so the bidi algorithm merged them with their
    neighbours and reordered the run into "75 NORMAL 35 · HIGH 55 · ALERT".
    The unit belongs inside the island with its number."""
    from harel.serve.terminal import render_item

    _, views = ran
    page = render_item(views, _any_uid(views))

    # The whole tier scale is one run.
    assert "NORMAL 35 · HIGH 55 · ALERT 75" in page
    # A timestamp keeps its zone; a close keeps its "ET".
    assert "UTC</span>" in page
    assert "ET</span>" in page
    # ...and none of them is left dangling after a closing isolate.
    for orphan in ("</span> UTC", "</span> ET", "</span> pp"):
        assert orphan not in page, orphan


# ------------------------------------------------- timestamps and causation -- #
def test_a_date_in_the_future_is_never_rendered_as_just_now():
    """`he.ago` only tested `minutes < 1`, and -2880 is also less than one. So a
    Federal Register document scheduled to publish on Monday was presented on
    Saturday night as having come out this instant - the single most misleading
    thing a tape can say."""
    from datetime import datetime, timedelta, timezone

    from harel.serve import hebrew as he

    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    rendered = he.ago(soon)
    assert "הרגע" not in rendered
    # "מחר" or "בעוד N ימים" - either says schedule; none of them says age.
    assert any(word in rendered for word in ("בעוד", "מחר")), rendered
    # And the past still reads as the past.
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    assert "לפני" in he.ago(past)


def test_a_publication_date_stops_being_forthcoming_when_it_arrives(config, db):
    """`forthcoming` was decided once, in the collector, and nothing ever
    cleared it. A public-inspection copy leaves the PI feed the moment it
    publishes and is never collected again, so its meta kept saying "not out
    yet" for ever and the terminal went on announcing "מתפרסם 31.7" days into
    August - a scheduled date, in the past, presented as news to come."""
    now = datetime.now(timezone.utc)

    def add(uid, scheduled, hours_ago):
        filed = (now - timedelta(hours=hours_ago)).isoformat()
        db.conn.execute(
            "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, "
            "title, url, published_at, collected_at, score, tier, meta_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, "federal_register", "federal_register", uid,
             f"[FR-EARLY] Airworthiness Directives ({uid})",
             "https://example.invalid/fr", filed, filed, 30.0, "NORMAL",
             # Exactly what the collector stored on the day it was filed.
             json.dumps({"forthcoming": True, "public_inspection": True,
                         "scheduled_publication_date": scheduled})))
        db.conn.execute(
            "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, "
            "why, score) VALUES (?,?,?,?,?,?)",
            (uid, "TATT", "SECTOR_REG", 0.6, "t", 30.0))

    add("published", (now - timedelta(days=4)).date().isoformat(), hours_ago=150)
    add("pending", (now + timedelta(days=3)).date().isoformat(), hours_ago=5)
    db.conn.commit()

    feed = Views(db=db, config=config).feed(min_score=0, hours=400, limit=50)
    items = {i["uid"]: i for i in feed["items"]}
    assert not items["published"].get("forthcoming"), \
        "a date that has arrived is not a date to come, whatever meta was told"
    assert "publishes_on" not in items["published"]
    assert items["pending"]["forthcoming"] is True, \
        "lead time is the whole reason the early copy is collected"
    assert items["pending"]["publishes_on"] and items["pending"]["discovered_at"]


def test_a_quote_separates_when_it_printed_from_when_we_fetched_it(config, db):
    """On a Saturday, Friday's closing print was labelled "בן 2 דק׳" - the age
    of our HTTP request wearing the price's name."""
    from datetime import datetime, timedelta, timezone

    from harel.models import PriceSnapshot
    from harel.views import Views

    now = datetime.now(timezone.utc)
    close = now - timedelta(days=2)
    db.save_price(PriceSnapshot(
        ticker="TEVA", asof=now, market_time=close, last=35.01, prev_close=35.41,
        change_pct=-1.13, session="closed", provider="yahoo",
    ))
    quote = Views(db=db, config=config).quote("TEVA")

    assert quote["fetch_age_minutes"] < 5
    assert quote["market_age_minutes"] > 60 * 24
    assert quote["market_time"].startswith(close.isoformat()[:10])
    assert "market closed" in quote["freshness"]
    assert close.date().isoformat() in quote["freshness"]


def test_a_sector_keyword_match_is_context_and_never_a_driver(config, db):
    """The same UFLPA entity-list notice was offered as the reason TSEM rose
    4.0% and as the reason CAMT fell 3.5%, in one session, with SOXX flat. A
    document that explains a move and its opposite explains neither."""
    from datetime import datetime, timedelta, timezone

    from harel.models import Link, PriceSnapshot, RawItem, ScoredItem
    from harel.views import Views

    now = datetime.now(timezone.utc)
    for ticker, pct in (("TSEM", 4.0), ("CAMT", -3.5), ("SOXX", 0.1)):
        db.save_price(PriceSnapshot(
            ticker=ticker, asof=now, market_time=now, last=100.0, prev_close=100.0,
            change_pct=pct, session="regular", provider="yahoo"))

    item = RawItem(
        source="federal_register", source_kind="federal_register",
        external_id="fr:2026-15628",
        title="[FR] Notice Regarding the Uyghur Forced Labor Prevention Act Entity List",
        url="https://example.invalid/uflpa", summary="", body="",
        published_at=now - timedelta(hours=6),
        meta={"document_number": "2026-15628"},
    )
    links = [Link("TSEM", "SECTOR_REG", 0.62, 'mentions "entity list"'),
             Link("CAMT", "SECTOR_REG", 0.62, 'mentions "entity list"')]
    db.upsert_item(
        ScoredItem(raw=item, links=links, events=[], score=33.0, tier="normal",
                   reasons=[], per_ticker_score={"TSEM": 33.0, "CAMT": 33.0}),
        "k-uflpa", "c-uflpa")

    moving = Views(db=db, config=config).whats_moving(min_abs_pct=2.0)
    by_ticker = {m["ticker"]: m for m in moving["movers"]}

    for ticker in ("TSEM", "CAMT"):
        mover = by_ticker[ticker]
        assert not mover["drivers"], f"{ticker}: a keyword match is not a cause"
        assert mover["explained"] is False
        titles = [c["title"] for c in mover["possible_context"]]
        assert any("Uyghur" in t for t in titles), \
            f"{ticker}: held back as context, but the reader must still see it"
        assert all(c["causal_eligible"] is False for c in mover["possible_context"])

    raised = {a["ticker"] for a in moving["unexplained"]}
    assert {"TSEM", "CAMT"} <= raised, "both moves are still open questions"


# What lifts a sector-level match from context to cause is the basket moving
# together. No sector in the shipped universe holds more than two names, so
# `_sector_move` sees at most one peer and the rule below can never fire on real
# config - but it is still the rule that decides whether a sector-wide document
# may be called a cause, so these two run against a four-name sector.
PEER_BLOCK = """
  ZZBIOA:
    name: "Synthetic biotech peer A"
    sector: biotech_clinical
    float_class: micro
  ZZBIOB:
    name: "Synthetic biotech peer B"
    sector: biotech_clinical
    float_class: micro
  ZZBIOC:
    name: "Synthetic biotech peer C"
    sector: biotech_clinical
    float_class: micro
"""


@pytest.fixture
def config_with_a_full_sector(tmp_path):
    import shutil

    from conftest import REPO_ROOT
    from harel.config import load_config

    cdir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", cdir)
    universe = cdir / "universe.yaml"
    universe.write_text(universe.read_text(encoding="utf-8") + PEER_BLOCK,
                        encoding="utf-8")
    return load_config(cdir)


def _move(db, ticker, pct):
    from harel.models import PriceSnapshot

    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker=ticker, asof=now, market_time=now, last=100.0,
                                prev_close=100.0, change_pct=pct, session="closed",
                                provider="yahoo"))


def test_the_sector_median_is_the_basket_middle_not_its_upper_half(
        config_with_a_full_sector, db):
    """`sorted(moves)[len(moves) // 2]` takes the upper-middle element, so an
    even-numbered basket reads high. It gates whether a sector-wide document may
    be promoted to a cause: peers at 0.1 / 0.5 / 1.2 / 2.0 cleared the 1.0pp
    "the group really moved" bar at 1.2 when the middle of the group is 0.85."""
    views = Views(db=db, config=config_with_a_full_sector)
    for ticker, pct in (("CGEN", 0.1), ("ZZBIOA", 0.5),
                        ("ZZBIOB", 1.2), ("ZZBIOC", 2.0)):
        _move(db, ticker, pct)
    _move(db, "ORMP", 3.0)
    db.conn.commit()

    sector = views._sector_move("ORMP")
    assert sector["names"] == 4
    assert sector["median_pct"] == pytest.approx(0.85)
    assert views._sector_wide_corroboration(db.latest_price("ORMP"), sector) is False, \
        "a basket whose middle moved 0.85% is not a sector event"


def test_a_sector_document_after_the_bell_is_never_promoted_to_a_driver(
        config_with_a_full_sector, db, monkeypatch):
    """`_driver_cutoff` bound the candidate lists but not sector context - and
    sector context is promoted into `drivers` the moment the basket corroborates
    it. So a notice filed at 16:13 could be offered as the cause of a move that
    had finished at 16:00, which is the single thing the bell test exists to
    prevent."""
    now = datetime.now(timezone.utc)
    bell = now - timedelta(hours=3)
    # The bell has its own test; pinning it here keeps this one off the calendar.
    monkeypatch.setattr("harel.views.last_session_close", lambda when=None: bell)
    views = Views(db=db, config=config_with_a_full_sector)

    for ticker, pct in (("CGEN", 2.0), ("ZZBIOA", 2.2),
                        ("ZZBIOB", 1.8), ("ZZBIOC", 2.5)):
        _move(db, ticker, pct)
    _move(db, "ORMP", 3.0)

    def add(uid, title, published):
        # Below the driver threshold on purpose: demoting an item from cause to
        # context is what lowers its score, so this is the shape a sector
        # document actually arrives in.
        db.conn.execute(
            "INSERT OR REPLACE INTO items (uid, source, source_kind, external_id, "
            "title, url, published_at, collected_at, score, tier, meta_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, "federal_register", "federal_register", uid, title,
             "https://example.invalid/fr", published.isoformat(), now.isoformat(),
             12.0, "NOISE", json.dumps({"document_number": uid})))
        db.conn.execute(
            "INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, "
            "why, score) VALUES (?,?,?,?,?,?)",
            (uid, "ORMP", "SECTOR_REG", 0.62, 'mentions "insulin"', 12.0))

    add("early", "[FR] Notice on insulin biosimilars, on public inspection 09:40",
        bell - timedelta(hours=2))
    add("late", "[FR] Notice on insulin biosimilars, filed at 16:13",
        bell + timedelta(minutes=13))
    db.conn.commit()

    mover = next(m for m in views.whats_moving(min_abs_pct=1.0)["movers"]
                 if m["ticker"] == "ORMP")
    drivers = [d["title"] for d in mover["drivers"]]
    context = [c["title"] for c in mover["possible_context"]]

    assert any("09:40" in t for t in drivers), \
        "a corroborated sector document from before the bell is still promotable"
    assert not any("16:13" in t for t in drivers), \
        "the move was over before this document existed"
    assert any("16:13" in t for t in context), \
        "hiding it is the same failure reversed - the reader must see what we read"


def test_one_document_filed_early_and_published_later_is_one_event(config, db):
    """Public inspection and publication are two copies of one document under
    one number. Left unmerged they were two stories, and the later copy - which
    scores higher for being fresher - answered the bell test for both, putting a
    document readable on Friday morning "after the bell" on Friday night."""
    from datetime import datetime, timedelta, timezone

    from harel.dedupe import dedupe_key
    from harel.models import RawItem

    now = datetime.now(timezone.utc)
    common = dict(source_kind="federal_register", url="", summary="", body="")
    early = RawItem(
        source="federal_register_public_inspection", external_id="pi:2026-15628",
        title="[FR-EARLY] Uyghur Forced Labor Prevention Act Entity List",
        published_at=now - timedelta(days=2),
        meta={"dedupe_id": "federal_register:2026-15628"}, **common)
    late = RawItem(
        source="federal_register", external_id="fr:2026-15628",
        title="[FR] Notice Regarding the Uyghur Forced Labor Prevention Act Entity List",
        published_at=now,
        meta={"dedupe_id": "federal_register:2026-15628"}, **common)

    assert dedupe_key(early) == dedupe_key(late), \
        "one document number is one event, whatever the title prefix says"

    # And a document with no stable id still falls back to title-and-day.
    plain = RawItem(source="rss", external_id="x", title="Something else",
                    published_at=now, meta={}, **common)
    assert dedupe_key(plain) != dedupe_key(late)


def test_a_source_with_no_collector_says_so_instead_of_asking_for_a_key(db, config):
    """Two blindnesses that look identical on the panel and are not.

    A source can be declared in sources.yaml for a `kind` no collector
    implements. `build_collectors` looks the kind up in the registry, finds
    nothing, and skips it with a debug line - so the source counts as
    configured, polls nothing, and the only message about it was "set
    FCC_API_KEY". Supplying that key would have cleared the warning and
    collected exactly as much as before: nothing. `fcc_filings` and
    `courtlistener` were both sitting behind it.
    """
    from harel.collect.base import registered_kinds
    from harel.serve.terminal import _coverage_warning_he

    views = Views(db=db, config=config)
    entries = views.coverage_warning_entries()
    orphans = {e["source"]: e for e in entries if e["kind"] == "no_collector"}
    implemented = set(registered_kinds())

    # The rule, not today's list: every enabled source whose kind has no
    # collector must be named, and nothing else may be.
    expected = {s.key for s in views.config.sources.values()
                if s.enabled and s.kind not in implemented}
    assert set(orphans) == expected, "the panel must name exactly the unimplemented sources"

    # And such a source must never ALSO be reported as merely missing a key -
    # that is the message that sends someone off to get one.
    keyless = {e["source"] for e in entries if e["kind"] == "missing_key"}
    assert not (keyless & expected), \
        "a source with no collector was still asking for an API key"
    assert not ({e["source"] for e in views.health()["missing_api_keys"]} & expected)

    for entry in orphans.values():
        assert entry["source_kind"] not in implemented
        # Both languages compose from the entry rather than translating prose.
        assert "API" in _coverage_warning_he(entry)
    if orphans:
        english = " ".join(views._coverage_warnings())
        assert "no collector implements" in english


def test_a_move_can_qualify_on_its_sector_alone(config, db):
    """CGEN rose 1.9% on a day XBI fell 3.1% - a 5pp divergence from its own
    sector, on 1.4x volume, with nothing behind it. It never appeared, because
    1.9% did not clear the absolute gate and the relative move was therefore
    never computed at all. A name holding up while its sector sells off is the
    "what do they know?" question this panel exists to raise.
    """
    from harel.models import PriceSnapshot

    now = datetime.now(timezone.utc)
    # Small absolute move, large move against the sector, real volume.
    db.save_price(PriceSnapshot(ticker="CGEN", asof=now, last=2.35, prev_close=2.335,
                                change_pct=0.64, volume_multiple=1.42,
                                session="closed", provider="yahoo"))
    db.save_price(PriceSnapshot(ticker="XBI", asof=now, last=147.0, prev_close=151.5,
                                change_pct=-2.94, session="closed", provider="yahoo"))
    db.conn.commit()

    views = Views(db=db, config=config)
    # 2.5 is what morning_brief uses, and it is what hid this.
    movers = views.whats_moving(min_abs_pct=2.5)["movers"]
    cgen = next((m for m in movers if m["ticker"] == "CGEN"), None)

    assert cgen is not None, "a 3.6pp divergence from its sector must be examined"
    assert cgen["relative_pct"] == pytest.approx(3.58, abs=0.01)


def test_a_quiet_name_drifting_on_no_volume_is_not_a_signal(config, db):
    """The volume floor is what stops the relative path firing on a spread."""
    from harel.models import PriceSnapshot

    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="CGEN", asof=now, last=2.35, prev_close=2.335,
                                change_pct=0.64, volume_multiple=0.1,
                                session="closed", provider="yahoo"))
    db.save_price(PriceSnapshot(ticker="XBI", asof=now, last=147.0, prev_close=151.5,
                                change_pct=-2.94, session="closed", provider="yahoo"))
    db.conn.commit()

    movers = Views(db=db, config=config).whats_moving(min_abs_pct=2.5)["movers"]
    assert not any(m["ticker"] == "CGEN" for m in movers)


def test_a_peers_own_price_recap_is_not_commentary_on_our_move(ran):
    """"Palo Alto Networks Stock Price Up 1.9%" was offered under ALLT as
    post-move commentary, and a Neurocrine earnings reaction under TEVA. Both
    are real articles; neither describes the move on the row it sat in. Only a
    recap of THIS company's own move is the circular-reasoning case that list
    exists to name."""
    _, views = ran
    for mover in views.whats_moving(min_abs_pct=0.0)["movers"]:
        for item in mover.get("post_move_commentary") or []:
            assert (item.get("relation") or "") in ("DIRECT", "SUBSIDIARY"), (
                f"{item['title'][:60]!r} is a {item.get('relation')} story, "
                f"not commentary on {mover['ticker']}'s move")


def test_a_name_that_barely_moved_is_reported_as_decoupling_not_as_a_move(config, db):
    """CGEN rose 0.6% while XBI fell 2.9%. The alert is right, the wording was
    not: "תנועה חריגה" for a 0.6% stock reads as though it jumped, when the
    moving part was its sector leaving without it. Different question - not
    "what happened to it" but "why did it not follow" - so it says which."""
    from harel.models import PriceSnapshot
    from harel.serve.terminal import _unexplained_section

    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="CGEN", asof=now, last=2.35, prev_close=2.335,
                                change_pct=0.64, volume_multiple=1.42,
                                session="closed", provider="yahoo"))
    db.save_price(PriceSnapshot(ticker="XBI", asof=now, last=147.0, prev_close=151.5,
                                change_pct=-2.94, session="closed", provider="yahoo"))
    db.conn.commit()

    alerts = Views(db=db, config=config).whats_moving(min_abs_pct=2.5)["unexplained"]
    cgen = next(a for a in alerts if a["ticker"] == "CGEN")
    assert cgen["kind"] == "sector_decoupling"
    assert cgen["decoupled"] is True

    rendered = _unexplained_section([cgen])
    assert "התנתקות" in rendered
    assert "תנועה חריגה" not in rendered


def test_a_stock_that_really_moved_keeps_the_move_wording(config, db):
    from harel.models import PriceSnapshot
    from harel.serve.terminal import _unexplained_section

    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(ticker="TATT", asof=now, last=30.0, prev_close=31.4,
                                change_pct=-4.6, volume_multiple=1.5,
                                session="closed", provider="yahoo"))
    db.save_price(PriceSnapshot(ticker="ITA", asof=now, last=100.6, prev_close=100.0,
                                change_pct=0.6, session="closed", provider="yahoo"))
    db.conn.commit()

    alerts = Views(db=db, config=config).whats_moving(min_abs_pct=2.5)["unexplained"]
    tatt = next(a for a in alerts if a["ticker"] == "TATT")
    assert tatt["kind"] == "unexplained_relative_move"
    assert "תנועה חריגה" in _unexplained_section([tatt])


def test_the_closing_print_is_labelled_as_the_close_not_as_the_last_trade(config, db):
    """The movers board is computed from the session close, so calling it "the
    last trade" is false of the tape: the panel said "עסקה אחרונה 16:00 ET"
    while the real last print was 19:34. Both belong on screen, apart."""
    from harel.models import PriceSnapshot
    from harel.serve.terminal import _quote_label

    now = datetime.now(timezone.utc)
    db.save_price(PriceSnapshot(
        ticker="CGEN", asof=now, last=2.35, prev_close=2.335, change_pct=0.64,
        market_time=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
        extended_last=2.38, extended_change_pct=1.28,
        extended_time=datetime(2026, 7, 31, 23, 49, tzinfo=timezone.utc),
        session="closed", provider="yahoo"))
    db.conn.commit()

    label = _quote_label(Views(db=db, config=config).quote("CGEN"))
    assert "סגירת הסשן" in label
    assert "16:00 ET" in label
    # The real last print, on its own line, not blended into the reference price.
    assert "מסחר מאוחר" in label and "19:49 ET" in label


# --------------------------------------------------------------------------- #
# The calendar is only as good as the links behind it.
# --------------------------------------------------------------------------- #
def _calendar_rows(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM calendar")]


def _seed_calendar(db, *, ticker, url, kind="rule_effective", date="2027-06-01",
                   label="Rule effective: [FR] Something", source="federal_register",
                   relation="SECTOR_REG"):
    from harel.models import CalendarEntry

    db.save_calendar([CalendarEntry(
        ticker=ticker, kind=kind, date=date, label=label, source=source,
        confidence=0.9, url=url, relation=relation)])
    db.conn.commit()


def test_a_calendar_date_whose_link_was_withdrawn_is_removed(config, db):
    """A date is on the calendar only because some item linked some ticker. When
    a tightened rule withdraws that claim, `rescore` deletes the item_tickers row
    and moves on - and nothing ever removed the date it had produced. 63 of 116
    rows were orphans, and `_next_catalyst` will print one as a name's "next
    known date"."""
    _seed_calendar(db, ticker="NICE", url="https://example.gov/immigration-rule")
    assert len(_calendar_rows(db)) == 1

    # No item, therefore no link, therefore nothing justifies the date.
    assert db.purge_orphan_calendar() == 1
    assert _calendar_rows(db) == []


def test_a_calendar_date_whose_link_survives_is_kept(config, db):
    """The regression that matters. A purge that cannot tell a live date from a
    dead one is worse than no purge."""
    from datetime import datetime, timezone

    from harel.models import Link, RawItem, ScoredItem

    url = "https://example.gov/airworthiness-directive"
    raw = RawItem(source="federal_register", source_kind="federal_register",
                  external_id="fr:1", title="[FR] Airworthiness Directives", url=url,
                  published_at=datetime.now(timezone.utc))
    scored = ScoredItem(raw=raw, links=[Link(ticker="TATT", relation="SECTOR_REG",
                                             confidence=0.62, why="sector")],
                        events=[], score=30.0, per_ticker_score={"TATT": 30.0},
                        tier="NOISE", reasons=[])
    db.upsert_item(scored, "k1", "c1")
    _seed_calendar(db, ticker="TATT", url=url)
    db.conn.commit()

    assert db.purge_orphan_calendar() == 0
    assert len(_calendar_rows(db)) == 1


def test_an_issuer_announced_earnings_date_is_never_collateral(config, db):
    """There are zero earnings orphans today and that has to stay true - these
    are the dates the whole calendar exists for. The purge must key on whether
    the LINK survives, never on the kind, so this passes for the same reason a
    live regulatory date passes and not as a special case."""
    from datetime import datetime, timezone

    from harel.models import Link, RawItem, ScoredItem

    url = "https://ir.example.com/q2-results-date"
    raw = RawItem(source="company_ir_rss", source_kind="rss", external_id="ir:1",
                  title="Example to Report Second Quarter 2026 Results on August 4",
                  url=url, published_at=datetime.now(timezone.utc))
    scored = ScoredItem(raw=raw, links=[Link(ticker="TEVA", relation="DIRECT",
                                             confidence=0.95, why="names Teva")],
                        events=[], score=60.0, per_ticker_score={"TEVA": 60.0},
                        tier="HIGH", reasons=[])
    db.upsert_item(scored, "k2", "c2")
    _seed_calendar(db, ticker="TEVA", url=url, kind="earnings", date="2027-08-04",
                   label="Q2 results (company-announced date)",
                   source="company_ir_rss", relation="DIRECT")

    assert db.purge_orphan_calendar() == 0
    assert [r["kind"] for r in _calendar_rows(db)] == ["earnings"]


def test_the_sweep_reaches_dates_older_than_the_rescore_window(config, db):
    """Deliberately a whole-table sweep. The rows most likely to be stale are the
    oldest, which are exactly the ones an --hours window stops examining - so a
    per-rescored-item purge would miss them for ever."""
    from harel.pipeline import Pipeline

    # An orphan whose (nonexistent) item would be far outside any window.
    _seed_calendar(db, ticker="NICE", url="https://example.gov/ancient",
                   date="2031-01-01")
    result = Pipeline(config=config, db=db,
                      client=FakeHttpClient({})).rescore(since_hours=1.0)

    assert result["calendar_purged"] == 1
    assert _calendar_rows(db) == []
