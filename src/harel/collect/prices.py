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
from datetime import datetime, time as dtime, timedelta, timezone

from ..http import HttpError
from ..models import PriceSnapshot, RawItem
from .base import Collector, register

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# US market hours in ET, expressed in UTC offsets we resolve at runtime.
PREMARKET_START = dtime(4, 0)
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
AFTERHOURS_END = dtime(20, 0)


@register("prices")
class PriceCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        use_yahoo = "finance.yahoo.com" in self.source.base_url
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
        adv20 = _mean([b["volume"] for b in bars[-21:-1]]) or None
        change_pct = (
            (last_bar["close"] - prev_bar["close"]) / prev_bar["close"] * 100
            if prev_bar["close"] else None
        )
        return PriceSnapshot(
            ticker=ticker,
            asof=datetime.strptime(last_bar["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc),
            last=last_bar["close"],
            prev_close=prev_bar["close"],
            change_pct=change_pct,
            volume=last_bar["volume"],
            adv20=adv20,
            volume_multiple=(last_bar["volume"] / adv20) if adv20 else None,
            day_high=last_bar["high"],
            day_low=last_bar["low"],
            session="closed",
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

        Refetched at most once a day per name: 3 months of daily bars do not
        change between two five-minute passes.
        """
        existing = self.db.recent_bars(ticker, 1)
        today = datetime.now(timezone.utc).date().isoformat()
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
                "date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
                "open": at("open") or close, "high": at("high") or close,
                "low": at("low") or close, "close": close,
                "volume": at("volume") or 0,
            })
        if bars:
            self.db.save_bars(ticker, bars)

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

        last = meta.get("regularMarketPrice")
        # `chartPreviousClose` is the close before the *requested range*, so with
        # range=5d it is last week's close and every move looks like a 5-day move.
        # `previousClose` is the prior session's close, which is what an intraday
        # move means. Keep the chart field only as a fallback.
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        # Pre/post prints live in a separate block on this endpoint.
        for key in ("preMarketPrice", "postMarketPrice"):
            if meta.get(key):
                last = meta[key]

        change_pct = (
            (last - prev_close) / prev_close * 100
            if last and prev_close else None
        )
        adv20 = self._adv_from_bars(ticker)
        volume = meta.get("regularMarketVolume")

        return PriceSnapshot(
            ticker=ticker,
            asof=datetime.now(timezone.utc),
            last=last,
            prev_close=prev_close,
            change_pct=change_pct,
            volume=volume,
            adv20=adv20,
            volume_multiple=(volume / adv20) if (volume and adv20) else None,
            day_high=meta.get("regularMarketDayHigh"),
            day_low=meta.get("regularMarketDayLow"),
            session=current_session(),
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
        if snap.change_pct is None or abs(snap.change_pct) < threshold:
            return None

        # Only "unexplained" if nothing decent landed for this name recently.
        recent = self.db.feed(
            tickers=[snap.ticker], min_score=45, since_hours=18, limit=1,
            collapse_clusters=False,
        )
        if recent:
            return None

        direction = "up" if snap.change_pct > 0 else "down"
        return self.make_item(
            external_id=f"unexplained:{snap.ticker}:{snap.asof.date().isoformat()}:{direction}",
            title=(
                f"[TAPE] {snap.ticker} {direction} {abs(snap.change_pct):.1f}% "
                f"with no matching news"
            ),
            url="",
            summary=(
                f"{snap.ticker} moved {snap.change_pct:+.2f}% "
                f"(session: {snap.session}"
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
                "change_pct": snap.change_pct,
                "volume_multiple": snap.volume_multiple,
                "session": snap.session,
                "forced_score": float(conf.get("unexplained_alert_score", 70)),
            },
        )


def current_session(now: datetime | None = None) -> str:
    """Which US session are we in? ET is UTC-5 / UTC-4; month-based approximation
    is accurate enough for labelling a news item."""
    now = now or datetime.now(timezone.utc)
    offset = 4 if 3 <= now.month <= 11 else 5
    et_dt = now - timedelta(hours=offset)
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


def _mean(values: list[float]) -> float:
    values = [v for v in values if v]
    return sum(values) / len(values) if values else 0.0
