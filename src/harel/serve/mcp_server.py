"""MCP server - lets an LLM agent query the terminal directly as tools.

    pip install "harel-terminal[mcp]"
    harel mcp                       # speaks MCP over stdio

Register it with Claude Code / Claude Desktop:

    {
      "mcpServers": {
        "harel": { "command": "harel", "args": ["mcp"] }
      }
    }

The tool descriptions below are part of the product: they are what stops the
agent from reporting a competitor's clinical readout as our company's own news.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import get_config
from ..db import Database
from ..views import Views

AGENT_INSTRUCTIONS = """\
You are reading a news terminal covering a fixed basket of 22 Israeli-linked
equities, ranked for SHORT-TERM (intraday to a few days) trading.

Non-negotiable rules:
1. `relation` tells you whose news it is. DIRECT/SUBSIDIARY = our issuer.
   PRODUCT_RIVAL / PEER / CUSTOMER / SECTOR_* = somebody else's news that reads
   across to us. Never present the second kind as the first.
2. Cite `source` and `url`. Do not assert anything the item does not say.
3. `score` is materiality for a day trader, not importance in general.
   A patent grant scores low on purpose.
4. If `corroboration` is absent and the source trust is low (google_news,
   calcalist), say the story is single-sourced.
5. `coverage_warnings` lists sources that are off and tickers that are not being
   collected. Read it before you ever say "there is no news".
6. An item with meta.kind == "unexplained_move" means the tape moved and the
   system found no cause. Report that as an open question, not as a cause.

Start a session with `morning_brief`. Use `ticker_brief` before writing about
one name. Use `search` to check whether something already came up.
"""


def build_server(db_path: str | None = None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The MCP SDK is not installed. Run: pip install 'harel-terminal[mcp]'"
        ) from exc

    views = Views(db=Database(db_path), config=get_config())
    mcp = FastMCP("harel-terminal", instructions=AGENT_INSTRUCTIONS)

    @mcp.tool()
    def morning_brief(hours: float = 16.0) -> str:
        """Overnight digest: alerts, high-materiality items, TASE/MAYA reports
        filed during Israeli hours, price movers, and this week's catalysts.
        Call this first in any session."""
        return _json(views.morning_brief(hours=hours))

    @mcp.tool()
    def feed(
        tickers: str = "",
        min_score: float = 45.0,
        hours: float = 24.0,
        limit: int = 40,
        relations: str = "",
        events: str = "",
    ) -> str:
        """Ranked news feed.

        tickers:   comma-separated symbols, empty for the whole basket
        min_score: 75+ = ALERT, 55+ = HIGH, 35+ = NORMAL
        relations: filter, e.g. "DIRECT" for our own news only, or
                   "PRODUCT_RIVAL,PEER" for read-across only
        events:    filter by event type, e.g. "clinical_readout,equity_offering"
        """
        return _json(views.feed(
            tickers=_split(tickers), min_score=min_score, hours=hours,
            limit=limit, relations=_split(relations), events=_split(events),
            include_reasons=True,
        ))

    @mcp.tool()
    def ticker_brief(ticker: str, hours: float = 48.0) -> str:
        """Everything on one name: direct news, indirect/read-across news, price
        and volume context, the competitor and product watch list, and known
        upcoming catalysts."""
        return _json(views.ticker_brief(ticker, hours=hours))

    @mcp.tool()
    def search(query: str, limit: int = 30, hours: float = 0, tickers: str = "") -> str:
        """Full-text search over everything collected. SQLite FTS5 syntax:
        phrases in double quotes, OR / NOT, NEAR(a b, 5). hours=0 means no time
        limit."""
        return _json(views.search(
            query, limit=limit, hours=hours or None, tickers=_split(tickers)
        ))

    @mcp.tool()
    def whats_moving(min_abs_pct: float = 2.0) -> str:
        """Price movers joined with their best available explanation. Movers with
        `explained: false` had no matching story - flag those as open questions."""
        return _json(views.whats_moving(min_abs_pct=min_abs_pct))

    @mcp.tool()
    def get_item(uid: str) -> str:
        """Full record for one item: body text, the complete scoring trace, all
        ticker links, and the same story as carried by other sources."""
        return _json(views.item(uid))

    @mcp.tool()
    def calendar(tickers: str = "", days: int = 45) -> str:
        """Known upcoming catalysts: earnings dates, trial primary-completion
        dates, regulator comment deadlines, capacity auctions."""
        return _json(views.calendar(_split(tickers), days=days))

    @mcp.tool()
    def universe() -> str:
        """The covered basket, with sector, float class, TASE dual-listing status
        and each name's peer set. Also flags unresolved tickers."""
        return _json(views.universe())

    @mcp.tool()
    def health() -> str:
        """Source health, missing API keys, degraded collectors and unresolved
        tickers. Check this before concluding that a quiet feed means quiet news."""
        return _json(views.health())

    return mcp


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=None, default=str)


def _split(value: str) -> list[str] | None:
    if not value:
        return None
    return [v.strip().upper() for v in value.split(",") if v.strip()]


def main(db_path: str | None = None) -> None:  # pragma: no cover
    build_server(db_path).run()
