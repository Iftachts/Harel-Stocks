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
from ..views import RELATION_MEANING, Views


def create_app(db_path: str | None = None):
    try:
        from fastapi import FastAPI, Query
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "FastAPI is not installed. Run: pip install 'harel-terminal[serve]'"
        ) from exc

    class Utf8JSONResponse(JSONResponse):
        """Say the encoding out loud.

        The default is bare `application/json`, and a client that falls back to
        ISO-8859-1 when no charset is declared turns every Hebrew headline into
        mojibake - which is half of what this basket is for. The HTML terminal
        was fine only because it carries its own <meta charset>.
        """
        media_type = "application/json; charset=utf-8"

    from .terminal import render_item, render_sources, render_terminal

    db = Database(db_path)
    views = Views(db=db, config=get_config())

    app = FastAPI(
        title="Harel Terminal",
        version="1.0.0",
        description=(
            "News and regulatory terminal for a 22-name Israeli equity basket, "
            "ranked for short-term trading. Read /agent/manifest first."
        ),
        default_response_class=Utf8JSONResponse,
    )

    @app.get("/", response_class=HTMLResponse)
    def terminal() -> str:
        return render_terminal(views)

    @app.get("/item/{uid}", response_class=HTMLResponse)
    def item_page(uid: str) -> str:
        """The evidence behind one line of the feed, for a human who is going to
        check it before trading it."""
        return render_item(views, uid)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page() -> str:
        return render_sources(views)

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
        include_tape: bool = Query(
            False, description="include [TAPE] unexplained-move markers (see /api/moving)"),
    ) -> dict[str, Any]:
        return views.feed(
            tickers=_split(tickers), min_score=min_score, hours=hours, limit=limit,
            relations=_split(relations), events=_split(events), include_reasons=reasons,
            include_tape=include_tape,
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

    @app.get("/api/explain/{uid}")
    def explain(uid: str) -> dict[str, Any]:
        """Full audit of one item: provenance, timing, link rules, score trace,
        corroboration, tape context and outside verification links."""
        return views.explain(uid)

    @app.get("/api/sources")
    def sources() -> dict[str, Any]:
        return views.sources_report()

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
        # One definition, read by the manifest, the MCP instructions and the
        # drill-down page alike, so what the agent is told and what the trader
        # reads cannot drift apart.
        "relation": RELATION_MEANING,
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
        "When the user asks why an item is ranked where it is, why it is tagged "
        "with a ticker, or whether it can be trusted, call /api/explain/{uid} "
        "rather than reasoning from the headline. It carries the provenance, the "
        "scoring trace, the corroboration and outside links to verify it.",
        "Every item has a human page at /item/{uid}. Offer that link when the "
        "user says they want to check something themselves.",
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
        "/api/explain/{uid}": "the audit trail: which query found it, source trust, "
                              "publication time vs the bell, why it links to each "
                              "ticker, the score arithmetic, who else carried it, "
                              "the tape, and links to verify it elsewhere",
        "/api/sources": "every source, its trust, and when it last returned anything",
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
