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

from datetime import datetime, timezone
from typing import Any, Sequence

from .config import Config, get_config
from .db import Database

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
        out["corroboration"] = len(also) + 1
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
    ) -> dict[str, Any]:
        """The main ranked feed. Defaults are tuned for a day trader: last 24h,
        material items only."""
        rows = self.db.feed(
            tickers=tickers, min_score=min_score, since_hours=hours,
            limit=limit, relations=relations, events=events,
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
            top = self.db.feed(tickers=[ticker], min_score=30, since_hours=30, limit=3)
            movers.append({
                "ticker": ticker,
                "change_pct": round(price["change_pct"], 2),
                "volume_multiple": (round(price["vol_mult"], 2)
                                    if price.get("vol_mult") else None),
                "session": price.get("session"),
                "explained": bool(top),
                "drivers": [_compact(r) for r in top],
            })
        movers.sort(key=lambda m: abs(m["change_pct"]), reverse=True)
        return {"asof": datetime.now(timezone.utc).isoformat(), "movers": movers}

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
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "window_hours": hours,
            "alerts": [_compact(r, include_reasons=True) for r in alerts],
            "high": [_compact(r) for r in high[:20]],
            "tase_overnight": [_compact(r) for r in overnight_tase[:15]],
            "movers": self.whats_moving(min_abs_pct=2.5)["movers"][:10],
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
        for state in self.db.source_health():
            if (state.get("consecutive_failures") or 0) >= 3:
                warnings.append(
                    f"source '{state['source']}' has failed "
                    f"{state['consecutive_failures']} times: {state.get('last_error')}"
                )
        return warnings
