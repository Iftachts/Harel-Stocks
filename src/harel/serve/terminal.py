"""The terminal view.

Deliberately dense and keyboard-oriented: amber on black, monospace, no images,
no framework. A trader scans this, they do not browse it. Everything is rendered
server-side from the same :class:`Views` object the API and MCP server use.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from ..views import Views

TIER_COLOR = {
    "ALERT": "#ff4d4d",
    "HIGH": "#ffa500",
    "NORMAL": "#d9d9d9",
    "NOISE": "#6b6b6b",
}
RELATION_COLOR = {
    "DIRECT": "#ffb000",
    "SUBSIDIARY": "#ffb000",
    "PRODUCT_RIVAL": "#5fd7ff",
    "CUSTOMER": "#5fd7ff",
    "PEER": "#8fbf6f",
    "SUPPLIER": "#8fbf6f",
    "SECTOR_REG": "#c58fff",
    "SECTOR_THEME": "#909090",
    "MACRO": "#909090",
}

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #000; color: #d9d9d9;
  font: 13px/1.45 "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
}
header {
  position: sticky; top: 0; z-index: 5; background: #0a0a0a;
  border-bottom: 1px solid #333; padding: 8px 14px;
  display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap;
}
h1 { font-size: 14px; margin: 0; color: #ffb000; letter-spacing: 1px; }
.muted { color: #7a7a7a; }
main { padding: 12px 14px 60px; }
section { margin-bottom: 26px; }
h2 {
  font-size: 12px; color: #ffb000; text-transform: uppercase;
  letter-spacing: 1.5px; border-bottom: 1px solid #2a2a2a;
  padding-bottom: 4px; margin: 0 0 8px;
}
table { width: 100%; border-collapse: collapse; }
td, th { padding: 3px 8px 3px 0; vertical-align: top; text-align: left; }
th { color: #7a7a7a; font-weight: normal; font-size: 11px; }
tr.item:hover { background: #111; }
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; }
.score { font-weight: bold; text-align: right; width: 42px; }
.tkr { color: #ffb000; font-weight: bold; width: 56px; }
.rel { font-size: 10px; width: 108px; }
.tm { color: #6b6b6b; width: 118px; white-space: nowrap; }
.src { color: #6b6b6b; font-size: 11px; width: 150px; }
.why { color: #6b6b6b; font-size: 11px; }
.up { color: #4ade80; } .dn { color: #ff6b6b; }
.pill {
  display: inline-block; border: 1px solid #333; border-radius: 2px;
  padding: 0 5px; margin-right: 4px; font-size: 10px; color: #9a9a9a;
}
.warn {
  border-left: 3px solid #ffa500; background: #140f00;
  padding: 6px 10px; margin-bottom: 6px; color: #ffcf7a; font-size: 12px;
}
.empty { color: #6b6b6b; font-style: italic; padding: 6px 0; }
"""


def render_terminal(views: Views) -> str:
    brief = views.morning_brief(hours=24)
    feed = views.feed(min_score=40, hours=24, limit=60)
    moving = views.whats_moving(min_abs_pct=1.5)
    health = views.health()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>HAREL TERMINAL</title>",
        f"<style>{CSS}</style></head><body>",
        "<header>",
        "<h1>HAREL&nbsp;TERMINAL</h1>",
        f"<span class='muted'>{now}</span>",
        f"<span class='muted'>{health['db']['items']} items &middot; "
        f"{health['db']['alerts_24h']} alerts/24h &middot; "
        f"{health['sources_available']}/{health['sources_configured']} sources live</span>",
        "</header><main>",
    ]

    warnings = brief.get("coverage_warnings") or []
    if warnings:
        parts.append("<section><h2>Coverage warnings</h2>")
        parts += [f"<div class='warn'>{html.escape(w)}</div>" for w in warnings]
        parts.append("</section>")

    parts.append(_section("Alerts (last 24h)", brief["alerts"]))
    parts.append(_movers_section(moving["movers"]))
    parts.append(_section("Feed", feed["items"]))
    if brief.get("tase_overnight"):
        parts.append(_section("TASE / MAYA overnight", brief["tase_overnight"]))
    parts.append(_calendar_section(brief.get("calendar_next_7d") or []))

    parts.append("</main></body></html>")
    return "".join(parts)


