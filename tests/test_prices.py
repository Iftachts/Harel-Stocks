"""The price collector: what the tape says, and when it said it."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeHttpClient, fixture_json
from harel.collect.base import CollectorContext
from harel.collect.prices import PriceCollector, current_session

ET = ZoneInfo("America/New_York")


def ctx(config, db, routes):
    return CollectorContext(config=config, client=FakeHttpClient(routes), db=db)


def collector(config, db, routes, source="prices_yahoo"):
    return PriceCollector(config.sources[source], ctx(config, db, routes))


def et_stamp(year, month, day, hour, minute) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=ET).timestamp())


# ------------------------------------------------------- pre / post market -- #
def test_an_after_hours_print_is_the_last_price_not_the_four_oclock_close(config, db):
    """The whole reason this source is configured: "gives pre/post-market
    prices, which matter a lot for an overnight-gap workflow".

    TEVA closes 19.80, reports after the bell and trades 22.50 after hours. The
    snapshot used to carry 19.80 and +10.0% under a session labelled
    "afterhours" - the entire after-hours move invisible, and `market_time`
    claiming the 16:00 print. The code looked for `preMarketPrice` /
    `postMarketPrice` in `meta`, and the v8 chart endpoint has neither key; the
    extended-hours prints are bars in `indicators.quote[0].close`, stamped
    after `meta.regularMarketTime`.
    """
    payload = fixture_json("yahoo_teva_afterhours.json")
    snap = collector(config, db, {"interval=5m": payload})._yahoo_snapshot("TEVA")

    # The after-hours print is captured - but beside the session, not blended
    # into it. Folding it into `change_pct` made a finished session keep moving:
    # SOXX read +0.07% at the bell and -0.77% once a thin post-market print
    # landed, and a name's "relative to sector" then compared its own
    # post-market drift against an ETF's.
    assert snap.extended_last == pytest.approx(22.50)
    assert snap.extended_change_pct == pytest.approx(13.64, abs=0.1)
    assert snap.extended_time.astimezone(ET).strftime("%H:%M") == "16:55"

    # The session itself is the close against the prior close, and nothing else.
    assert snap.last == pytest.approx(19.80)
    assert snap.change_pct == pytest.approx(10.0, abs=0.1)


def test_a_quote_with_no_extended_hours_bars_keeps_the_regular_close(config, db):
    """No after-hours print means the closing print is still the last price."""
    payload = fixture_json("yahoo_teva.json")
    snap = collector(config, db, {"interval=5m": payload})._yahoo_snapshot("TEVA")

    assert snap.last == pytest.approx(19.80)
    assert snap.change_pct == pytest.approx(10.0, abs=0.1)


def test_a_bar_inside_the_regular_session_is_not_an_extended_hours_print(config, db):
    """A five-minute bar opens before its trades happen, so mid-session the
    newest bar can be stamped a few seconds after the last regular print. That
    is the session we are already in, not an extended-hours quote."""
    payload = {"chart": {"result": [{
        "meta": {"symbol": "TEVA", "regularMarketPrice": 19.80,
                 "previousClose": 18.00,
                 "regularMarketTime": et_stamp(2026, 7, 30, 11, 4)},
        "timestamp": [et_stamp(2026, 7, 30, 11, 0), et_stamp(2026, 7, 30, 11, 5)],
        "indicators": {"quote": [{"close": [19.78, 19.79]}]},
    }]}}
    snap = collector(config, db, {"interval=5m": payload})._yahoo_snapshot("TEVA")

    assert snap.last == pytest.approx(19.80)
    assert snap.market_time.astimezone(ET).strftime("%H:%M") == "11:04"


def test_the_tape_alert_measures_the_move_a_trader_can_still_trade(config, db):
    """`_unexplained_move_item` tested its threshold against the stale 16:00
    number, so the print that actually moved never reached the alert text.

    The alert measures the whole repricing - prior close to the latest print,
    through the extended session - while `change_pct` stays the regular session
    alone. The two answer different questions: a name that closed flat and then
    went 25% bid on an after-hours leak has moved 25% and must be flagged, but
    it has not moved 25% against its sector index, which did not trade.
    """
    payload = fixture_json("yahoo_teva_afterhours.json")
    items = list(collector(config, db, {"interval=5m": payload}).collect())

    alert = next(i for i in items
                 if i.meta.get("kind") == "unexplained_move" and "TEVA" in i.title)
    assert alert.meta["change_pct"] == pytest.approx(25.0, abs=0.1)
    assert "25." in alert.title, "the headline number is the tradeable one"
    # Both halves stay legible, so the reader can see where the move happened.
    assert alert.meta["session_change_pct"] == pytest.approx(10.0, abs=0.1)
    assert alert.meta["extended_change_pct"] == pytest.approx(13.64, abs=0.1)


# --------------------------------------------------------------- sessions -- #
@pytest.mark.parametrize("utc, expected", [
    # DST ends the first Sunday of November, so November 20th is EST (UTC-5) -
    # a month-based `4 if 3 <= month <= 11 else 5` had these an hour early.
    ("2026-11-20T13:45", "premarket"),    # 08:45 ET, half an hour to the open
    ("2026-11-20T20:30", "regular"),      # 15:30 ET, half an hour to the bell
    # DST starts the second Sunday of March, so March 5th is still EST.
    ("2027-03-05T20:30", "regular"),      # 15:30 ET
    # Controls either side of the switch, where the guess happened to be right.
    ("2026-07-30T20:30", "afterhours"),   # 16:30 ET on EDT
    ("2026-01-15T19:30", "regular"),      # 14:30 ET on EST
    ("2026-08-01T17:00", "closed"),       # a Saturday
])
def test_the_session_label_follows_the_tzdb_not_the_month(utc, expected):
    """The label is stored on every snapshot, printed in the [TAPE] alert and
    read by the scoring timing boost, so an hour of drift is an hour of the
    feed calling the pre-market open."""
    now = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
    assert current_session(now) == expected


# ------------------------------------------------------------ daily bars --- #
def test_a_daily_bars_observation_time_is_that_sessions_close(config, db):
    """A bar date parsed bare is midnight UTC, which the terminal renders in ET
    as the previous evening: the 2026-07-31 bar was labelled "Thursday 20:00
    ET". A daily bar is the record of a session, and its print is the close."""
    csv = ("Date,Open,High,Low,Close,Volume\n"
           "2026-07-30,18.00,20.10,17.95,19.80,41000000\n"
           "2026-07-31,19.80,20.40,19.60,20.20,12000000\n")
    snap = collector(config, db, {"stooq.com": csv},
                     source="prices_stooq")._stooq_snapshot("TEVA")

    printed = snap.market_time.astimezone(ET)
    assert printed.strftime("%a %Y-%m-%d %H:%M") == "Fri 2026-07-31 16:00"
    # `asof` is when we fetched, and we fetched it just now.
    assert (datetime.now(timezone.utc) - snap.asof).total_seconds() < 60


