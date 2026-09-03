"""US trading halts and volatility pauses.

The taxonomy in config/scoring.yaml scores twenty-odd event types and none of
them is a halt, and no source in config/sources.yaml could have produced one.
That is the largest hole in the event model for an intraday trader: a T12 - the
exchange stopping the tape because it has asked the company a question and not
yet had an answer - is the highest-information event that can happen to a
position, and it is the one event where being told late is worst, because the
name reopens with a gap and no opportunity to act in between.

NASDAQ publishes every halt and pause across the US tape, free, keyless, as
structured RSS with its own ``ndaq:`` namespace: symbol, market, reason code,
halt time to the millisecond, and the resumption quote and trade times as soon
as they are set.

The two families are deliberately not scored alike:

* NEWS AND REGULATORY halts (T1, T2, T12, H4, H9, H10, H11, D, ...) are the
  event. Trading is stopped because of something the trader does not yet know.
* VOLATILITY pauses (LUDP / LUDS) are five-minute limit-up-limit-down pauses
  and were 22 of the 42 records in a single session's feed. They report that
  the stock moved fast, which the tape already says; carried at the same weight
  they would drown the halts that matter. They are collected and capped.

Peers are matched as well as our own names, because a halt on a peer is a
sector event - "CyberArk halted, news pending" is information about PANW - and
the peer symbols are already in config/universe.yaml.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import datetime, timezone

from ..models import RawItem
from .base import Collector, register

# Reason codes where trading stopped because of INFORMATION. Sourced from
# NASDAQ's own halt-code table; the ones that matter for a single-name position.
_NEWS_CODES = {
    "T1":  "news pending",
    "T2":  "news released",
    "T5":  "single-stock trading pause - 10% price move",
    "T6":  "extraordinary market activity",
    "T8":  "ETF component halt",
    "T12": "additional information requested by the exchange",
    "H4":  "non-compliance with listing standards",
    "H9":  "not current in required filings",
    "H10": "SEC trading suspension",
    "H11": "regulatory concern",
    "D":   "security deletion / delisting",
    "M":   "corporate action / merger effective",
}
# Limit-up-limit-down pauses. Mechanical, five minutes, and very common.
_VOLATILITY_CODES = {"LUDP", "LUDS"}

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_FIELD = re.compile(r"<ndaq:(\w+)\s*/>|<ndaq:(\w+)>(.*?)</ndaq:\2>", re.S)


@register("trading_halts")
class TradingHaltsCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        symbols = self._symbols_of_interest()
        for url in self.source.feeds:
            try:
                resp = self.client.get(url)
            except Exception as exc:
                self.warn(f"{url}: {type(exc).__name__}: {exc}")
                continue
            if resp.not_modified:
                continue
            if not resp.ok:
                self.warn(f"{url}: HTTP {resp.status}")
                continue
            body = resp.text or ""
            if _is_bot_wall(body):
                # HTTP 200 carrying a JavaScript challenge instead of the feed.
                # This is the trap that must never be read as "no halts today":
                # an empty tape and a locked door look identical on the status
                # code, and only one of them means the market is calm.
                self.warn(
                    f"{url}: HTTP 200 but the body is an Incapsula bot "
                    f"challenge, not the feed - halts are NOT being seen. "
                    f"Lower the poll rate or fall back to the NYSE CSV at "
                    f"https://www.nyse.com/api/trade-halts/current/download"
                )
                self.save_state(last_error="bot challenge instead of feed")
                continue
            found = 0
            for raw in _ITEM.findall(body):
                found += 1
                try:
                    item = self._to_item(_fields(raw), symbols)
                except Exception as exc:
                    self.warn(f"unparseable halt record: {type(exc).__name__}: {exc}")
                    continue
                if item is not None:
                    yield item
            if not found and _during_us_session():
                # Outside market hours an empty halt feed is simply the truth,
                # and warning about it every overnight pass would train the
                # reader to ignore the one warning that matters. During the
                # session it is a real signal: nothing halted anywhere on the
                # US tape for a whole poll is unusual enough to look at.
                self.warn(f"{url}: parsed 0 halt records during the US session")

    def _symbols_of_interest(self) -> dict[str, tuple[str, str]]:
        """symbol -> (our ticker, relation). Includes peers, which is the point."""
        out: dict[str, tuple[str, str]] = {}
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            out[ticker.upper()] = (ticker, "DIRECT")
            for peer in tc.peers or []:
                # A bare exchange symbol only; entries like "HIK.L" or "X-FAB"
                # are foreign listings that never appear on the US halt tape.
                sym = str(peer).upper().strip()
                if sym.isalpha() and 1 <= len(sym) <= 5:
                    out.setdefault(sym, (ticker, "PEER"))
        return out

    def _to_item(self, f: dict[str, str],
                 symbols: dict[str, tuple[str, str]]) -> RawItem | None:
        symbol = (f.get("IssueSymbol") or "").upper().strip()
        if not symbol or symbol not in symbols:
            return None
        ticker, relation = symbols[symbol]
        code = (f.get("ReasonCode") or "").upper().strip()
        halted_at = _halt_datetime(f.get("HaltDate"), f.get("HaltTime"))
        if halted_at is None or halted_at < self.ctx.since:
            return None

        volatility = code in _VOLATILITY_CODES
        meaning = _NEWS_CODES.get(code, "volatility pause" if volatility
                                  else f"reason code {code}")
        resumption = " ".join(x for x in (f.get("ResumptionDate"),
                                          f.get("ResumptionTradeTime")) if x).strip()
        name = f.get("IssueName") or symbol
        verb = "paused" if volatility else "HALTED"
        title = f"[HALT] {symbol} {verb} - {meaning} ({code})"
        if relation == "PEER":
            title += f" - peer of {ticker}"
        return self.make_item(
            external_id=f"halt:{symbol}:{code}:{halted_at.isoformat()}",
            title=title,
            url="https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts",
            summary=(
                f"{name} ({symbol}) on {f.get('Market') or 'the US tape'} "
                f"{'paused' if volatility else 'halted'} at "
                f"{f.get('HaltTime') or '?'} ET on {f.get('HaltDate') or '?'}. "
                f"Reason {code}: {meaning}. "
                + (f"Resumption {resumption} ET." if resumption
                   else "No resumption time published yet - it is still halted.")
            ),
            published_at=halted_at,
            seed_tickers=[ticker],
            seed_relation=relation,
            meta={
                "kind": "trading_halt",
                "symbol": symbol,
                "reason_code": code,
                "reason": meaning,
                "market": f.get("Market"),
                "halt_time_et": f.get("HaltTime"),
                "resumption_et": resumption or None,
                "still_halted": not resumption,
                # Consumed by the noise cap in config/scoring.yaml. A
                # limit-up-limit-down pause is the tape repeating itself; a
                # news-pending halt is not.
                "form_type": "HALT-VOLATILITY" if volatility else "HALT-NEWS",
            },
        )


def _is_bot_wall(body: str) -> bool:
    """A challenge page served with HTTP 200 instead of the feed."""
    head = body[:1500].lower()
    return ("_incapsula_resource" in head
            or "noindex, nofollow" in head and "<rss" not in head
            or "enable javascript" in head)


def _during_us_session(now: datetime | None = None) -> bool:
    """Roughly 04:00-20:00 ET on a weekday - halts can print pre and post."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return True
    return et.weekday() < 5 and 4 <= et.hour < 20


def _fields(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _FIELD.finditer(raw):
        if m.group(1):                      # self-closing, e.g. <ndaq:Foo />
            out[m.group(1)] = ""
        else:
            out[m.group(2)] = html.unescape((m.group(3) or "").strip())
    return out


def _halt_datetime(date_part: str | None, time_part: str | None) -> datetime | None:
    """NASDAQ publishes ET wall-clock with no offset; read it as ET."""
    if not date_part:
        return None
    clock = (time_part or "00:00:00").split(".")[0] or "00:00:00"
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            naive = datetime.strptime(f"{date_part.strip()} {clock}", fmt)
            break
        except ValueError:
            continue
    else:
        return None
    try:
        from zoneinfo import ZoneInfo

        return naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(
            timezone.utc)
    except Exception:
        return naive.replace(tzinfo=timezone.utc)
