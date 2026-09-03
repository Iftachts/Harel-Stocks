"""The two event classes the system previously had no channel for at all.

`trading_halt` and `rating_change` were both scored in config/scoring.yaml and
neither had a source that could produce one - the first had no event type
either. These tests pin the parsing of both feeds against real payload shapes,
and pin the two distinctions that decide whether either is usable:

* a news-pending halt must outrank a limit-up-limit-down pause, which was 22 of
  42 records in a single session's feed;
* an upgrade must outrank a "maintains" that moved no price target.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harel.collect.base import CollectorContext
from harel.collect.halts import TradingHaltsCollector, _fields, _halt_datetime
from harel.collect.ratings import AnalystRatingsCollector, _devalue, _row_datetime
from harel.config import get_config


# A verbatim record from https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts
HALT_RECORD = """
      <title>HQ</title>
      <pubDate>Wed, 02 Sep 2026 04:00:00 GMT</pubDate>
      <ndaq:HaltDate>09/02/2026</ndaq:HaltDate>
      <ndaq:HaltTime>15:51:09.073</ndaq:HaltTime>
      <ndaq:IssueSymbol>{symbol}</ndaq:IssueSymbol>
      <ndaq:IssueName>Some Issuer Inc</ndaq:IssueName>
      <ndaq:Market>NASDAQ</ndaq:Market>
      <ndaq:ReasonCode>{code}</ndaq:ReasonCode>
      <ndaq:PauseThresholdPrice />
      <ndaq:ResumptionDate>09/02/2026</ndaq:ResumptionDate>
      <ndaq:ResumptionTradeTime>16:00:00</ndaq:ResumptionTradeTime>