def _section(title: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return (f"<section><h2>{html.escape(title)}</h2>"
                f"<div class='empty'>nothing yet - run `harel collect`</div></section>")

    rows = ["<table><tr><th class='score'>SC</th><th class='tkr'>SYM</th>"
            "<th class='rel'>REL</th><th class='tm'>TIME</th>"
            "<th>HEADLINE</th><th class='src'>SOURCE</th></tr>"]
    for it in items:
        tier_color = TIER_COLOR.get(it.get("tier", "NORMAL"), "#d9d9d9")
        rel = it.get("relation", "")
        rel_color = RELATION_COLOR.get(rel, "#909090")
        url = it.get("url") or "#"
        title_html = html.escape(it["title"])[:190]
        events = "".join(
            f"<span class='pill'>{html.escape(e)}</span>" for e in (it.get("events") or [])[:3]
        )
        corr = it.get("corroboration")
        corr_html = f"<span class='pill'>x{corr}</span>" if corr else ""
        why = html.escape(it.get("why") or "")
        rows.append(
            f"<tr class='item'>"
            f"<td class='score' style='color:{tier_color}'>{it['score']:.0f}</td>"
            f"<td class='tkr'>{html.escape(it.get('ticker', ''))}</td>"
            f"<td class='rel' style='color:{rel_color}'>{html.escape(rel)}</td>"
            f"<td class='tm'>{html.escape(_fmt_time(it['t']))}</td>"
            f"<td><a href='{html.escape(url)}' target='_blank' rel='noreferrer'>"
            f"{title_html}</a> {events}{corr_html}"
            f"<div class='why'>{why}</div></td>"
            f"<td class='src'>{html.escape(it['source'])}</td></tr>"
        )
    rows.append("</table>")
    return f"<section><h2>{html.escape(title)}</h2>{''.join(rows)}</section>"


def _movers_section(movers: list[dict[str, Any]]) -> str:
    if not movers:
        return ""
    rows = ["<table><tr><th class='tkr'>SYM</th><th class='score'>CHG</th>"
            "<th class='rel'>VOL</th><th>DRIVER</th></tr>"]
    for m in movers:
        cls = "up" if m["change_pct"] >= 0 else "dn"
        vol = f"{m['volume_multiple']:.1f}x" if m.get("volume_multiple") else "-"
        if m["drivers"]:
            top = m["drivers"][0]
            driver = (f"<a href='{html.escape(top.get('url') or '#')}' target='_blank' "
                      f"rel='noreferrer'>{html.escape(top['title'])[:150]}</a>")
        else:
            driver = "<span class='muted'>no matching news - flow, technical, or a gap in coverage</span>"
        rows.append(
            f"<tr class='item'><td class='tkr'>{html.escape(m['ticker'])}</td>"
            f"<td class='score {cls}'>{m['change_pct']:+.1f}%</td>"
            f"<td class='rel'>{vol}</td><td>{driver}</td></tr>"
        )
    rows.append("</table>")
    return f"<section><h2>Movers</h2>{''.join(rows)}</section>"


def _calendar_section(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    rows = ["<table><tr><th class='tm'>DATE</th><th class='tkr'>SYM</th>"
            "<th class='rel'>KIND</th><th>EVENT</th></tr>"]
    for e in entries:
        rows.append(
            f"<tr class='item'><td class='tm'>{html.escape(e['date'])}</td>"
            f"<td class='tkr'>{html.escape(e['ticker'])}</td>"
            f"<td class='rel'>{html.escape(e['kind'])}</td>"
            f"<td>{html.escape(e['label'])}</td></tr>"
        )
    rows.append("</table>")
    return f"<section><h2>Calendar (next 7 days)</h2>{''.join(rows)}</section>"


def _fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16]
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    return dt.strftime("%m-%d %H:%M")