def test_todays_bar_keeps_up_with_the_session_it_is_recording(config, db):
    """Yahoo's 1d chart includes the session in progress, so the first pass
    after the open stores a PARTIAL bar - and the once-a-day refetch guard then
    returned early for the rest of the day. A name that opened 18.00 and closed
    20.50 kept its 09:35 values until the next morning, on the same screen as
    the live quote (`ticker_brief` returns `recent_bars` beside `price`)."""
    today = datetime.now(ET).date()
    open_ts = et_stamp(today.year, today.month, today.day, 9, 30)

    def routes(close, high, volume, quote_ts):
        return {
            "interval=1d": {"chart": {"result": [{
                "meta": {"symbol": "TEVA"},
                "timestamp": [open_ts - 86400, open_ts],
                "indicators": {"quote": [{
                    "open": [17.5, 18.00], "high": [18.2, high],
                    "low": [17.4, 17.98], "close": [18.0, close],
                    "volume": [8_000_000, volume]}]}}]}},
            "interval=5m": {"chart": {"result": [{
                "meta": {"symbol": "TEVA", "regularMarketPrice": close,
                         "previousClose": 18.00, "regularMarketTime": quote_ts,
                         "regularMarketVolume": volume,
                         "regularMarketDayHigh": high, "regularMarketDayLow": 17.98},
                "timestamp": [], "indicators": {"quote": [{}]}}]}},
        }

    collector(config, db, routes(18.05, 18.10, 900_000,
                                 open_ts + 300))._yahoo_snapshot("TEVA")
    later = collector(config, db, routes(20.50, 20.60, 14_000_000,
                                         open_ts + 6 * 3600))
    later._yahoo_snapshot("TEVA")

    bar = db.recent_bars("TEVA", 1)[-1]
    assert bar["date"] == today.isoformat()
    assert bar["close"] == pytest.approx(20.50)
    assert bar["high"] == pytest.approx(20.60)
    assert bar["volume"] == pytest.approx(14_000_000)
    # The open came from the daily chart and must survive the refresh.
    assert bar["open"] == pytest.approx(18.00)
    # …and none of it cost a second request against an endpoint that
    # rate-limits aggressively: the second pass only fetched the quote.
    assert [c for c in later.client.calls if "interval=1d" in c] == []


# ------------------------------------------------------------ going blind -- #
def test_a_basket_that_saved_nothing_is_an_outage_not_a_quiet_day(config, db):
    """Yahoo degrades politely: HTTP 200, an empty chart result, and the reason
    tucked into chart.error. Every name failing that way used to leave no trace
    at all - no snapshot, no warning, nothing in source_state - so the whole
    tape went dark and `harel doctor` read the source as healthy."""
    empty = {"chart": {"result": [], "error": {"code": "Not Found"}}}
    coll = collector(config, db, {"query1.finance.yahoo.com": empty})
    items = list(coll.collect())

    assert items == [], "no snapshots means no tape alerts to emit"
    assert any("empty chart result" in w for w in coll.warnings), \
        "a 200 that carries nothing must still be named per ticker"

    expected = (f"no price snapshots saved for any of "
                f"{len(config.active_tickers)} tickers")
    assert expected in coll.warnings
    assert db.get_source_state("prices_yahoo").get("last_error") == expected, \
        "the outage must reach source_state, which is what `harel doctor` reads"


def test_a_finished_session_stops_moving(config, db):
    """The bug the user caught: SOXX reported +0.1% at the bell and -0.8% an
    hour later, on a session that had been over the whole time.

    `change_pct` was the LAST print against the prior close, and after 16:00
    the last print is a post-market tick. So the number kept drifting, and the
    relative-move comparison measured a small-cap's thin post-market against an
    ETF's - different hours, different liquidity, not a comparison.
    """
    payload = fixture_json("yahoo_teva_afterhours.json")
    snap = collector(config, db, {"interval=5m": payload})._yahoo_snapshot("TEVA")

    regular, prior = snap.last, snap.prev_close
    assert snap.change_pct == pytest.approx((regular - prior) / prior * 100)
    # Every post-market print in the fixture is above the close, so a blended
    # number would be strictly larger. The session number must not have moved.
    assert snap.extended_last > snap.last
    assert snap.change_pct < snap.extended_last / prior * 100 - 100 + 100