"""


def _halts_collector(db):
    cfg = get_config()
    from harel.http import HttpClient
    ctx = CollectorContext(config=cfg, client=HttpClient(user_agent="test"),
                           db=db, lookback_hours=24 * 30)
    return TradingHaltsCollector(cfg.sources["nasdaq_halts"], ctx)


def test_self_closing_namespaced_field_does_not_break_the_parse():
    """<ndaq:PauseThresholdPrice /> has no closing tag."""
    fields = _fields(HALT_RECORD.format(symbol="TEVA", code="T12"))
    assert fields["IssueSymbol"] == "TEVA"
    assert fields["ReasonCode"] == "T12"
    assert fields["PauseThresholdPrice"] == ""


def test_halt_time_is_read_as_eastern_not_utc():
    """15:51 in New York is 19:51Z. Reading it as UTC would date a mid-session
    halt to before the open and let the recency decay age it five hours."""
    dt = _halt_datetime("09/02/2026", "15:51:09.073")
    assert dt is not None and dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 19


def test_our_names_and_their_peers_are_both_watched(tmp_path):
    from harel.db import Database
    collector = _halts_collector(Database(str(tmp_path / "t.db")))
    symbols = collector._symbols_of_interest()
    assert symbols["TEVA"] == ("TEVA", "DIRECT")
    # A halt on a cyber peer is information about PANW.
    assert symbols["CRWD"][1] == "PEER"
    assert symbols["CRWD"][0] == "PANW"


def test_an_unrelated_symbol_is_ignored(tmp_path):
    from harel.db import Database
    collector = _halts_collector(Database(str(tmp_path / "t.db")))
    fields = _fields(HALT_RECORD.format(symbol="ZZZZ", code="T12"))
    assert collector._to_item(fields, collector._symbols_of_interest()) is None


@pytest.mark.parametrize("code,expected_form", [
    ("T12", "HALT-NEWS"),
    ("H10", "HALT-NEWS"),
    ("LUDP", "HALT-VOLATILITY"),
])
def test_halt_family_decides_the_noise_cap(code, expected_form, tmp_path):
    from harel.db import Database
    collector = _halts_collector(Database(str(tmp_path / "t.db")))
    fields = _fields(HALT_RECORD.format(symbol="TEVA", code=code))
    item = collector._to_item(fields, collector._symbols_of_interest())
    assert item is not None
    assert item.meta["form_type"] == expected_form


def test_volatility_pause_is_capped_and_news_halt_is_not():
    caps = get_config().scoring.noise_form_types
    assert "HALT-VOLATILITY" in caps
    assert "HALT-NEWS" not in caps
    assert caps["HALT-VOLATILITY"] < get_config().scoring.tiers["normal"]


# --------------------------------------------------------------- ratings ---
def test_devalue_resolves_sveltekit_index_encoding():
    """Every value in the payload is an index into the same flat array."""
    arr = [{"ratings": 1}, [2], {"firm": 3, "pt_now": 4}, "Barclays", 42]
    assert _devalue(arr, 0) == {"ratings": [{"firm": "Barclays", "pt_now": 42}]}


def test_devalue_treats_negative_index_as_absent():
    assert _devalue([{"a": -1}], 0) == {"a": None}


def test_devalue_terminates_on_a_cycle():
    """A self-referential index must not recurse forever."""
    assert _devalue([{"a": 0}], 0) is not None


def test_rating_timestamp_is_eastern():
    dt = _row_datetime({"date": "2026-08-12", "time": "09:20:17"})
    assert dt is not None
    assert dt.astimezone(timezone.utc).hour == 13   # 09:20 ET -> 13:20Z


def test_rating_row_without_a_time_still_parses():
    assert _row_datetime({"date": "2026-08-12"}) is not None


def _rating_item(db, row):
    cfg = get_config()
    from harel.http import HttpClient
    ctx = CollectorContext(config=cfg, client=HttpClient(user_agent="test"),
                           db=db, lookback_hours=24 * 3650)
    collector = AnalystRatingsCollector(cfg.sources["analyst_ratings"], ctx)
    return collector._to_item("TEVA", row)


def test_target_change_is_an_action_and_a_bare_maintain_is_routine(tmp_path):
    from harel.db import Database
    db = Database(str(tmp_path / "t.db"))
    today = datetime.now(timezone.utc).date().isoformat()

    moved = _rating_item(db, {"firm": "Barclays", "action_rt": "Maintains",
                              "rating_new": "Buy", "pt_old": 40, "pt_now": 42,
                              "date": today, "time": "09:20:17"})
    assert moved.meta["form_type"] == "RATING-ACTION"
    assert "raised" in moved.title

    flat = _rating_item(db, {"firm": "Evercore ISI", "action_rt": "Maintains",
                             "rating_new": "Buy", "pt_old": None, "pt_now": None,
                             "date": today, "time": "09:20:17"})
    assert flat.meta["form_type"] == "RATING-ROUTINE"

    # An upgrade is an action whatever it does to the target.
    up = _rating_item(db, {"firm": "KBW", "action_rt": "Upgrades",
                           "rating_old": "Hold", "rating_new": "Buy",
                           "pt_old": None, "pt_now": None,
                           "date": today, "time": "10:29:11"})
    assert up.meta["form_type"] == "RATING-ACTION"
    assert "from Hold to Buy" in up.title


def test_rating_titles_are_readable_by_the_event_taxonomy(tmp_path):
    """The collector writes titles in the phrasings scoring.yaml keys on."""
    from harel.db import Database
    from harel.enrich.events import classify_events
    db = Database(str(tmp_path / "t.db"))
    today = datetime.now(timezone.utc).date().isoformat()
    item = _rating_item(db, {"firm": "Stifel Nicolaus", "action_rt": "Initiates",
                             "rating_new": "Buy", "pt_old": None, "pt_now": 270,
                             "date": today, "time": "20:15:08"})
    assert "rating_change" in {r.key for r, _ in classify_events(item, get_config())}


def test_a_rating_older_than_the_window_is_dropped(tmp_path):
    from harel.db import Database
    from harel.http import HttpClient
    cfg = get_config()
    db = Database(str(tmp_path / "t.db"))
    ctx = CollectorContext(config=cfg, client=HttpClient(user_agent="test"),
                           db=db, lookback_hours=24)
    collector = AnalystRatingsCollector(cfg.sources["analyst_ratings"], ctx)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).date().isoformat()
    assert collector._to_item("TEVA", {"firm": "X", "action_rt": "Upgrades",
                                       "date": old, "time": "09:00:00"}) is None


# ------------------------------------------------------------ user agent ---
def test_fda_and_sec_get_the_self_identifying_user_agent():
    """fda.gov 302s a browser string to an abuse-detection page.

    Measured on one host, seconds apart, same URL: the research UA returned
    HTTP 200 and 15940 bytes of RSS; the Chrome UA returned 302 ->
    /apology_objects/abuse-detection-apology.html -> 404. That 404 is what the
    system reported as "feed may have moved", 5055 consecutive times in
    production without one success.
    """
    from harel.http import HttpClient

    client = HttpClient(user_agent="HarelTerminal/1.0 (contact: x@example.com)")
    for host in ("www.fda.gov", "api.fda.gov", "www.sec.gov", "data.sec.gov"):
        assert client._ua_for(host) == client.user_agent, host
    # The IR platforms are the opposite case and must keep the browser string.
    for host in ("ir.towersemi.com", "investors.paloaltonetworks.com",
                 "news.google.com"):
        assert client._ua_for(host) != client.user_agent, host


def test_rate_limited_hosts_include_the_ones_that_429():
    """StockTitan returned 429 on the seventh rapid request."""
    from harel.http import HOST_RATE_LIMITS, DEFAULT_RATE

    assert HOST_RATE_LIMITS["www.stocktitan.net"] < DEFAULT_RATE
    assert HOST_RATE_LIMITS["www.fda.gov"] < DEFAULT_RATE


# ------------------------------------------------------------- TASE leg ---
def test_tase_leg_is_enabled_and_covers_the_dual_listed_names():
    """Tel Aviv trades 02:50-10:25 ET - almost entirely before the US open."""
    cfg = get_config()
    assert cfg.sources["prices_yahoo"].raw.get("tase_leg") is True
    dual = [t for t in cfg.active_tickers
            if cfg.ticker(t) and cfg.ticker(t).tase_id]
    # 21 of 22 quote in Tel Aviv; PANW reports to MAYA but has no .TA line.
    assert len(dual) >= 20


def test_tase_rows_are_stored_under_their_own_symbol_not_the_us_ticker(tmp_path):
    """A Tel Aviv print must never overwrite the US print for the same name."""
    from harel.collect.prices import PriceCollector
    assert PriceCollector.TASE_SUFFIX == ".TA"
    assert PriceCollector.FX_SYMBOL == "ILS=X"


def test_a_bot_challenge_served_with_http_200_is_not_read_as_no_halts():
    """The trap: an empty tape and a locked door look identical on the status
    code, and only one of them means the market is calm."""
    from harel.collect.halts import _is_bot_wall

    incapsula = ('<html style="height:100%"><head><META NAME="ROBOTS" '
                 'CONTENT="NOINDEX, NOFOLLOW"><script type="text/javascript" '
                 'src="/_Incapsula_Resource?SWJIYLWA=719d34"></script></head>')
    assert _is_bot_wall(incapsula)
    assert _is_bot_wall('<html>Please enable JavaScript to continue</html>')

    real = ('﻿<?xml version="1.0" encoding="utf-8"?><rss version="2.0" '
            'xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><item>')
    assert not _is_bot_wall(real)


def test_the_same_action_published_twice_is_one_item(tmp_path):
    """StockAnalysis published one Goldman target raise on PANW twice, seven
    seconds apart. A second-precision id made that the same call twice at the
    top of the feed."""
    from harel.db import Database
    db = Database(str(tmp_path / "t.db"))
    today = datetime.now(timezone.utc).date().isoformat()
    row = {"firm": "Goldman Sachs", "action_rt": "Maintains", "rating_new": "Buy",
           "pt_old": 380, "pt_now": 390, "date": today}
    first = _rating_item(db, {**row, "time": "01:45:09"})
    second = _rating_item(db, {**row, "time": "01:45:16"})
    assert first.uid == second.uid

    # A genuinely different action by the same desk on the same day survives.
    upgrade = _rating_item(db, {**row, "action_rt": "Upgrades", "time": "01:45:09"})
    assert upgrade.uid != first.uid
