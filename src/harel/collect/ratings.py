"""Analyst rating actions - the gap this project priced at $200-400/month.

docs/LIMITATIONS.md ranks analyst actions as the largest single quality gap
against Bloomberg and concludes that closing it costs a Benzinga subscription,
because "every free source is either a scrape against the terms of use, hours
late, or very partial on small names". That was true of the sources it had
looked at. It is not true of this one.

StockAnalysis.com renders its ratings page from a plain JSON endpoint - the
SvelteKit data payload the page itself fetches - keyless, with no browser
User-Agent required, and it carries the whole action and not just a headline
about it: the firm, whether it is an upgrade / downgrade / initiation /
maintain, the rating on both sides, the price target on both sides, and a
timestamp to the second.

What that buys, measured on this basket rather than asserted:

    TSEM  2026-09-01 20:15:08  Initiates  Stifel Nicolaus  -> Buy   PT $270
    ALLT  2026-08-13 14:40:23  Maintains  Northland        -> Buy   PT 19 -> 17
    NYAX  2026-08-17 10:29:11  Upgrades   KBW              -> Buy   PT $75

The Stifel initiation reached the terminal before this collector existed, but
only as a Google News headline scoring 4.1 NOISE; the Northland target cut on
Allot - a small cap where an analyst action is often the only news of the day -
reached it not at all.

LIMITS, verified rather than assumed:

* Coverage is this source's coverage, and it is partial: 11 of the 22 resolve
  (TEVA TSEM NVMI CAMT NICE PERI NYAX ORA TATT ALLT PANW) and 11 return a
  ratings page that 404s - ICL, ESLT, CGEN, GILT, AUDC, LPSN, OPK, ORMP, KMDA,
  BWAY, KEN. That is not the same as "no analyst covers them", and the warning
  says so: Elbit is covered by several desks and simply is not here. Treat this
  as a source that closes the gap for the liquid half of the basket.
* A "Maintains" that moves no price target is the analyst restating a position
  nobody asked about. Those are emitted - they are corroboration, and the agent
  downstream can use them - but tagged ``RATING-ROUTINE`` so the noise cap in
  config/scoring.yaml holds them out of the ranked feed, exactly as routine
  Form 4s are held out.
* This is a secondary source: the broker published the note, StockAnalysis
  transcribed it. Hence trust 0.85 rather than 1.0 - above an aggregator
  rewriting a headline, below the issuer speaking.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

# Actions that are an event in themselves, whatever they do to the target.
_LOUD_ACTIONS = {"upgrades", "upgrade", "downgrades", "downgrade",
                 "initiates", "initiate", "initiated", "resumes", "resume"}

# How deep the devalue resolver will follow index references before giving up.
# The payload is a flat array of ~140 entries; anything past this is a cycle.
_MAX_DEPTH = 12


def _devalue(arr: list[Any], idx: Any, depth: int = 0) -> Any:
    """Resolve SvelteKit's ``devalue`` index-encoded payload.

    The endpoint does not return objects; it returns a flat array in which every
    value is an INDEX into that same array, so ``{"firm": 11}`` means "the firm
    is whatever lives at position 11". Nulls are encoded as -1. Written out
    rather than pulled from a dependency because it is nine lines and the
    alternative is a package that has to be trusted to parse a financial feed.
    """
    if depth > _MAX_DEPTH or not isinstance(idx, int) or idx < 0 or idx >= len(arr):
        return None
    value = arr[idx]
    if isinstance(value, dict):
        return {k: _devalue(arr, i, depth + 1) for k, i in value.items()}
    if isinstance(value, list):
        return [_devalue(arr, i, depth + 1) for i in value]
    return value


@register("analyst_ratings")
class AnalystRatingsCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        template = self.source.raw.get("per_ticker_url")
        if not template:
            self.warn("no per_ticker_url configured")
            return
        uncovered: list[str] = []
        for ticker in self.active_tickers:
            try:
                rows = self._ratings_for(ticker, template)
            except HttpError as exc:
                self.warn(f"{ticker}: {exc}")
                continue
            except Exception as exc:
                self.warn(f"{ticker}: unexpected {type(exc).__name__}: {exc}")
                continue
            if rows is None:
                uncovered.append(ticker)
                continue
            for row in rows:
                item = self._to_item(ticker, row)
                if item is not None:
                    yield item
        if uncovered:
            # One line, not one per name, and worded carefully. This is a gap in
            # THIS SOURCE, not in the market: stockanalysis.com/stocks/eslt/
            # ratings/ answers 404 while Elbit is covered by plenty of desks. The
            # endpoint still returns HTTP 200 for a missing page because SvelteKit
            # serves its error payload with a 200, so the absence has to be read
            # off the body. Saying "no analyst covers this" would be a false
            # statement about a real company, which is worse than saying nothing.
            self.warn("no ratings page at this source for "
                      + ", ".join(uncovered)
                      + " - these names may still be covered elsewhere")

    # -- fetch -------------------------------------------------------------- #
    def _ratings_for(self, ticker: str, template: str) -> list[dict[str, Any]] | None:
        """Rating rows for one name, or None when nobody covers it."""
        resp = self.client.get(template.format(ticker=ticker.lower(),
                                               TICKER=ticker.upper()))
        payload = resp.json() or {}
        for node in payload.get("nodes") or []:
            if not isinstance(node, dict) or node.get("type") != "data":
                continue
            arr = node.get("data")
            if not isinstance(arr, list) or not arr:
                continue
            root = _devalue(arr, 0)
            if not isinstance(root, dict) or "ratings" not in root:
                continue
            rows = root.get("ratings")
            if not isinstance(rows, list):
                return None
            return [r for r in rows if isinstance(r, dict) and r.get("firm")]
        return None

    # -- shape -------------------------------------------------------------- #
    def _to_item(self, ticker: str, row: dict[str, Any]) -> RawItem | None:
        firm = str(row.get("firm") or "").strip()
        action = str(row.get("action_rt") or "").strip()
        if not firm or not action:
            return None
        published = _row_datetime(row)
        if published is None or published < self.ctx.since:
            return None

        new_rating = str(row.get("rating_new") or "").strip()
        old_rating = str(row.get("rating_old") or "").strip()
        pt_now, pt_old = row.get("pt_now"), row.get("pt_old")
        currency = str(row.get("curr") or "USD").strip()

        # The verb matters to the taxonomy: config/scoring.yaml keys
        # rating_change off phrasings like "initiates ... at Buy" and
        # "price target raised", so the title is written in those terms rather
        # than in the source's own field names.
        bits = [f"{firm} {action.lower()} {ticker}"]
        if new_rating and old_rating and new_rating != old_rating:
            bits.append(f"from {old_rating} to {new_rating}")
        elif new_rating:
            bits.append(f"at {new_rating}")
        if pt_now is not None and pt_old is not None and pt_now != pt_old:
            direction = "raised" if _num(pt_now) > _num(pt_old) else "lowered"
            bits.append(f"- price target {direction} to {currency} {pt_now} "
                        f"(from {pt_old})")
        elif pt_now is not None:
            bits.append(f"- price target {currency} {pt_now}")

        routine = (action.lower() not in _LOUD_ACTIONS
                   and (pt_now is None or pt_now == pt_old))
        return self.make_item(
            # Keyed on the ACTION, not on its timestamp. The source published
            # the same Goldman Sachs target raise on PANW twice, seven seconds
            # apart (01:45:09 and 01:45:16), and a second-precision id made
            # those two different items - the same call twice at the top of the
            # feed. Date plus firm plus what was actually decided collapses a
            # re-publication while still admitting a genuine second action by
            # the same desk on the same day, which would differ in the action,
            # the rating or the target.
            external_id=(f"rating:{ticker}:{firm}:{published.date().isoformat()}"
                         f":{action.lower()}:{new_rating}:{pt_now}"),
            title="[RATING] " + " ".join(bits),
            url=f"https://stockanalysis.com/stocks/{ticker.lower()}/ratings/",
            summary=(f"{firm} {action.lower()} {ticker}. "
                     f"Rating {old_rating or '-'} -> {new_rating or '-'}. "
                     f"Price target {pt_old if pt_old is not None else '-'} -> "
                     f"{pt_now if pt_now is not None else '-'} {currency}. "
                     f"Analyst {row.get('analyst') or 'unnamed'}."),
            published_at=published,
            seed_tickers=[ticker],
            seed_relation="DIRECT",
            meta={
                "dataset": "analyst_ratings",
                "firm": firm,
                "action": action,
                "rating_old": old_rating or None,
                "rating_new": new_rating or None,
                "price_target_old": pt_old,
                "price_target_new": pt_now,
                "currency": currency,
                "analyst": row.get("analyst"),
                # Consumed by the noise cap in config/scoring.yaml, the same way
                # "4-ROUTINE" holds down compensation-plumbing Form 4s.
                "form_type": "RATING-ROUTINE" if routine else "RATING-ACTION",
            },
        )


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _row_datetime(row: dict[str, Any]) -> datetime | None:
    """The action's own timestamp, to the second where the source gives one.

    Both halves are ET wall-clock without an offset. They are read as US/Eastern
    and converted, because treating a 09:20 New York print as 09:20 UTC would
    place a pre-open downgrade five hours before it happened and let the recency
    decay treat it as older than it is.
    """
    date_part = str(row.get("date") or "").strip()
    if not date_part:
        return None
    time_part = str(row.get("time") or "00:00:00").strip() or "00:00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(f"{date_part} {time_part}", fmt)
            break
        except ValueError:
            continue
    else:
        try:
            naive = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            return None
    try:
        from zoneinfo import ZoneInfo

        return naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(
            timezone.utc)
    except Exception:
        return naive.replace(tzinfo=timezone.utc)
