"""The terminal view.

Deliberately dense and keyboard-oriented: amber on black, monospace, no images,
no framework. A trader scans this, they do not browse it. Everything is rendered
server-side from the same :class:`Views` object the API and MCP server use.
"""

from __future__ import annotations

import html
import json
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
.chg { font-weight: bold; text-align: right; width: 118px; }
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
.tape {
  border-left: 3px solid #ff4d4d; background: #170a0a;
  padding: 6px 10px; margin-bottom: 6px; color: #ffb3b3; font-size: 12px;
}
.dig {
  color: #5fd7ff; font-size: 10px; border: 1px solid #24404a;
  border-radius: 2px; padding: 0 4px; margin-left: 6px; white-space: nowrap;
}
.dig:hover { background: #0d2028; text-decoration: none; }
nav a { color: #7a7a7a; margin-right: 12px; }
nav a:hover { color: #ffb000; }

/* --- drill-down ------------------------------------------------------- */
.kv { width: 100%; margin-bottom: 4px; }
.kv td { padding: 2px 10px 2px 0; }
.kv td.k { color: #7a7a7a; width: 150px; white-space: nowrap; vertical-align: top; }
.big { font-size: 15px; color: #e8e8e8; margin: 4px 0 2px; }
.trace td { padding: 1px 10px 1px 0; }
.trace td.op { width: 26px; text-align: right; color: #7a7a7a; }
.op-base { color: #ffb000; } .op-add { color: #4ade80; } .op-cap { color: #ff6b6b; }
.total { color: #ffb000; font-weight: bold; }
.note { color: #6b6b6b; font-size: 11px; margin: 4px 0 0; }
pre.raw {
  white-space: pre-wrap; word-break: break-word; color: #9a9a9a;
  background: #0a0a0a; border: 1px solid #1e1e1e; padding: 8px;
  max-height: 320px; overflow: auto; margin: 0 0 8px;
}
.check td { padding: 3px 10px 3px 0; }
.ok { color: #4ade80; } .bad { color: #ff6b6b; } .off { color: #6b6b6b; }
"""


def render_terminal(views: Views) -> str:
    brief = views.morning_brief(hours=24)
    # 40 was reachable only because the [TAPE] markers carried a forced 70 and
    # sat in the feed. With those moved to the movers board where they belong,
    # genuine news peaks in the low 30s once recency decay has run for a few
    # hours - so a threshold of 40 renders an empty panel on any day that is not
    # breaking live, which is exactly when you would distrust the whole tool.
    # 20 matches the RUNBOOK's own advice for a quiet tape.
    feed = views.feed(min_score=20, hours=24, limit=60)
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
        "<nav><a href='/sources'>sources</a>"
        "<a href='/agent/manifest'>manifest</a>"
        "<a href='/api/morning'>json</a></nav>",
        "</header><main>",
    ]

    warnings = brief.get("coverage_warnings") or []
    if warnings:
        parts.append("<section><h2>Coverage warnings</h2>")
        # The "why" behind a disabled source is a paragraph, and eight of them
        # pushed the actual news below the fold. Keep the headline reason here;
        # the full note lives in config/sources.yaml and /api/health.
        for w in warnings:
            short = w if len(w) <= 120 else w[:117].rstrip(" ,.;-") + "..."
            parts.append(f"<div class='warn'>{html.escape(short)}</div>")
        parts.append("</section>")

    # Tape alerts come first. A move that outran its sector with nothing behind
    # it is the most urgent thing on the page precisely BECAUSE there is no
    # story - and a news feed, by construction, could never raise it.
    parts.append(_unexplained_section(brief.get("unexplained_moves") or []))

    empty_alerts = (
        "no news alerts in the last 24h"
        if health["db"]["items"] else "database is empty - run `harel collect`"
    )
    parts.append(_section("News alerts (last 24h)", brief["alerts"], empty=empty_alerts))
    parts.append(_movers_section(moving["movers"]))
    parts.append(_section(
        "Feed", feed["items"],
        empty=("nothing above score 20 in the last 24h"
               if health["db"]["items"] else "database is empty - run `harel collect`"),
    ))
    if brief.get("tase_overnight"):
        parts.append(_section("TASE / MAYA overnight", brief["tase_overnight"]))
    parts.append(_calendar_section(brief.get("calendar_next_7d") or []))

    parts.append("</main></body></html>")
    return "".join(parts)


def _section(title: str, items: list[dict[str, Any]], empty: str | None = None) -> str:
    if not items:
        # "run `harel collect`" was printed whenever a panel was empty, so a
        # working system with a quiet tape accused itself of never having
        # collected. Distinguishing the two is the entire point of this page.
        return (f"<section><h2>{html.escape(title)}</h2>"
                f"<div class='empty'>{html.escape(empty or 'nothing to show')}"
                f"</div></section>")

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
            f"<td class='tm'>{_time_cell(it)}</td>"
            f"<td><a href='{html.escape(url)}' target='_blank' rel='noreferrer'>"
            f"{title_html}</a> {events}{corr_html}{_dig(it.get('uid'))}"
            f"<div class='why'>{why}</div></td>"
            f"<td class='src'>{html.escape(it['source'])}</td></tr>"
        )
    rows.append("</table>")
    return f"<section><h2>{html.escape(title)}</h2>{''.join(rows)}</section>"


def _unexplained_section(alerts: list[dict[str, Any]]) -> str:
    """The tape moved and we found nothing. Stated as a question, not a cause."""
    if not alerts:
        return ""
    rows = []
    for a in alerts:
        catalyst = a.get("next_catalyst")
        tail = html.escape(a["checked"])
        if catalyst and catalyst.get("strength") == "company":
            tail += (f"<br>next known catalyst: <b>{html.escape(catalyst['date'])}</b> "
                     f"{html.escape(catalyst['label'][:70])} &mdash; positioning ahead "
                     f"of it is possible but unverified")
        elif catalyst:
            tail += (f"<br>{html.escape(catalyst.get('caveat', 'weak link'))}: "
                     f"{html.escape(catalyst['date'])} "
                     f"{html.escape(catalyst['label'][:70])}")
        rows.append(
            f"<div class='tape'><b>{html.escape(a['headline'])}</b>"
            f"<div class='why'>{tail.lstrip(' &middot;')}</div></div>"
        )
    return ("<section><h2>Unexplained moves</h2>" + "".join(rows)
            + "<p class='note'>These are questions, not findings. The tape moved "
              "clear of its sector and nothing we read explains it: check the "
              "order book, options flow and any pending catalyst before "
              "assuming there is news we missed.</p></section>")


def _movers_section(movers: list[dict[str, Any]]) -> str:
    if not movers:
        return ""
    rows = ["<table><tr><th class='tkr'>SYM</th><th class='chg'>CHG</th>"
            "<th class='rel'>VOL</th><th class='rel'>vs SECTOR</th>"
            "<th>DRIVER</th></tr>"]
    for m in movers:
        cls = "up" if m["change_pct"] >= 0 else "dn"
        vol = f"{m['volume_multiple']:.1f}x" if m.get("volume_multiple") else "-"
        # Where the percentage came from. A trader reconciling against their own
        # screen needs to know it is a delayed Yahoo print, not a live quote.
        provenance = f"<div class='why'>{html.escape(_quote_label(m.get('quote')))}</div>"

        rel_pct = m.get("relative_pct")
        if rel_pct is None:
            rel_cell = "<span class='muted'>-</span>"
        else:
            rel_cls = "up" if rel_pct >= 0 else "dn"
            rel_cell = (f"<span class='{rel_cls}'>{rel_pct:+.1f}pp</span>"
                        f"<div class='why'>{html.escape(str(m.get('benchmark') or ''))} "
                        f"{m.get('benchmark_pct', 0):+.1f}%</div>")

        if m["drivers"]:
            top = m["drivers"][0]
            driver = (f"<a href='{html.escape(top.get('url') or '#')}' target='_blank' "
                      f"rel='noreferrer'>{html.escape(top['title'])[:150]}</a>"
                      f"{_dig(top.get('uid'))}")
        elif rel_pct is not None and abs(rel_pct) < 2.0:
            # Most of the move is the group. Saying "no matching news" here
            # invites you to hunt for a company story that does not exist.
            driver = ("<span class='muted'>tracks its sector - "
                      f"{html.escape(str(m.get('benchmark') or ''))} "
                      f"{m.get('benchmark_pct', 0):+.1f}%, no stock-specific news"
                      "</span>")
        else:
            driver = "<span class='muted'>no matching news - flow, technical, or a gap in coverage</span>"
        # Published after the bell: cannot explain today's move, but it is the
        # next session's setup, so show it rather than dropping it.
        for late in (m.get("after_the_bell") or [])[:1]:
            driver += (
                f"<div class='why'>after the bell &middot; not a cause of this move: "
                f"<a href='{html.escape(late.get('url') or '#')}' target='_blank' "
                f"rel='noreferrer'>{html.escape(late['title'])[:120]}</a></div>"
            )
        # Written because the price moved. Shown, because a trader will find it
        # anyway and needs to know we classified it rather than missed it.
        for recap in (m.get("post_move_commentary") or [])[:1]:
            driver += (
                f"<div class='why'>post-move commentary &middot; reactive, not a catalyst: "
                f"<a href='{html.escape(recap.get('url') or '#')}' target='_blank' "
                f"rel='noreferrer'>{html.escape(recap['title'])[:120]}</a>"
                f"{_dig(recap.get('uid'))}</div>"
            )
        rows.append(
            f"<tr class='item'><td class='tkr'>{html.escape(m['ticker'])}</td>"
            f"<td class='chg {cls}'>{m['change_pct']:+.1f}%{provenance}</td>"
            f"<td class='rel'>{vol}</td><td class='rel'>{rel_cell}</td>"
            f"<td>{driver}</td></tr>"
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


# --------------------------------------------------------------------------- #
# Drill-down: one item, all of its evidence.
#
# The feed is a summary, and a summary is a claim. This page is the claim's
# working: which query found it, how much that source is trusted and why, when it
# was published against the bell, which rule attached it to which symbol, the
# arithmetic of the score, who else carried it, what the tape did - and a set of
# outside links so the whole thing can be checked without us.
# --------------------------------------------------------------------------- #
def render_item(views: Views, uid: str) -> str:
    data = views.explain(uid)
    if data.get("error"):
        return _document("NOT FOUND", "", (
            f"<section><h2>Not found</h2><div class='empty'>"
            f"{html.escape(data['error'])}</div>"
            f"<p class='note'>{html.escape(data.get('hint', ''))}</p></section>"
        ))

    origin = data["where_it_came_from"]
    when = data["when"]
    scored = data["how_it_scored"]
    parts: list[str] = []

    url = data.get("url") or "#"
    parts.append(
        f"<section><div class='big'><a href='{html.escape(url)}' target='_blank' "
        f"rel='noreferrer'>{html.escape(data['title'])}</a></div>"
        f"<div class='muted'>{html.escape(data['uid'])}</div></section>"
    )

    trust = origin.get("trust")
    parts.append(_kv("Where it came from", [
        ("source", f"{html.escape(origin['source'])} &middot; "
                   f"{html.escape(origin.get('source_label') or '')}"),
        ("trust", f"{trust if trust is not None else '?'} &mdash; "
                  f"<span class='muted'>{html.escape(origin.get('trust_means') or '')}</span>"),
        ("found by", html.escape(str(origin.get("found_by") or "-"))),
        ("feed / query", _link(origin.get("feed_url"), 110)),
        ("publisher", html.escape(str(origin.get("publisher") or "-"))),
        ("collector", html.escape(str(origin.get("collector") or "-"))),
        ("id at source", f"<span class='muted'>"
                         f"{html.escape(str(origin.get('id_at_source') or '-'))[:90]}</span>"),
    ]))

    lag = when.get("detection_lag_minutes")
    lag_html = "-" if lag is None else (
        f"{lag} min after publication"
        + ("" if lag < 30 else " <span class='bad'>&mdash; late</span>")
    )
    parts.append(_kv("When", [
        ("publication date", html.escape(str(when.get("publication_date") or "-"))),
        ("published", f"{html.escape(str(when.get('published_utc') or 'unknown'))[:16]} UTC "
                      f"&middot; {html.escape(str(when.get('published_et') or ''))} "
                      f"&middot; {html.escape(str(when.get('published_israel') or ''))}"),
        ("age", f"{when.get('age_hours', '?')}h"),
        ("session", html.escape(str(when.get("session_at_publication") or "-"))),
        ("vs the bell", html.escape(str(when.get("vs_last_close") or "-"))),
        ("we first saw it", f"{html.escape(str(when.get('first_seen_by_us_utc') or ''))[:16]} UTC"),
        ("detection lag", lag_html),
    ]))

    link_rows = []
    for link in data["who_it_is_about"]:
        colour = RELATION_COLOR.get(link["relation"], "#909090")
        link_rows.append(
            f"<tr class='item'><td class='tkr'>{html.escape(link['ticker'])}</td>"
            f"<td class='rel' style='color:{colour}'>{html.escape(link['relation'])}</td>"
            f"<td class='rel'>conf {link['confidence']:.2f}</td>"
            f"<td class='score' style='color:"
            f"{TIER_COLOR.get(link['tier'], '#d9d9d9')}'>{link['score']:.0f}</td>"
            f"<td>{html.escape(link.get('why') or '')}"
            f"<div class='why'>{html.escape(link['relation'])} means: "
            f"{html.escape(link.get('relation_means') or '')}</div></td></tr>"
        )
    parts.append(
        "<section><h2>Who it is about</h2><table>"
        "<tr><th class='tkr'>SYM</th><th class='rel'>REL</th><th class='rel'>LINK</th>"
        "<th class='score'>SC</th><th>WHY THIS SYMBOL</th></tr>"
        + "".join(link_rows) + "</table></section>"
    )

    thresholds = scored["thresholds"]
    trace_rows = [_trace_row(s) for s in scored["trace"]["item"]]
    for ticker, steps in scored["trace"]["per_ticker"].items():
        trace_rows.append(f"<tr><td class='op'></td><td class='tkr'>"
                          f"{html.escape(ticker)}</td></tr>")
        trace_rows.extend(_trace_row(s) for s in steps)
    parts.append(
        f"<section><h2>How the score was built</h2>"
        f"<div class='big'><span class='total'>{scored['score']:.1f}</span> "
        f"<span style='color:{TIER_COLOR.get(scored['tier'], '#d9d9d9')}'>"
        f"{html.escape(str(scored['tier']))}</span> "
        f"<span class='muted'>&nbsp;NORMAL {thresholds['NORMAL']:.0f} &middot; "
        f"HIGH {thresholds['HIGH']:.0f} &middot; ALERT {thresholds['ALERT']:.0f}</span></div>"
        f"<div class='why'>events: "
        f"{html.escape(', '.join(scored['events']) or 'none matched')}</div>"
        f"<table class='trace'>{''.join(trace_rows)}</table>"
        f"<p class='note'>{html.escape(scored['note'])}</p></section>"
    )

    carried = data["who_else_carried_it"]
    if carried["members"]:
        member_rows = "".join(
            f"<tr class='item'><td class='src'>{html.escape(m['source'])}</td>"
            f"<td class='tm'>{html.escape(str(m.get('published_at') or ''))[:16]}</td>"
            f"<td><a href='{html.escape(m.get('url') or '#')}' target='_blank' "
            f"rel='noreferrer'>{html.escape(m['title'])[:150]}</a>"
            f"{_dig(m.get('uid'))}</td></tr>"
            for m in carried["members"]
        )
        body = f"<table>{member_rows}</table>"
    else:
        body = ("<div class='empty'>single-sourced &mdash; nobody else we read "
                "has carried this story</div>")
    parts.append(
        f"<section><h2>Who else carried it "
        f"<span class='muted'>x{carried['corroboration']} "
        f"({html.escape(carried['counts'])})</span></h2>{body}</section>"
    )

    tape_rows = []
    for q in data["what_the_tape_did"]:
        cls = "up" if (q.get("change_pct") or 0) >= 0 else "dn"
        change = q.get("change_pct")
        vol = f"{q['volume_multiple']:.1f}x" if q.get("volume_multiple") else "-"
        chg_cell = (f"<td class='chg {cls}'>{change:+.2f}%</td>"
                    if change is not None else "<td class='chg'>-</td>")
        tape_rows.append(
            f"<tr class='item'><td class='tkr'>{html.escape(q['ticker'])}</td>"
            f"{chg_cell}"
            f"<td class='rel'>{vol}</td>"
            f"<td>{html.escape(str(q.get('math') or '-'))}"
            f"<div class='why'>{html.escape(q.get('provider') or '?')} &middot; "
            f"{html.escape(q.get('provider_note') or '')} &middot; "
            f"{html.escape(q.get('freshness') or '')}</div></td></tr>"
        )
    if tape_rows:
        parts.append(
            "<section><h2>What the tape did</h2><table>"
            "<tr><th class='tkr'>SYM</th><th class='chg'>CHG</th>"
            "<th class='rel'>VOL</th><th>THE ARITHMETIC, AND WHERE IT CAME FROM</th></tr>"
            + "".join(tape_rows) + "</table></section>"
        )

    check_rows = "".join(
        f"<tr class='item'><td><a href='{html.escape(c['url'])}' target='_blank' "
        f"rel='noreferrer'>{html.escape(c['label'])}</a></td>"
        f"<td class='why'>{html.escape(c['checks'])}</td></tr>"
        for c in data["check_it_yourself"]
    )
    parts.append(
        f"<section><h2>Check it yourself</h2><table class='check'>{check_rows}</table>"
        f"<p class='note'>None of these are us. If the original says something "
        f"different from the line above, trust the original and tell the system "
        f"it was wrong.</p></section>"
    )

    raw = data["raw"]
    raw_parts = []
    if raw.get("summary"):
        raw_parts.append(f"<pre class='raw'>{html.escape(raw['summary'])}</pre>")
    if raw.get("body_excerpt"):
        raw_parts.append(f"<pre class='raw'>{html.escape(raw['body_excerpt'])}</pre>")
    raw_parts.append(
        f"<pre class='raw'>{html.escape(json.dumps(raw.get('meta') or {}, ensure_ascii=False, indent=1, default=str))}</pre>"
    )
    parts.append(f"<section><h2>Raw record</h2>{''.join(raw_parts)}"
                 f"<p class='note'>Same record as "
                 f"<a href='/api/explain/{html.escape(data['uid'])}'>"
                 f"/api/explain/{html.escape(data['uid'][:12])}</a></p></section>")

    return _document(f"{data['uid'][:8]} &middot; HAREL", "", "".join(parts))


# --------------------------------------------------------------------------- #
def render_sources(views: Views) -> str:
    """Did the system even look? A quiet screen and a blind one look identical
    until you can see, per source, when it last returned anything."""
    report = views.sources_report()
    rows = []
    for s in report["sources"]:
        if not s["enabled"]:
            status, cls = "off", "off"
        elif not s["available"]:
            status, cls = f"no {s['requires_key']}", "bad"
        elif s["failing_endpoints"]:
            status, cls = f"{s['failing_endpoints']} failing", "bad"
        elif s["degraded"]:
            status, cls = "degraded", "bad"
        else:
            status, cls = "live", "ok"
        lag = s.get("median_lag_minutes")
        if lag is None:
            lag_cell = "<span class='muted'>-</span>"
        else:
            lag_cls = "ok" if lag <= 20 else ("bad" if lag >= 90 else "")
            lag_cell = (f"<span class='{lag_cls}'>{lag:.0f}m</span>"
                        f"<div class='why'>p90 {s.get('p90_lag_minutes', 0):.0f}m "
                        f"&middot; n={s.get('lag_sample', 0)}</div>")
        rows.append(
            f"<tr class='item'><td class='tkr'>{html.escape(s['source'])}</td>"
            f"<td class='rel {cls}'>{html.escape(status)}</td>"
            f"<td class='rel'>{s['trust']:.2f}</td>"
            f"<td class='rel'>{s['items_last_run'] or '-'}</td>"
            f"<td class='rel'>{lag_cell}</td>"
            f"<td class='tm'>{html.escape(str(s['last_ok_at'] or '-'))[:16]}</td>"
            f"<td>{html.escape(s['label'])}"
            f"<div class='why'>{html.escape(s['trust_means'])} &middot; "
            f"latency {html.escape(str(s['latency']))}"
            + (f"<br>{html.escape(s['last_error'] or '')}" if s['last_error'] else "")
            + (f"<br>{html.escape(s['note'][:200])}" if s["note"] else "")
            + "</div></td></tr>"
        )
    warn_html = "".join(
        f"<div class='warn'>{html.escape(w)}</div>" for w in report["warnings"])
    body = (
        f"<section><h2>Coverage warnings</h2>"
        f"{warn_html or '<div class=empty>none</div>'}</section>"
        "<section><h2>Every source</h2><table>"
        "<tr><th class='tkr'>KEY</th><th class='rel'>STATUS</th><th class='rel'>TRUST</th>"
        "<th class='rel'>ITEMS</th><th class='rel'>LAG</th><th class='tm'>LAST OK</th>"
        "<th>WHAT IT IS</th></tr>"
        + "".join(rows) + "</table>"
        "<p class='note'>ITEMS is what the last pass returned, per source. A live "
        "source returning 0 for days is not the same as a quiet market &mdash; check "
        "LAST OK before you conclude there is no news.<br>"
        "LAG is the median delay between something being published and this "
        "system seeing it, over everything published in the last 24 hours. It is "
        "the number that decides whether a source is tradeable or merely "
        "informative: at 5 minutes you can act on it, at 90 you are reading "
        "history. Right after a first backfill it reads high for a day, because "
        "the backlog really was seen late.</p></section>"
    )
    return _document("SOURCES &middot; HAREL", "", body)


# --------------------------------------------------------------------------- #
def _document(title: str, header_extra: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><style>{CSS}</style></head><body>"
        "<header><h1><a href='/'>HAREL&nbsp;TERMINAL</a></h1>"
        f"{header_extra}"
        "<nav><a href='/'>terminal</a><a href='/sources'>sources</a></nav>"
        f"</header><main>{body}</main></body></html>"
    )


def _kv(title: str, pairs: list[tuple[str, str]]) -> str:
    rows = "".join(f"<tr><td class='k'>{html.escape(k)}</td><td>{v}</td></tr>"
                   for k, v in pairs)
    return f"<section><h2>{html.escape(title)}</h2><table class='kv'>{rows}</table></section>"


def _link(url: str | None, cut: int = 80) -> str:
    if not url:
        return "-"
    return (f"<a href='{html.escape(url)}' target='_blank' rel='noreferrer'>"
            f"{html.escape(url[:cut])}{'&hellip;' if len(url) > cut else ''}</a>")


_OP = {"base": ("=", "op-base"), "multiply": ("&times;", ""),
       "add": ("+", "op-add"), "cap": ("!", "op-cap"), "note": ("", "")}


def _trace_row(step: dict[str, str]) -> str:
    symbol, cls = _OP.get(step["kind"], ("", ""))
    text = step["step"]
    # The sign is in the symbol column; leaving it in the text too reads as "++4".
    if step["kind"] == "add" and text.startswith("+"):
        text = text[1:]
    return (f"<tr><td class='op {cls}'>{symbol}</td>"
            f"<td colspan='2' class='{cls}'>{html.escape(text)}</td></tr>")


def _dig(uid: str | None) -> str:
    """The link that turns a headline into evidence."""
    if not uid:
        return ""
    return (f"<a class='dig' href='/item/{html.escape(uid)}' "
            f"title='where this came from and how it scored'>why?</a>")


def _quote_label(quote: dict[str, Any] | None) -> str:
    """One-line price provenance for a dense table."""
    if not quote:
        return "no quote stored"
    provider = quote.get("provider") or "unknown"
    if provider == "stooq":
        return f"stooq EOD {str(quote.get('asof') or '')[:10]}"
    age = quote.get("age_minutes")
    if age is None:
        return f"{provider}, capture time unknown"
    return f"{provider} · {age}m old, delayed"


def _time_cell(item: dict[str, Any]) -> str:
    """When it was published - or, honestly, when we merely found it."""
    if item.get("published_unknown"):
        seen = _fmt_time(item.get("discovered_at") or item["t"])
        return (f"<span class='muted'>date unknown</span>"
                f"<div class='why'>seen {html.escape(seen)}</div>")
    return html.escape(_fmt_time(item["t"]))


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
