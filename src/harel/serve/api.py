"""REST API - the machine-readable surface for the downstream LLM agent.

    pip install "harel-terminal[serve]"
    harel serve            # http://127.0.0.1:8787

Binds to loopback by default. This is a single-user system holding a trading
edge; there is no auth layer because there is no reason to expose it.
"""

from __future__ import annotations

from typing import Any

from ..config import get_config
from ..db import Database
from ..views import Views


def create_app(db_path: str | None = None):
    try:
        from fastapi import FastAPI, Query
        from fastapi.responses import HTMLResponse
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "FastAPI is not installed. Run: pip install 'harel-terminal[serve]'"
        ) from exc

    from .terminal import render_terminal

    db = Database(db_path)
    views = Views(db=db, config=get_config())

    app = FastAPI(
        title="Harel Terminal",
        version="1.0.0",
        description=(
            "News and regulatory terminal for a 22-name Israeli equity basket, "
            "ranked for short-term trading. Read /agent/manifest first."
        ),
    )

    @app.get("/", response_class=HTMLResponse)
    def terminal() -> str:
        return render_terminal(views)

    @app.get("/agent/manifest")
    def manifest() -> dict[str, Any]:
        """Self-description for the LLM agent: what exists and how to use it."""
        return AGENT_MANIFEST

    @app.get("/api/feed")
    def feed(
        tickers: str | None = Query(None, description="comma-separated"),
        min_score: float = 45.0,
        hours: float = 24.0,
        limit: int = 40,
        relations: str | None = None,
        events: str | None = None,
        reasons: bool = False,
    ) -> dict[str, Any]:
        return views.feed(
            tickers=_split(tickers), min_score=min_score, hours=hours, limit=limit,
            relations=_split(relations), events=_split(events), include_reasons=reasons,
        )

    @app.get("/api/brief/{ticker}")
    def brief(ticker: str, hours: float = 48.0, limit: int = 25) -> dict[str, Any]:
        return views.ticker_brief(ticker, hours=hours, limit=limit)

    @app.get("/api/search")
    def search(q: str, limit: int = 30, hours: float | None = None,
               tickers: str | None = None) -> dict[str, Any]:
        return views.search(q, limit=limit, hours=hours, tickers=_split(tickers))

    @app.get("/api/moving")
    def moving(min_abs_pct: float = 2.0) -> dict[str, Any]:
        return views.whats_moving(min_abs_pct=min_abs_pct)

    @app.get("/api/morning")
    def morning(hours: float = 16.0) -> dict[str, Any]:
        return views.morning_brief(hours=hours)

    @app.get("/api/item/{uid}")
    def item(uid: str) -> dict[str, Any]:
        return views.item(uid)

    @app.get("/api/calendar")
    def calendar(tickers: str | None = None, days: int = 45) -> dict[str, Any]:
        return views.calendar(_split(tickers), days=days)

    @app.get("/api/universe")
    def universe() -> dict[str, Any]:
        return views.universe()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return views.health()

    return app


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip().upper() for v in value.split(",") if v.strip()]


AGENT_MANIFEST: dict[str, Any] = {
    "purpose": (
        "Ranked news, regulatory and tape context for a fixed 22-name Israeli "
        "equity basket, filtered for what can move a price in the next few hours."
    ),
    "how_to_read_an_item": {
        "score": "0-100 materiality for a SHORT-TERM trader, not for a long-term thesis.",
        "tier": "ALERT >=75, HIGH 55-74, NORMAL 35-54. Below 35 is hidden by default.",
        "relation": {
            "DIRECT": "the company's own news - treat as fact about the issuer",
            "SUBSIDIARY": "a controlled entity; economically the same issuer",
            "PRODUCT_RIVAL": "same molecule / mechanism / design socket - read across",
            "CUSTOMER": "a customer's spend, which is our revenue",
            "PEER": "a named competitor's own news - sector sentiment, not our fact",
            "SECTOR_REG": "a regulator acting on our sector",
            "SECTOR_THEME": "a thematic story touching the sector",
        },
        "why": "plain-language justification for the link; quote it, do not invent one",
        "reasons": "the full scoring trace; use it to explain rankings",
        "corroboration": "number of independent sources carrying the same story",
    },
    "rules_for_the_agent": [
        "Never state that a PEER or SECTOR_* item is news about our company.",
        "Always cite `url` and `source`; never paraphrase a headline as fact.",
        "`corroboration` counts INDEPENDENT SOURCES, not documents. Prefer items "
        "with corroboration >= 2 when a single low-trust source claims something big.",
        "In /api/moving, `drivers` are stories that predate the closing bell and "
        "could have caused the move. `after_the_bell` published after the close - "
        "never present those as the cause of that day's move; they are the next "
        "session's catalyst.",
        "Check `coverage_warnings` in /api/morning before claiming 'no news'.",
        "An item with meta.kind == 'unexplained_move' means the tape moved and we "
        "found NO story - say so explicitly rather than inventing a cause.",
    ],
    "endpoints": {
        "/api/morning": "start here each session - overnight digest incl. TASE reports",
        "/api/feed": "ranked feed; filter by tickers, relations, events, score",
        "/api/brief/{ticker}": "one name: direct + indirect news, price, watch list, calendar",
        "/api/search": "SQLite FTS5 full-text over everything collected",
        "/api/moving": "price movers joined with their best explanation",
        "/api/item/{uid}": "full record incl. body and duplicate sources",
        "/api/calendar": "known upcoming catalysts",
        "/api/health": "source health, missing keys, unresolved tickers",
    },
    "event_types": [
        "merger_acquisition", "regulatory_decision_primary", "clinical_readout",
        "short_seller_report", "guidance_change", "equity_offering", "earnings",
        "major_contract", "litigation_outcome", "compliance_action", "index_event",
        "rating_change", "listing_compliance", "operational_disruption",
        "macro_sector_policy", "commodity_move", "partnership_licensing",
        "management_change", "capital_return", "insider_activity",
        "product_launch", "patent_grant", "conference_presentation",
        "award_recognition",
    ],
}
