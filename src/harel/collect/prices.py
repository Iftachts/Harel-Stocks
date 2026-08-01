"""Price / tape context.

Two jobs:

1. Give the scorer something to corroborate with. News that the tape is already
   confirming outranks news that nothing reacted to.
2. Catch the inverse case - a stock moving hard with *no* story attached. For a
   short-term trader that is itself a top-priority alert ("what do they know?"),
   so this collector emits it as a synthetic news item.

Sources are free and therefore delayed. See docs/LIMITATIONS.md: if you need
true real-time quotes, that is a paid feed, and it is a separate decision from
the news pipeline.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..http import HttpError
from ..models import PriceSnapshot, RawItem
from .base import Collector, register

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# The exchange's own clock. Every session boundary below is a wall-clock time in
# New York, so it has to be resolved against the real tzdb - see current_session.
MARKET_TZ = ZoneInfo("America/New_York")

PREMARKET_START = dtime(4, 0)
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
AFTERHOURS_END = dtime(20, 0)


@register("prices")
class PriceCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        use_yahoo = "finance.yahoo.com" in self.source.base_url
        # Index proxies first, so a name's move can be read against its group.
        # Absolute moves alone made the whole basket look unexplained on a day
        # the semis index ran 8%.
        if use_yahoo:
            for symbol in self.cfg.benchmark_symbols:
                try:
                    snap = self._yahoo_snapshot(symbol)
                    if snap is not None:
                        self.db.save_price(snap)
                except Exception as exc:
                    self.warn(f"benchmark {symbol}: {type(exc).__name__}: {exc}")

        for ticker in self.active_tickers:
            try:
                snap = (
                    self._yahoo_snapshot(ticker) if use_yahoo
                    else self._stooq_snapshot(ticker)
                )
            except HttpError as exc:
                self.warn(f"{ticker}: {exc}")
                continue
            except Exception as exc:
                self.warn(f"{ticker}: unexpected {type(exc).__name__}: {exc}")
                continue

            if snap is None:
                continue
            self.db.save_price(snap)

            alert = self._unexplained_move_item(snap)
            if alert is not None:
                yield alert
        self.db.conn.commit()

    # -- Stooq: daily bars, ADV, gap ---------------------------------------- #
    def _stooq_snapshot(self, ticker: str) -> PriceSnapshot | None:
        url = STOOQ_URL.format(symbol=f"{ticker.lower()}.us")
        resp = self.client.get(url, allow_status=(404,))
        if resp.status >= 400 or not resp.text.strip():
            self.warn(f"{ticker}: stooq returned no data")
            return None

        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if not rows or "Close" not in rows[0]:
            self.warn(f"{ticker}: unexpected stooq CSV header {list(rows[0]) if rows else []}")
            return None

        bars = []
        for row in rows[-260:]:
            try:
                bars.append({
                    "date": row["Date"],
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]), "close": float(row["Close"]),
                    "volume": float(row.get("Volume") or 0),
                })
            except (ValueError, KeyError):
                continue
        if len(bars) < 2:
            return None
        self.db.save_bars(ticker, bars)

        last_bar, prev_bar = bars[-1], bars[-2]
        # A daily bar's observation time is that session's closing print, 16:00
        # ET. Parsed as a bare date it is midnight UTC, which the terminal
        # renders in ET as the PREVIOUS evening: a 2026-07-31 bar was displayed
        # as "Thursday 20:00 ET" instead of Friday 16:00 ET.
        closed_at = datetime.strptime(last_bar["date"], "%Y-%m-%d").replace(
            hour=16, tzinfo=MARKET_TZ).astimezone(timezone.utc)
        adv20 = _mean([b["volume"] for b in bars[-21:-1]]) or None
        change_pct = (
            (last_bar["close"] - prev_bar["close"]) / prev_bar["close"] * 100
            if prev_bar["close"] else None
        )
        return PriceSnapshot(
            ticker=ticker,
            # `asof` is when we fetched, `market_time` when the exchange
            # printed. Stooq set both to the bar date, so the quote panel
            # reported a bar we had just downloaded as "fetched 20 hours ago".
            asof=datetime.now(timezone.utc),
            market_time=closed_at,
            last=last_bar["close"],
            prev_close=prev_bar["close"],
            change_pct=change_pct,
            volume=last_bar["volume"],
            adv20=adv20,
            volume_multiple=(last_bar["volume"] / adv20) if adv20 else None,
            day_high=last_bar["high"],
            day_low=last_bar["low"],
            session="closed",
            provider="stooq",
        )

    def _yahoo_backfill_bars(self, ticker: str) -> None:
        """Keep a daily bar history so ADV20 - and therefore relative volume -
        actually exists.

        `_adv_from_bars` reads the `bars` table, which only the Stooq collector
        ever wrote, and Stooq has been returning nothing. So adv20 was None for
        every name, `volume_multiple` was None with it, and the movers board
        showed "vol -" for everything. The scoring formula in the README claims
        a tape-confirmation bonus on volume > 2x ADV; that half has never once
        fired. Yahoo returns ~60 daily bars with volume in one call, so the data
        was there all along.

        Refetched at most once a day per name: the *history* behind today does
        not change between two five-minute passes. Today's own bar does - it is
        the session in progress - and `_refresh_today_bar` keeps that one
        current out of the quote we already fetch, at no extra request.
        """
        existing = self.db.recent_bars(ticker, 1)
        # Bar dates are exchange dates, so "today" has to be one too. On UTC
        # dates this guard stopped matching at 20:00 ET, when the UTC day has
        # already rolled over, and refetched 3 months of history on every pass
        # until midnight ET.
        today = datetime.now(MARKET_TZ).date().isoformat()
        if existing and str(existing[-1].get("date", ""))[:10] >= today:
            return

        resp = self.client.get(
            YAHOO_URL.format(symbol=ticker),
            params={"range": "3mo", "interval": "1d"},
            allow_status=(404, 401, 403, 429),
        )
        if resp.status >= 400:
            return
        result = ((resp.json() or {}).get("chart") or {}).get("result") or []
        if not result:
            return
        stamps = result[0].get("timestamp") or []
        quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]

        bars = []
        for i, ts in enumerate(stamps):
            def at(key: str) -> float | None:
                series = quote.get(key) or []
                return series[i] if i < len(series) else None

            close = at("close")
            if close is None:
                continue          # Yahoo pads holidays with nulls
            bars.append({
                # The exchange's date, the same one `_refresh_today_bar` keys
                # on: two spellings of one session would be two rows for it.
                "date": datetime.fromtimestamp(ts, MARKET_TZ).date().isoformat(),
                "open": at("open") or close, "high": at("high") or close,
                "low": at("low") or close, "close": close,
                "volume": at("volume") or 0,
            })
        if bars:
            self.db.save_bars(ticker, bars)

    def _refresh_today_bar(self, ticker: str, meta: dict[str, Any]) -> None:
        """Keep the bar for the session in progress in step with the quote.

        Yahoo's interval=1d chart includes today, so the first backfill after
        the open stores a PARTIAL bar - and the once-a-day guard above then
        returns early for the rest of the day. A name that opened 18.00 and
        closed 20.50 kept its 09:35 values until the next morning, on the same
        screen as the live quote (`views.ticker_brief` returns `recent_bars`
        next to `price`), so the two disagreed all day.

        The refresh comes out of the meta block of the quote we already
        fetched: the bar and the quote can no longer disagree because they are
        the same numbers, and it costs no second request against an endpoint
        that rate-limits aggressively.
        """
        ts = meta.get("regularMarketTime")
        close = meta.get("regularMarketPrice")
        if not isinstance(ts, (int, float)) or not ts or close is None:
            return
        date = datetime.fromtimestamp(ts, MARKET_TZ).date().isoformat()
        stored = next((b for b in self.db.recent_bars(ticker, 3)
                       if str(b.get("date", ""))[:10] == date), {})
        self.db.save_bars(ticker, [{
            "date": date,
            # meta carries no open. The backfill's bar does, and replacing the
            # row must not throw it away.
            "open": stored.get("open"),
            "high": meta.get("regularMarketDayHigh") or stored.get("high") or close,
            "low": meta.get("regularMarketDayLow") or stored.get("low") or close,
            "close": close,
            "volume": meta.get("regularMarketVolume") or stored.get("volume") or 0,
        }])

    # -- Yahoo: including pre/post market ----------------------------------- #
    def _yahoo_snapshot(self, ticker: str) -> PriceSnapshot | None:
        self._yahoo_backfill_bars(ticker)
        resp = self.client.get(
            YAHOO_URL.format(symbol=ticker),
            params={"range": "5d", "interval": "5m", "includePrePost": "true"},
            allow_status=(404, 401, 403, 429),
        )
        if resp.status >= 400:
            self.warn(
                f"{ticker}: Yahoo returned HTTP {resp.status} - this endpoint is "
                f"unofficial and rate-limits aggressively"
            )
            return None

        result = ((resp.json() or {}).get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}

        self._refresh_today_bar(ticker, meta)

        last = meta.get("regularMarketPrice")
        # `chartPreviousClose` is the close before the *requested range*, so with
        # range=5d it is last week's close and every move looks like a 5-day move.
        # `previousClose` is the prior session's close, which is what an intraday
        # move means. Keep the chart field only as a fallback.
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")

        # The exchange's own timestamp for the last print. Without it the only
        # time we had was our fetch time, so a Friday close pulled on Saturday
        # was presented as a two-minute-old price - an observation time and a
        # fetch time are not the same number and must not share a field.
        market_ts = meta.get("regularMarketTime")
        market_time = (datetime.fromtimestamp(market_ts, timezone.utc)
                       if isinstance(market_ts, (int, float)) and market_ts else None)

        # Pre/post-market prints are the reason this source is here at all
        # ("they matter a lot for an overnight-gap workflow"), and meta does not
        # carry them: the v8 chart endpoint has no preMarketPrice/postMarketPrice
        # key, so the loop that looked for them never fired once and `last` was
        # always the 16:00 print. They are in the 5m series, stamped after
        # regularMarketTime. TEVA closing 19.80, reporting after the bell and
        # trading 22.50 at 19:34 ET was published as 19.80 / +10% / "afterhours":
        # the whole after-hours move invisible, under an after-hours label.
        # The session's own return, measured close-to-close and nothing else.
        # Overwriting `last` with the post-market print - which is what this did
        # - made `change_pct` mean "regular session plus whatever has happened
        # since", so a finished session kept moving: SOXX read +0.07% at the
        # bell and -0.77% once a thin post-market print landed. Worse, the
        # relative-move comparison then measured a small-cap's post-market drift
        # against an ETF's, which are not the same hours or the same liquidity.
        change_pct = (
            (last - prev_close) / prev_close * 100
            if last and prev_close else None
        )

        extended_last = extended_change = extended_time = None
        extended = _last_extended_print(result[0], market_ts)
        if extended is not None:
            extended_last, extended_time = extended
            if last:
                extended_change = (extended_last - last) / last * 100
        adv20 = self._adv_from_bars(ticker)
        volume = meta.get("regularMarketVolume")

        return PriceSnapshot(
            ticker=ticker,
            asof=datetime.now(timezone.utc),
            market_time=market_time,
            last=last,
            prev_close=prev_close,
            change_pct=change_pct,
            extended_last=extended_last,
            extended_change_pct=extended_change,
            extended_time=extended_time,
            volume=volume,
            adv20=adv20,
            volume_multiple=(volume / adv20) if (volume and adv20) else None,
            day_high=meta.get("regularMarketDayHigh"),
            day_low=meta.get("regularMarketDayLow"),
            session=current_session(),
            provider="yahoo",
        )

    def _adv_from_bars(self, ticker: str) -> float | None:
        bars = self.db.recent_bars(ticker, 21)
        vols = [b["volume"] for b in bars[:-1] if b.get("volume")]
        return _mean(vols) or None

    # -- synthetic alert ---------------------------------------------------- #
    def _unexplained_move_item(self, snap: PriceSnapshot) -> RawItem | None:
        conf = self.cfg.scoring.price_confirmation
        if not conf.get("enabled", True):
            return None
        threshold = float(conf.get("unexplained_move_pct", 5.0))
        # The repricing a trader is actually looking at, which is the whole way
        # from the prior close to the latest print - through the extended
        # session when there is one. `change_pct` is deliberately the regular
        # session alone, because that is the only basis on which a name and its
        # sector index compare; but a name that closed flat and then went 25%
        # bid on an after-hours leak has moved 25%, and an alert measuring the
        # session would never say so.
        move = _total_move(snap)
        if move is None or abs(move) < threshold:
            return None

        # Only "unexplained" if nothing decent landed for this name recently.
        recent = self.db.feed(
            tickers=[snap.ticker], min_score=45, since_hours=18, limit=1,
            collapse_clusters=False,
        )
        if recent:
            return None

        direction = "up" if move > 0 else "down"
        return self.make_item(
            external_id=f"unexplained:{snap.ticker}:{snap.asof.date().isoformat()}:{direction}",
            title=(
                f"[TAPE] {snap.ticker} {direction} {abs(move):.1f}% "
                f"with no matching news"
            ),
            url="",
            summary=(
                f"{snap.ticker} moved {move:+.2f}% from the prior close "
                f"(regular session {snap.change_pct:+.2f}%"
                + (f", then {snap.extended_change_pct:+.2f}% after the bell"
                   if snap.extended_change_pct else "")
                + f"; now: {snap.session}"
                + (f", volume {snap.volume_multiple:.1f}x ADV20"
                   if snap.volume_multiple else "")
                + "). No item scoring >=45 was collected in the last 18 hours. "
                "Either the driver is not in our sources, or it is a flow/technical move."
            ),
            published_at=snap.asof,
            seed_tickers=[snap.ticker],
            seed_relation="DIRECT",
            meta={
                "synthetic": True,
                "kind": "unexplained_move",
                "change_pct": move,
                "session_change_pct": snap.change_pct,
                "extended_change_pct": snap.extended_change_pct,
                "volume_multiple": snap.volume_multiple,
                "session": snap.session,
                "forced_score": float(conf.get("unexplained_alert_score", 70)),
            },
        )


def _total_move(snap: PriceSnapshot) -> float | None:
    """Prior close to the latest print, extended hours included."""
    last = snap.extended_last if snap.extended_last is not None else snap.last
    if not last or not snap.prev_close:
        return None
    return (last - snap.prev_close) / snap.prev_close * 100


def current_session(now: datetime | None = None) -> str:
    """Which US session a moment falls in, on the exchange's own clock.

    `offset = 4 if 3 <= month <= 11 else 5` was a month-based DST guess, and
    DST does not turn on the 1st: it ends the first Sunday of November and
    starts the second Sunday of March. So for most of November and the first
    week of March every 13:30-14:30 UTC print was labelled "regular" while the
    tape was still pre-market, and every 20:00-21:00 UTC print "afterhours"
    with half an hour left in the session. That label is stored on the
    snapshot, shown in the [TAPE] alert and read by the scoring timing boost.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et_dt = now.astimezone(MARKET_TZ)
    et = et_dt.time()
    if et_dt.weekday() >= 5:
        return "closed"
    if PREMARKET_START <= et < REGULAR_OPEN:
        return "premarket"
    if REGULAR_OPEN <= et < REGULAR_CLOSE:
        return "regular"
    if REGULAR_CLOSE <= et < AFTERHOURS_END:
        return "afterhours"
    return "closed"


