"""Agent-facing views.

This is the contract the downstream LLM agent reads. Everything here is shaped
for *token efficiency and unambiguous grounding*:

* every item carries its `uid`, `url`, `source` and `published_at` so the agent
  can cite instead of paraphrase,
* every item carries `relation` and `why`, so the agent never mistakes a
  competitor's readout for our company's own news,
* every item carries `reasons`, so the agent can explain the ranking,
* cross-source duplicates are collapsed into `also[]`, and the count of
  independent confirmations is exposed as `corroboration`.

The same functions back the REST API, the MCP server and the CLI, so all three
surfaces cannot drift apart.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .config import Config, get_config
from .db import Database

MARKET_TZ = ZoneInfo("America/New_York")
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
MARKET_CLOSE_HOUR = 16

# What a trust weight actually means, in words. The number alone ("trust 0.60")
# tells a trader nothing about whether they should go and read the original.
TRUST_MEANING = [
    (0.95, "primary document - the issuer or the regulator itself"),
    (0.85, "first-hand, but not the issuer's own words"),
    (0.70, "reliable secondary reporting"),
    (0.00, "aggregator - somebody else's reporting, rewritten"),
]

# What each relation means, in one line. This is the canonical copy: the REST
# manifest and the MCP instructions both read it from here, so the explanation a
# trader sees and the explanation the agent is given can never drift apart.
RELATION_MEANING = {
    "DIRECT": "the company's own news - treat as fact about the issuer",
    "SUBSIDIARY": "a controlled entity; economically the same issuer",
    "PRODUCT_RIVAL": "same molecule / mechanism / design socket - read across",
    "CUSTOMER": "a customer's spend, which is our revenue",
    "PEER": "a named competitor's own news - sector sentiment, not our fact",
    "SUPPLIER": "an input we depend on",
    "SECTOR_REG": "a regulator acting on our sector",
    "SECTOR_THEME": "a thematic story touching the sector",
    "MACRO": "a market-wide condition, not a company fact",
}

# Free price feeds, and what each one is actually giving you.
PROVIDER_MEANING = {
    "yahoo": "Yahoo chart endpoint - unofficial and delayed (~15 min), "
             "includes pre/post-market prints",
    "stooq": "Stooq daily bar - end-of-day only, never intraday",
}


def last_session_close(now: datetime | None = None) -> datetime:
    """The most recent US equity close at or before ``now``, in UTC.

    Used to decide whether a story could have moved today's print. Mid-session
    this returns yesterday's close, so everything published today is eligible;
    after the bell it returns today's close, so a filing accepted at 16:13 ET
    is not offered as the explanation for a move that finished at 16:00.
    """
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(MARKET_TZ)
    close = et.replace(hour=MARKET_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    if et < close:
        close -= timedelta(days=1)
    while close.weekday() >= 5:          # roll back over Sat/Sun
        close -= timedelta(days=1)
    return close.astimezone(timezone.utc)


def _published_utc(row: dict[str, Any]) -> datetime | None:
    raw = row.get("published_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

MAX_SUMMARY_CHARS = 420


# --------------------------------------------------------------------------- #
def _compact(row: dict[str, Any], include_reasons: bool = False) -> dict[str, Any]:
    out = {
        "uid": row["uid"],
        "t": row["published_at"],
        "title": row["title"],
        "source": row["source"],
        "url": row.get("url") or "",
        "score": round(row.get("ticker_score") or row.get("score") or 0, 1),
        "tier": row.get("tier"),
        "events": row.get("events") or [],
    }
    if row.get("ticker"):
        out["ticker"] = row["ticker"]
        out["relation"] = row.get("relation")
        out["why"] = row.get("why")
    if row.get("tickers"):
        out["tickers"] = row["tickers"]
    summary = (row.get("summary") or "").strip()
    if summary:
        out["summary"] = summary[:MAX_SUMMARY_CHARS]
    meta = row.get("meta") or {}
    for key in ("form_type", "items", "item_labels", "nct_id", "status", "change",
                "agencies", "document_number", "public_inspection", "sponsor",
                "classification", "synthetic", "kind"):
        if meta.get(key):
            out.setdefault("meta", {})[key] = meta[key]
    also = row.get("also") or []
    if also:
        # Count independent SOURCES, not cluster members. Twelve Form 4s filed
        # the same afternoon are one source saying one thing twelve times, not
        # twelve confirmations - and the agent manifest tells the model to trust
        # this number when a single low-trust source claims something big.
        sources = {row.get("source")} | {a.get("source") for a in also}
        out["corroboration"] = len({s for s in sources if s})
        out["also"] = [{"source": a["source"], "url": a["url"]} for a in also[:5]]
    if include_reasons:
        out["reasons"] = row.get("reasons") or []
    return out


# --------------------------------------------------------------------------- #
class Views:
    def __init__(self, db: Database | None = None, config: Config | None = None) -> None:
        self.db = db or Database()
        self.config = config or get_config()

    # ------------------------------------------------------------- feed ---- #
    def feed(
        self,
        tickers: Sequence[str] | None = None,
        min_score: float = 45.0,
        hours: float = 24.0,
        limit: int = 40,
        relations: Sequence[str] | None = None,
        events: Sequence[str] | None = None,
        include_reasons: bool = False,
        include_tape: bool = False,
        max_per_ticker: int | None = 3,
    ) -> dict[str, Any]:
        """The main ranked feed. Defaults are tuned for a day trader: last 24h,
        material items only.

        Tape markers are excluded by default. "[TAPE] X up 7% with no matching
        news" is the *absence* of a story, carrying a forced score of 70, so on
        a busy tape six of them outranked every real headline - the Teva
        earnings story sat at 30 underneath them. They are not news and they
        already have a home in `whats_moving`, which is the view built to answer
        "what moved and why". Pass include_tape=True to see them here anyway.
        """
        rows = self.db.feed(
            tickers=tickers, min_score=min_score, since_hours=hours,
            limit=limit, relations=relations, events=events,
            include_tape=include_tape,
            # Only when scanning the whole basket. Asking for one name means you
            # want everything on it.
            max_per_ticker=None if tickers else max_per_ticker,
        )
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "window_hours": hours,
            "min_score": min_score,
            "count": len(rows),
            "items": [_compact(r, include_reasons) for r in rows],
        }

    # ----------------------------------------------------- ticker brief ---- #
    def ticker_brief(self, ticker: str, hours: float = 48.0,
                     limit: int = 25) -> dict[str, Any]:
        """Everything an agent needs to write a note on one name."""
        ticker = ticker.upper()
        tc = self.config.ticker(ticker)
        if tc is None:
            return {"error": f"{ticker} is not in the universe",
                    "universe": self.config.active_tickers}
        if tc.unresolved:
            return {"error": f"{ticker} is unresolved", "hint": tc.resolution_hint}

        rows = self.db.feed(tickers=[ticker], min_score=0, since_hours=hours,
                            limit=limit * 3)
        direct = [r for r in rows if r["relation"] in ("DIRECT", "SUBSIDIARY")]
        indirect = [r for r in rows if r["relation"] not in ("DIRECT", "SUBSIDIARY")]

        price = self.db.latest_price(ticker)
        bars = self.db.recent_bars(ticker, 6)

        return {
            "ticker": ticker,
            "name": tc.name,
            "sector": self.config.sector(tc.sector).label,
            "float_class": tc.float_class,
            "risk_flags": tc.risk_flags,
            "exchange": tc.exchange,
            "dual_listed_tase": bool(tc.tase_id),
            "asof": datetime.now(timezone.utc).isoformat(),
            "window_hours": hours,
            "price": price,
            "recent_bars": bars,
            "direct_news": [_compact(r) for r in direct[:limit]],
            "indirect_news": [_compact(r) for r in indirect[:limit]],
            "watch": {
                "peers": tc.peers,
                "peer_names": tc.peer_names[:12],
                "our_products": list(tc.products.keys()),
                "rival_products": tc.competitor_products[:12],
                "single_points_of_failure": tc.single_points_of_failure,
            },
            "calendar": self.db.calendar([ticker]),
        }

    # ----------------------------------------------------------- search ---- #
    def search(self, query: str, limit: int = 30, hours: float | None = None,
               tickers: Sequence[str] | None = None) -> dict[str, Any]:
        try:
            rows = self.db.search(query, limit=limit, since_hours=hours, tickers=tickers)
        except Exception as exc:
            return {"error": f"search failed: {exc}",
                    "hint": "FTS5 syntax: use quotes for phrases, OR / NOT / NEAR()"}
        return {"query": query, "count": len(rows), "items": [_compact(r) for r in rows]}

    # ------------------------------------------------------------ quote ---- #
    def quote(self, ticker: str) -> dict[str, Any] | None:
        """The stored print for one symbol, with its provenance attached.

        A percentage on a screen is a claim, and a day trader has to be able to
        reconcile it against their own broker. That needs three things we used to
        drop on the floor: which feed it came from, when we captured it, and what
        the previous close we divided by actually was.
        """
        row = self.db.latest_price(ticker)
        if not row:
            return None
        asof = _published_utc({"published_at": row.get("asof")})
        age_min = ((datetime.now(timezone.utc) - asof).total_seconds() / 60
                   if asof else None)
        provider = (row.get("provider") or "").strip()

        if provider == "stooq":
            freshness = f"end-of-day bar for {str(row.get('asof'))[:10]} - not an intraday price"
        elif age_min is None:
            freshness = "capture time unknown"
        elif provider:
            freshness = (f"captured {age_min:.0f} min ago; the feed itself is "
                         f"delayed on top of that")
        else:
            freshness = (f"captured {age_min:.0f} min ago; provider was not "
                         f"recorded for this print")

        return {
            "ticker": ticker.upper(),
            "last": row.get("last"),
            "prev_close": row.get("prev_close"),
            "change_pct": (round(row["change_pct"], 2)
                           if row.get("change_pct") is not None else None),
            "volume": row.get("volume"),
            "adv20": row.get("adv20"),
            "volume_multiple": (round(row["vol_mult"], 2)
                                if row.get("vol_mult") else None),
            "session": row.get("session"),
            "provider": provider or "unknown",
            "provider_note": PROVIDER_MEANING.get(
                provider, "provider not recorded (print predates provenance tracking)"),
            "asof": row.get("asof"),
            "age_minutes": round(age_min) if age_min is not None else None,
            "freshness": freshness,
            "math": (f"({row['last']} - {row['prev_close']}) / {row['prev_close']} "
                     f"= {row['change_pct']:+.2f}%"
                     if row.get("last") and row.get("prev_close")
                     and row.get("change_pct") is not None else None),
        }

    # ---------------------------------------------------- what's moving ---- #
    def whats_moving(self, min_abs_pct: float = 2.0) -> dict[str, Any]:
        """Price movers joined with their best explanation."""
        movers = []
        for ticker in self.config.active_tickers:
            price = self.db.latest_price(ticker)
            if not price or price.get("change_pct") is None:
                continue
            if abs(price["change_pct"]) < min_abs_pct:
                continue
            candidates = [
                # Must not be stricter than the feed's own threshold, or the
                # two panels contradict each other: TEVA showed "no matching
                # news" here while its guidance-change story sat in the feed
                # directly below at 27.
                r for r in self.db.feed(tickers=[ticker], min_score=20,
                                        since_hours=30, limit=8)
                # Our own "the tape moved and we found nothing" marker is not a
                # story. Offering it as the driver of the move it describes is
                # circular, and it is not an after-hours catalyst either.
                if (r.get("meta") or {}).get("kind") != "unexplained_move"
            ]
            # Written *because* the price moved. An effect cannot be offered as
            # the cause, so these leave the driver list entirely and are shown
            # for what they are. Collected below the driver threshold on
            # purpose: capping them as noise is what stops them ranking, and if
            # that also made them invisible the reader would go looking for the
            # article they can see on Twitter and conclude we had missed it.
            commentary = [
                r for r in self.db.feed(tickers=[ticker], min_score=0,
                                        since_hours=30, limit=12)
                if self._is_reactive(r)
            ]
            candidates = [r for r in candidates if not self._is_reactive(r)]

            # A story published after the closing bell cannot have caused the
            # move that bell ended. Keep it - it is tomorrow's catalyst - but
            # never offer it as this move's explanation. Which bell, though,
            # depends on the print we are explaining: see _driver_cutoff.
            cutoff = self._driver_cutoff(price)
            drivers = [r for r in candidates
                       if (p := _published_utc(r)) is None or p <= cutoff]
            after_bell = [r for r in candidates
                          if (p := _published_utc(r)) is not None and p > cutoff]
            # What the group did, and what is left over once you subtract it.
            tc = self.config.ticker(ticker)
            bench_sym = self.config.benchmark_for(tc.sector) if tc else None
            bench = self.db.latest_price(bench_sym) if bench_sym else None
            bench_pct = bench.get("change_pct") if bench else None
            relative = (round(price["change_pct"] - bench_pct, 2)
                        if bench_pct is not None else None)

            movers.append({
                "ticker": ticker,
                "change_pct": round(price["change_pct"], 2),
                # Where this percentage came from, so it can be checked against a
                # broker screen instead of taken on faith.
                "quote": self.quote(ticker),
                "benchmark": bench_sym,
                "benchmark_pct": round(bench_pct, 2) if bench_pct is not None else None,
                "relative_pct": relative,
                "volume_multiple": (round(price["vol_mult"], 2)
                                    if price.get("vol_mult") else None),
                "session": price.get("session"),
                "explained": bool(drivers),
                "drivers": [_compact(r) for r in drivers[:3]],
                "after_the_bell": [_compact(r) for r in after_bell[:3]],
                "post_move_commentary": [_compact(r) for r in commentary[:3]],
            })
        movers.sort(key=lambda m: abs(m["change_pct"]), reverse=True)
        return {"asof": datetime.now(timezone.utc).isoformat(), "movers": movers,
                "unexplained": self._unexplained_alerts(movers)}

    # A move this far clear of its sector, with nothing to point at, is the
    # question of the session - "what do they know?" - and it is not news, so
    # nothing in a news feed was ever going to raise it.
    UNEXPLAINED_RELATIVE_PCT = 3.0

    def _unexplained_alerts(self, movers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Moves that outran their sector with no verified catalyst.

        The feed is built from stories, so a day with no story produced no
        alert - however violently the tape moved. TSEM +6.2% against SOXX +1.2%,
        with nothing behind it and results four days out, is precisely what a
        short-term trader needs raised, and "0 alerts" was the wrong answer.

        Deliberately *not* scored: this is the absence of information, and a
        materiality score computed from an absent story would be fiction.
        """
        out = []
        for mover in movers:
            if mover["drivers"]:
                continue
            relative = mover.get("relative_pct")
            magnitude = abs(relative) if relative is not None else abs(mover["change_pct"])
            if magnitude < self.UNEXPLAINED_RELATIVE_PCT:
                continue

            bits = [f"{mover['ticker']} {mover['change_pct']:+.1f}%"]
            if relative is not None:
                bits.append(f"{relative:+.1f}pp vs {mover['benchmark']} "
                            f"({mover['benchmark_pct']:+.1f}%)")
            if mover.get("volume_multiple"):
                bits.append(f"{mover['volume_multiple']:.1f}x volume")
            out.append({
                "ticker": mover["ticker"],
                "kind": "unexplained_relative_move",
                "headline": "UNEXPLAINED RELATIVE MOVE - " + " | ".join(bits),
                "change_pct": mover["change_pct"],
                "relative_pct": relative,
                "volume_multiple": mover.get("volume_multiple"),
                "session": mover.get("session"),
                # Say what we looked at and did not find, so this reads as a
                # question rather than as a claim.
                "checked": "no company story above score 20 in the last 30h",
                "post_move_commentary": mover.get("post_move_commentary") or [],
                "next_catalyst": self._next_catalyst(mover["ticker"]),
            })
        out.sort(key=lambda a: abs(a.get("relative_pct") or a["change_pct"]), reverse=True)
        return out

    def _next_catalyst(self, ticker: str) -> dict[str, Any] | None:
        """The nearest known date. Positioning ahead of results is the most
        common benign explanation for a move nobody can source."""
        entries = self.db.calendar([ticker], days_ahead=21)
        return entries[0] if entries else None

    # --------------------------------------------------- morning brief ---- #
    def morning_brief(self, hours: float = 16.0) -> dict[str, Any]:
        """Overnight digest: what happened while the US market was shut.

        For this basket that is the single most valuable view, because the
        Israeli session and the TASE disclosure window both run while New York
        sleeps.
        """
        alerts = self.db.feed(min_score=self.config.scoring.tiers.get("alert", 75),
                              since_hours=hours, limit=20)
        high = self.db.feed(min_score=self.config.scoring.tiers.get("high", 55),
                            since_hours=hours, limit=40)
        high = [r for r in high if r["uid"] not in {a["uid"] for a in alerts}]

        overnight_tase = [
            r for r in self.db.feed(min_score=25, since_hours=hours, limit=60)
            if r["source"] == "maya_tase"
        ]
        moving = self.whats_moving(min_abs_pct=2.5)
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "window_hours": hours,
            "alerts": [_compact(r, include_reasons=True) for r in alerts],
            # Tape alerts are not news alerts and were never going to come out
            # of a news feed. A basket can be violently repriced on a day when
            # nobody publishes anything.
            "unexplained_moves": moving["unexplained"],
            "high": [_compact(r) for r in high[:20]],
            "tase_overnight": [_compact(r) for r in overnight_tase[:15]],
            "movers": moving["movers"][:10],
            "calendar_next_7d": self.db.calendar(days_ahead=7),
            "coverage_warnings": self._coverage_warnings(),
        }

    # ------------------------------------------------------------- item ---- #
    def item(self, uid: str) -> dict[str, Any]:
        row = self.db.item(uid)
        if not row:
            return {"error": f"no item {uid}"}
        return {
            "uid": row["uid"],
            "title": row["title"],
            "url": row["url"],
            "source": row["source"],
            "published_at": row["published_at"],
            "summary": row["summary"],
            "body": (row["body"] or "")[:20000],
            "events": row["events"],
            "score": row["score"],
            "tier": row["tier"],
            "reasons": row["reasons"],
            "meta": row["meta"],
            "tickers": row["tickers"],
            "same_story_from_other_sources": row["cluster"],
        }

    # ---------------------------------------------------------- explain ---- #
    def explain(self, uid: str) -> dict[str, Any]:
        """Everything behind one line on the screen — for a trader who is going
        to check it themselves before risking money on it.

        `item()` returns the record. This returns the *audit*: which query found
        it, how trusted that source is and why, what time it was published in all
        three time zones that matter, whether it landed before or after the bell,
        which rule attached it to which ticker, the arithmetic of the score, who
        else carried the story, what the tape was doing, and a set of outside
        links to verify the whole thing without us.

        Nothing here is recomputed. It is the stored trace, unpacked.
        """
        full = self.db.resolve_uid(uid)
        if not full:
            return {"error": f"no item matching '{uid}'",
                    "hint": "uids are sha1 hex; a unique prefix of 8+ chars is enough"}
        row = self.db.item(full)
        meta = row.get("meta") or {}
        src = self.config.sources.get(row["source"])
        trust = src.trust if src else None

        pub = _published_utc(row)
        collected = _published_utc({"published_at": row.get("collected_at")})
        close = last_session_close()

        timing: dict[str, Any] = {"published_utc": row.get("published_at")}
        if pub:
            timing.update({
                "published_et": pub.astimezone(MARKET_TZ).strftime("%Y-%m-%d %H:%M ET"),
                "published_israel": pub.astimezone(ISRAEL_TZ).strftime("%Y-%m-%d %H:%M IL"),
                "age_hours": round((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 1),
                "session_at_publication": _market_session(pub),
                "vs_last_close": (
                    f"published before the {close.astimezone(MARKET_TZ):%Y-%m-%d %H:%M} ET "
                    f"close - it can be a cause of that session's move"
                    if pub <= close else
                    f"published after the {close.astimezone(MARKET_TZ):%Y-%m-%d %H:%M} ET "
                    f"close - it is the next session's setup, and cannot have caused "
                    f"that session's move"
                ),
            })
        timing["first_seen_by_us_utc"] = row.get("collected_at")
        if pub and collected:
            # How long we were blind to it. This is the number that says whether
            # the system is fast enough to trade on, and nothing else reports it.
            timing["detection_lag_minutes"] = round(
                (collected - pub).total_seconds() / 60)

        links = [
            {
                "ticker": t["ticker"],
                "relation": t["relation"],
                "relation_means": RELATION_MEANING.get(t["relation"], ""),
                "confidence": t["confidence"],
                "why": t["why"],
                "score": round(t["score"], 1),
                "tier": self.config.scoring.tier_for(t["score"]),
            }
            for t in row.get("tickers") or []
        ]

        members = row.get("cluster") or []
        carriers = {row["source"]} | {m["source"] for m in members if m.get("source")}

        tiers = self.config.scoring.tiers
        return {
            "uid": row["uid"],
            "title": row["title"],
            "url": row.get("url"),
            "where_it_came_from": {
                "source": row["source"],
                "source_label": src.label if src else row["source"],
                "collector": row.get("source_kind"),
                "trust": trust,
                "trust_means": _trust_meaning(trust),
                "typical_latency": src.latency if src else None,
                # The single most useful line for "why am I seeing this": the
                # exact query or feed that pulled it in.
                "found_by": (meta.get("feed_label") or meta.get("query")
                             or (src.label if src else row["source"])),
                "feed_url": meta.get("feed"),
                "publisher": meta.get("publisher"),
                "id_at_source": row.get("external_id"),
            },
            "when": timing,
            "who_it_is_about": links,
            "how_it_scored": {
                "score": round(row.get("score") or 0, 1),
                "tier": row.get("tier"),
                "thresholds": {"ALERT": tiers.get("alert", 75),
                               "HIGH": tiers.get("high", 55),
                               "NORMAL": tiers.get("normal", 35)},
                "events": row.get("events") or [],
                "trace": _trace(row.get("reasons") or []),
                "note": "the stored trace, in order. Multipliers compound; "
                        "'+' lines are added after; a cap overrides everything.",
            },
            "who_else_carried_it": {
                "corroboration": len(carriers),
                "counts": "distinct SOURCES, not documents",
                "members": [
                    {"source": m["source"], "title": m["title"], "url": m.get("url"),
                     "published_at": m.get("published_at"), "uid": m["uid"]}
                    for m in members
                ],
            },
            "what_the_tape_did": [
                q for q in (self.quote(t["ticker"]) for t in links) if q
            ],
            "check_it_yourself": self._verify_links(row, [t["ticker"] for t in links]),
            "raw": {
                "summary": row.get("summary"),
                "body_excerpt": (row.get("body") or "")[:4000],
                "meta": meta,
            },
        }

    # ------------------------------------------------- driver plumbing ---- #
    def _driver_cutoff(self, price: dict[str, Any]) -> datetime:
        """The latest a story can have been published and still explain *this*
        print.

        The bug this fixes: the cutoff was always the last completed close, but
        the price on screen is whatever the last snapshot holds. Mid-session
        that snapshot is *today's* live move, so a story published at 13:40 ET
        today - hours before the print it is being compared with - was stamped
        "after the bell, not a cause". The bell in question had not rung.

        While a session is running, everything published up to now precedes the
        current print. Only once trading is over does "after the close" mean
        anything.
        """
        if str(price.get("session") or "").lower() in ("premarket", "regular"):
            return datetime.now(timezone.utc)
        return last_session_close()

    def _is_reactive(self, row: dict[str, Any]) -> bool:
        """Was this written because the price moved?

        Checked against the config patterns at read time rather than trusting
        `meta.reactive_recap` alone, so it applies to everything already stored
        instead of only to what has been re-scored since.
        """
        if (row.get("meta") or {}).get("reactive_recap"):
            return True
        title = row.get("title") or ""
        return any(p.pattern.search(title) for p in self.config.scoring.reactive_patterns)

    def _verify_links(self, row: dict[str, Any], tickers: Sequence[str]) -> list[dict[str, str]]:
        """Outside places to confirm this, none of which are us."""
        from urllib.parse import quote_plus

        out: list[dict[str, str]] = []
        if row.get("url"):
            out.append({
                "label": "the original document",
                "url": row["url"],
                "checks": "that it says what our headline says it says",
            })
        title = (row.get("title") or "")[:120]
        if title:
            out.append({
                "label": "this headline on Google News",
                "url": f"https://news.google.com/search?q={quote_plus(title)}",
                "checks": "who else is carrying it, and who had it first",
            })
        for ticker in dict.fromkeys(tickers):
            tc = self.config.ticker(ticker)
            if tc and tc.cik10:
                out.append({
                    "label": f"{ticker} filings on SEC EDGAR",
                    "url": ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                            f"&CIK={tc.cik10}&type=&dateb=&owner=include&count=40"),
                    "checks": "whether there is a filing behind the story, and its exact time",
                })
            if tc and tc.tase_id:
                out.append({
                    "label": f"{ticker} immediate reports on MAYA (TASE)",
                    "url": f"https://maya.tase.co.il/company/{tc.tase_id}?view=reports",
                    "checks": "the Hebrew disclosure, which is often hours ahead of the US wire",
                })
            out.append({
                "label": f"{ticker} quote",
                "url": f"https://finance.yahoo.com/quote/{ticker}",
                "checks": "our price and volume numbers against a second screen",
            })
        return out

    # --------------------------------------------------------- calendar ---- #
    def calendar(self, tickers: Sequence[str] | None = None,
                 days: int = 45) -> dict[str, Any]:
        return {"entries": self.db.calendar(tickers, days_ahead=days)}

    # --------------------------------------------------------- universe ---- #
    def universe(self) -> dict[str, Any]:
        out = []
        for ticker in sorted(self.config.universe):
            tc = self.config.universe[ticker]
            out.append({
                "ticker": ticker,
                "name": tc.name,
                "sector": tc.sector,
                "enabled": tc.enabled and not tc.unresolved,
                "unresolved": tc.unresolved,
                "hint": tc.resolution_hint or None,
                "float_class": tc.float_class,
                "dual_listed_tase": bool(tc.tase_id),
                "peers": tc.peers[:8],
            })
        return {"count": len(out), "tickers": out}

    # ----------------------------------------------------------- health ---- #
    def health(self) -> dict[str, Any]:
        states = self.db.source_health()
        degraded = [
            s for s in states
            if (s.get("consecutive_failures") or 0) >= 2 or s.get("last_error")
        ]
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "db": self.db.counts(),
            "sources_configured": len(self.config.sources),
            "sources_available": sum(1 for s in self.config.sources.values() if s.available),
            "missing_api_keys": [
                {"source": k, "env_var": v} for k, v in self.config.missing_keys()
            ],
            "running_degraded": [
                {"source": k, "env_var": v} for k, v in self.config.degraded_sources()
            ],
            "disabled_sources": [
                {"source": s.key, "label": s.label, "note": s.raw.get("notes", "").strip()}
                for s in self.config.sources.values() if not s.enabled
            ],
            "unresolved_tickers": [
                {"ticker": t, "hint": self.config.universe[t].resolution_hint}
                for t in self.config.unresolved_tickers
            ],
            "degraded_sources": degraded,
            "source_state": states,
        }

    # ---------------------------------------------------- source report ---- #
    def sources_report(self) -> dict[str, Any]:
        """Every configured source, what it is, and when it last actually worked.

        `health()` answers "is anything broken". This answers the question a
        trader asks instead: "did the system even look?" - which is a different
        question, and the one that decides whether an empty screen means quiet.
        """
        # Two kinds of row live in `source_state`: one per source (written by the
        # pipeline) and one per feed URL (written by the RSS collector, keyed
        # "<source>:<url>"). Summing them double-counts, so the source-level row
        # is the record and the per-URL rows only contribute failure detail.
        by_key: dict[str, dict[str, Any]] = {}
        for state in self.db.source_health():
            key, sep, _url = str(state["source"]).partition(":")
            entry = by_key.setdefault(key, {"feeds": 0, "items_last_run": 0,
                                            "failing": 0, "last_ok_at": None,
                                            "last_run_at": None, "last_error": None})
            if sep:
                entry["feeds"] += 1
                if (state.get("consecutive_failures") or 0) >= 3:
                    entry["failing"] += 1
                    entry["last_error"] = state.get("last_error")
                continue
            entry["items_last_run"] = int(state.get("items_last_run") or 0)
            entry["last_ok_at"] = state.get("last_ok_at")
            entry["last_run_at"] = state.get("last_run_at")
            if state.get("last_error"):
                entry["last_error"] = state.get("last_error")

        lags = self.db.detection_lag(hours=6)
        out = []
        for key, source in sorted(self.config.sources.items()):
            seen = by_key.get(key, {})
            lag = lags.get(key) or {}
            note = " ".join((source.raw.get("notes") or "").split())
            out.append({
                "source": key,
                "label": source.label,
                "collector": source.kind,
                "trust": source.trust,
                "trust_means": _trust_meaning(source.trust),
                "latency": source.latency,
                "enabled": source.enabled,
                "available": source.available,
                "degraded": source.degraded,
                "requires_key": source.requires,
                "endpoints_tracked": seen.get("feeds", 0),
                "items_last_run": seen.get("items_last_run", 0),
                "last_ok_at": seen.get("last_ok_at"),
                "last_run_at": seen.get("last_run_at"),
                "failing_endpoints": seen.get("failing", 0),
                "last_error": seen.get("last_error"),
                # How stale this source's news is by the time we can act on it.
                # The single most important number for deciding whether to trade
                # off a source or merely to read it.
                "median_lag_minutes": lag.get("median_minutes"),
                "p90_lag_minutes": lag.get("p90_minutes"),
                "lag_sample": lag.get("items", 0),
                "note": note[:400],
            })
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "sources": out,
            "warnings": self._coverage_warnings(),
        }

    # --------------------------------------------------------- internal ---- #
    def _coverage_warnings(self) -> list[str]:
        warnings: list[str] = []
        for key, env in self.config.missing_keys():
            warnings.append(f"source '{key}' is off: {env} is not set")
        for key, env in self.config.degraded_sources():
            warnings.append(
                f"source '{key}' is running on an unofficial fallback endpoint "
                f"because {env} is not set - it may break without notice"
            )
        for ticker in self.config.unresolved_tickers:
            warnings.append(
                f"ticker '{ticker}' is unresolved and is NOT being collected - "
                f"{self.config.universe[ticker].resolution_hint}"
            )
        # A source switched off in config disappeared from this panel entirely,
        # so "24/29 sources live" could not be reconciled with what was on
        # screen. A source that is off on purpose still has to be visible - the
        # whole point of this panel is that silence and blindness look different.
        for source in self.config.sources.values():
            if source.enabled:
                continue
            note = " ".join((source.raw.get("notes") or "").split())
            reason = note.split("OFF:", 1)[-1].strip() if "OFF:" in note else note
            warnings.append(
                f"source '{source.key}' is disabled in config"
                + (f": {reason[:180]}" if reason else "")
            )
        # Failure counters are keyed "<source>:<url>" and survive a config edit,
        # so a feed we have since fixed or switched off keeps reporting its old
        # failures for ever. A warning nobody can act on trains you to ignore
        # the panel, so only complain about feeds we are still actually polling.
        live_urls = {u for s in self.config.sources.values() if s.enabled for u in s.feeds}
        live_urls |= {u for t in self.config.active_tickers
                      if self.config.ticker(t) for u in self.config.ticker(t).ir_feeds}

        for state in self.db.source_health():
            if (state.get("consecutive_failures") or 0) < 3:
                continue
            key = str(state["source"])
            src_key, _, url = key.partition(":")
            source = self.config.sources.get(src_key)
            if source is None or not source.enabled:
                continue                      # gone or already reported as disabled
            if url and url not in live_urls:
                continue                      # this URL is no longer configured
            warnings.append(
                f"source '{key}' has failed "
                f"{state['consecutive_failures']} times: {state.get('last_error')}"
            )
        return warnings