def _last_extended_print(chart: dict[str, Any],
                         regular_market_ts: Any) -> tuple[float, datetime] | None:
    """Newest 5m bar that printed outside the regular session, with its time.

    Matched on the bar's own clock rather than on "is it after hours *now*", so
    the 19:55 print is still the last price at 03:00 the next morning, and the
    same rule picks up pre-market prints: before the open, regularMarketTime is
    still yesterday's close, so today's 08:00 bars sit after it.
    """
    if not isinstance(regular_market_ts, (int, float)) or not regular_market_ts:
        return None                # nothing to measure "after the close" against
    stamps = chart.get("timestamp") or []
    quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    for i in range(min(len(stamps), len(closes)) - 1, -1, -1):
        ts = stamps[i]
        if not isinstance(ts, (int, float)) or ts <= regular_market_ts:
            break                  # timestamps ascend; everything older is older
        close = closes[i]
        if close is None:
            continue               # Yahoo pads gaps in the extended session
        # A bar's timestamp is the minute it OPENED, and the trade happened
        # somewhere in the five minutes after it. Reporting the open is the
        # end of that window we can actually stand behind.
        printed = datetime.fromtimestamp(ts, timezone.utc)
        if current_session(printed) in ("premarket", "afterhours"):
            return float(close), printed
    return None


def _mean(values: list[float]) -> float:
    values = [v for v in values if v]
    return sum(values) / len(values) if values else 0.0