# --------------------------------------------------------------------------- #
def _trust_meaning(trust: float | None) -> str:
    if trust is None:
        return "unknown source - not in config/sources.yaml"
    for floor, meaning in TRUST_MEANING:
        if trust >= floor:
            return meaning
    return ""


def _market_session(dt: datetime) -> str:
    """Which US session a timestamp fell in. Uses the real America/New_York
    rules rather than a month-based DST guess, because 'pre-market or not' is
    the difference between a gap and a nothing."""
    et = dt.astimezone(MARKET_TZ)
    if et.weekday() >= 5:
        return "weekend"
    minutes = et.hour * 60 + et.minute
    if minutes < 4 * 60:
        return "overnight"
    if minutes < 9 * 60 + 30:
        return "pre-market"
    if minutes < 16 * 60:
        return "regular session"
    if minutes < 20 * 60:
        return "after hours"
    return "overnight"


_TICKER_PREFIX = re.compile(r"^\[([A-Z0-9.\-]{1,8})\]\s*(.*)$")


def _step(text: str) -> dict[str, str]:
    """Label one line of the scoring trace so a reader can see the shape of the
    arithmetic without parsing it: what it started from, what scaled it, what was
    added, and what overrode the lot."""
    if text.startswith("+"):
        kind = "add"
    elif "cap" in text.lower():
        kind = "cap"
    elif "base=" in text:
        kind = "base"
    elif re.search(r"x\d", text):
        kind = "multiply"
    else:
        kind = "note"
    return {"kind": kind, "step": text}


def _trace(reasons: list[str]) -> dict[str, Any]:
    """Split the flat `reasons` list back into the item-wide steps and the
    per-ticker steps it was built from."""
    item_steps: list[dict[str, str]] = []
    per_ticker: dict[str, list[dict[str, str]]] = {}
    for reason in reasons:
        match = _TICKER_PREFIX.match(reason)
        if match:
            per_ticker.setdefault(match.group(1), []).append(_step(match.group(2)))
        else:
            item_steps.append(_step(reason))
    return {"item": item_steps, "per_ticker": per_ticker}
