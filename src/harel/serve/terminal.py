"""The terminal view - Hebrew, right-to-left.

Deliberately dense and keyboard-oriented: amber on black, monospace, no images,
no framework. A trader scans this, they do not browse it. Everything is rendered
server-side from the same :class:`Views` object the API and MCP server use.

The page is Hebrew; the REST API and the MCP tools stay English because that is
what the downstream agent is instructed in. Hebrew phrasing lives in
:mod:`hebrew`, and nothing here changes what is stored or scored.

Bidirectionality is the whole difficulty. The page direction is RTL, but most of
its content is not: symbols, prices, timestamps, source keys, URLs and roughly
half the headlines are Latin. Left to itself the bidi algorithm reorders them -
"+4.8%" renders as "%4.8+", and a headline ending in a bracket puts the bracket
on the wrong side. So every Latin or numeric field is wrapped in an explicit LTR
island, and headlines - which can be either language - carry dir="auto" so the
browser decides per string.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

from ..views import (PROVIDER_MEANING_HE, RELATION_LABEL_HE, RELATION_MEANING_HE,
                     SESSION_LABEL_HE, TRUST_MEANING_HE, Views)
from . import hebrew as he

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

# Monospace fonts carry no Hebrew glyphs, so the browser falls back per glyph.
# Naming the Hebrew faces explicitly keeps that fallback consistent instead of
# leaving it to whatever the system happens to pick.
FONT_STACK = ('"SF Mono", "Cascadia Mono", Menlo, Consolas, '
              '"Segoe UI", "Arial Hebrew", "Noto Sans Hebrew", sans-serif')

CSS = f"""
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: #000; color: #d9d9d9;
  font: 13px/1.55 {FONT_STACK};
}}
header {{
  position: sticky; top: 0; z-index: 5; background: #0a0a0a;
  border-bottom: 1px solid #333; padding: 8px 14px;
  display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap;
}}
h1 {{ font-size: 14px; margin: 0; color: #ffb000; letter-spacing: 0.5px; }}
.muted {{ color: #7a7a7a; }}
main {{ padding: 12px 14px 60px; }}
section {{ margin-bottom: 26px; }}
h2 {{
  font-size: 12px; color: #ffb000; letter-spacing: 0.5px;
  border-bottom: 1px solid #2a2a2a; padding-bottom: 4px; margin: 0 0 8px;
}}
table {{ width: 100%; border-collapse: collapse; }}
/* Logical properties throughout: the padding has to mirror with the page. */
td, th {{
  padding-block: 3px; padding-inline: 0 8px;
  vertical-align: top; text-align: start;
}}
th {{ color: #7a7a7a; font-weight: normal; font-size: 11px; }}
tr.item:hover {{ background: #111; }}
a {{ color: inherit; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

/* An LTR island. Latin text and numbers inside an RTL page reorder without it:
   "+4.8%" becomes "%4.8+", "(NASDAQ:TSEM)" loses its brackets to the far side. */
.ltr {{ direction: ltr; unicode-bidi: isolate; text-align: right; }}
.ltr-start {{ direction: ltr; unicode-bidi: isolate; text-align: left; }}

/* Cells keep the PAGE direction so their content hugs the reading edge - only
   the content inside is an LTR island. Putting direction:ltr on the cell itself
   aligned every number and symbol to the far side of its column, which glued
   "TATT" to "רגולציה" and "19" to "TATT". */
.score {{ font-weight: bold; width: 46px; white-space: nowrap; }}
.chg {{ font-weight: bold; width: 122px; white-space: nowrap; }}
.tkr {{ color: #ffb000; font-weight: bold; width: 64px; white-space: nowrap; }}
.rel {{ font-size: 11px; width: 108px; }}
.tm {{ color: #6b6b6b; width: 120px; }}
.src {{ color: #6b6b6b; font-size: 11px; width: 160px; }}
.why {{ color: #6b6b6b; font-size: 11px; }}
.up {{ color: #4ade80; }} .dn {{ color: #ff6b6b; }}
.pill {{
  display: inline-block; border: 1px solid #333; border-radius: 2px;
  padding: 0 5px; margin-inline-end: 4px; font-size: 10px; color: #9a9a9a;
}}
.warn {{
  border-inline-start: 3px solid #ffa500; background: #140f00;
  padding: 6px 10px; margin-bottom: 6px; color: #ffcf7a; font-size: 12px;
}}
/* The collect button. Logical properties like everything else, so it sits on
   the reading edge when the page mirrors. */
#collect {{
  font: inherit; color: #ffb000; background: #140f00; cursor: pointer;
  border: 1px solid #4a3800; border-radius: 2px; padding: 2px 10px;
}}
#collect:hover {{ background: #241a00; border-color: #ffb000; }}
/* The age of the data, which is the first thing the header has to answer. */
.updated {{ font-weight: bold; }}
.updated.ok {{ color: #4ade80; }}
.updated.warn-age {{ color: #ffa500; }}
.updated.bad {{ color: #ff6b6b; }}
.updated .muted {{ font-weight: normal; }}
.collect-form {{ margin: 0; display: inline; }}
.collect {{ font-size: 11px; }}
.collect.running {{ color: #ffb000; }}
.collect.ok {{ color: #4ade80; }}
.collect.bad {{ color: #ff6b6b; }}
.empty {{ color: #6b6b6b; font-style: italic; padding: 6px 0; }}
.tape {{
  border-inline-start: 3px solid #ff4d4d; background: #170a0a;
  padding: 6px 10px; margin-bottom: 6px; color: #ffb3b3; font-size: 12px;
}}
.dig {{
  color: #5fd7ff; font-size: 10px; border: 1px solid #24404a;
  border-radius: 2px; padding: 0 4px; margin-inline-start: 6px;
  white-space: nowrap; unicode-bidi: isolate;
}}
.dig:hover {{ background: #0d2028; text-decoration: none; }}
nav a {{ color: #7a7a7a; margin-inline-end: 12px; }}
nav a:hover {{ color: #ffb000; }}

/* --- drill-down ------------------------------------------------------- */
.kv {{ width: 100%; margin-bottom: 4px; }}
.kv td {{ padding-block: 2px; padding-inline: 0 10px; }}
.kv td.k {{ color: #7a7a7a; width: 160px; white-space: nowrap; vertical-align: top; }}
.big {{ font-size: 15px; color: #e8e8e8; margin: 4px 0 2px; }}
.trace td {{ padding-block: 1px; padding-inline: 0 10px; }}
.trace td.op {{
  width: 26px; color: #7a7a7a; direction: ltr; unicode-bidi: isolate;
  text-align: center;
}}
.op-base {{ color: #ffb000; }} .op-add {{ color: #4ade80; }} .op-cap {{ color: #ff6b6b; }}
.total {{ color: #ffb000; font-weight: bold; }}
.note {{ color: #6b6b6b; font-size: 11px; margin: 4px 0 0; }}
pre.raw {{
  white-space: pre-wrap; word-break: break-word; color: #9a9a9a;
  background: #0a0a0a; border: 1px solid #1e1e1e; padding: 8px;
  max-height: 320px; overflow: auto; margin: 0 0 8px;
  direction: ltr; text-align: left; unicode-bidi: isolate;
}}
.check td {{ padding-block: 3px; padding-inline: 0 10px; }}
.ok {{ color: #4ade80; }} .bad {{ color: #ff6b6b; }} .off {{ color: #6b6b6b; }}
"""


# --------------------------------------------------------------------------- #
# Bidi helpers. Everything Latin or numeric goes through one of these.
# --------------------------------------------------------------------------- #
def _ltr(text: Any) -> str:
    """An escaped LTR island - symbols, numbers, keys, timestamps."""
    return f"<span class='ltr'>{html.escape(str(text))}</span>"


def _last_update_he(updated: dict[str, Any] | None) -> str:
    """When the SYSTEM last collected - the age of everything below it.

    The header used to carry one time, the moment the HTML was rendered, which
    changes on every reload and describes nothing. A trader reading a quiet feed
    has to be able to tell "nothing happened" from "nothing has been fetched
    since Friday", and that is the one distinction this whole page exists to
    make.
    """
    if not updated or not updated.get("ever"):
        return (f"<span class='updated bad'>"
                f"{html.escape(he.UI['updated_never'])}</span>")

    status = updated.get("status")
    cls = {"stale": "warn-age", "very_stale": "bad"}.get(status, "ok")
    when = updated.get("finished_at")
    bits = [f"{html.escape(he.UI['updated'])} "
            f"{_ltr(he.ago(when))}"]
    # The clock as well as the age: "לפני 3 שעות" is the useful number, but the
    # absolute time is what you check against your broker screen.
    bits.append(f"<span class='muted'>{_ltr(he.day_label(when))} "
                f"{_ltr(str(when)[11:16])}</span>")
    if updated.get("partial"):
        bits.append(f"<span class='muted'>"
                    f"({html.escape(he.UI['updated_partial'])})</span>")
    return f"<span class='updated {cls}'>" + " &middot; ".join(bits) + "</span>"


def _detection_lag_he(lag: int | None) -> str:
    """How long we were blind to an item - or, when negative, how far ahead of it
    we were.

    A negative lag is not a fast detection. It means we hold the document before
    the date it publishes under, which is the Federal Register public-inspection
    lead time and the entire reason that source is polled. It was rendering as
    "-220 דקות אחרי הפרסום" - a negative count of minutes after an event that
    had not happened - and the `< 30` test below read it as comfortably on time.
    24 items in the live database carry one.
    """
    if lag is None:
        return "-"
    if lag < 0:
        return (f"{_ltr(-lag)} דקות <b>לפני</b> מועד הפרסום "
                f"&middot; זמן קדימה, לא פיגור")
    late = "" if lag < 30 else " <span class='bad'>&mdash; באיחור</span>"
    return f"{_ltr(lag)} דקות אחרי הפרסום{late}"


def _public_lag_he(minutes: int | None) -> str:
    """How long we were blind, measured from the moment the document became
    readable by anyone rather than from the date it publishes under.

    This is the honest one. `_detection_lag_he` above measures against
    `published_at`, which for a Federal Register document is a *scheduled* date -
    so it reports lead time, and a document sitting unfetched on public
    inspection since Friday still looked early. Rendered as a duration, not in
    minutes: the gap that prompted this is 2,340 of them.
    """
    if minutes is None:
        return "-"
    if minutes < 0:
        return (f"{html.escape(he.duration(-minutes))} <b>לפני</b> שהמסמך "
                f"היה זמין לציבור &middot; זמן קדימה, לא פיגור")
    late = "" if minutes < 30 else " <span class='bad'>&mdash; באיחור</span>"
    return f"{html.escape(he.duration(minutes))} אחרי שהמסמך היה זמין לציבור{late}"


def _auto(text: str, cut: int | None = None) -> str:
    """Text that may be Hebrew or Latin: let the browser decide per string."""
    value = text or ""
    # Cut the text, then escape it. The other way round cuts inside an entity:
    # "AT&T earnings" came out as "AT&a", which is what the reader saw - and an
    # apostrophe is six characters once escaped, so on a headline carrying one
    # the cut also landed nowhere near the length asked for.
    if cut:
        value = value[:cut]
    return f"<span dir='auto'>{html.escape(value)}</span>"


def _pct(value: float, digits: int = 1, suffix: str = "%") -> str:
    return f"<span class='ltr'>{value:+.{digits}f}{suffix}</span>"


# --------------------------------------------------------------------------- #
def render_terminal(views: Views, collect: dict[str, Any] | None = None) -> str:
    brief = views.morning_brief(hours=24)
    # 40 was reachable only because the [TAPE] markers carried a forced 70 and
    # sat in the feed. With those moved to the movers board where they belong,
    # genuine news peaks in the low 30s once recency decay has run for a few
    # hours - so a threshold of 40 renders an empty panel on any day that is not
    # breaking live, which is exactly when you would distrust the whole tool.
    feed = views.feed(min_score=20, hours=24, limit=60)
    moving = views.whats_moving(min_abs_pct=1.5)
    health = views.health()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = (
        # Labelled, because an unlabelled clock in a header reads as the age of
        # the data and this one is only the moment the HTML was built.
        f"<span class='muted'>{he.UI['rendered']} {_ltr(now)}</span>"
        f"<span class='muted'>{_ltr(health['db']['items'])} {he.UI['items']} "
        f"&middot; {_ltr(health['db']['alerts_24h'])} {he.UI['alerts_24h']} &middot; "
        f"{_ltr(str(health['sources_available']) + '/' + str(health['sources_configured']))} "
        f"{he.UI['sources_live']}</span>"
    )
    parts: list[str] = []

    entries = views.coverage_warning_entries()
    if entries:
        parts.append(f"<section><h2>{he.UI['coverage_warnings']}</h2>")
        for entry in entries:
            parts.append(f"<div class='warn'>{_coverage_warning_he(entry)}</div>")
        parts.append("</section>")

    # Tape alerts come first. A move that outran its sector with nothing behind
    # it is the most urgent thing on the page precisely BECAUSE there is no
    # story - and a news feed, by construction, could never raise it.
    parts.append(_unexplained_section(brief.get("unexplained_moves") or []))

    parts.append(_section(
        he.UI["news_alerts"], brief["alerts"],
        empty=he.UI["no_alerts"] if health["db"]["items"] else he.UI["db_empty"],
    ))
    parts.append(_movers_section(moving["movers"]))

    if feed["items"]:
        parts.append(_section(he.UI["feed"], feed["items"]))
    else:
        # An empty panel is the one thing this page must never be. "Nothing
        # above 20" reads as a threshold artefact and leaves the trader unable
        # to tell a quiet tape from a broken pipeline - the distinction the
        # whole page exists to make. Uncapped for the count: the feed keeps at
        # most three per name, and that number is the point of the panel.
        background = views.feed(min_score=0, hours=24, limit=400, max_per_ticker=None)
        parts.append(_background_section(background["items"], health))

    if brief.get("tase_overnight"):
        parts.append(_section(he.UI["tase_overnight"], brief["tase_overnight"]))
    parts.append(_calendar_section(brief.get("calendar_next_7d") or []))

    return _document(he.UI["brand"], header, "".join(parts), collect,
                     views.last_update())


def _coverage_warning_he(entry: dict[str, Any]) -> str:
    """Composed from the structured entry, never translated from the sentence -
    prose drifts, data does not."""
    kind = entry["kind"]
    if kind == "no_collector":
        return (f"למקור {_ltr(entry['source'])} אין קולקטור: הסוג "
                f"{_ltr(entry['source_kind'])} לא ממומש בקוד, ולכן הוא מדולג "
                f"בכל מעבר — מפתח API לא ישנה זאת")
    if kind == "missing_key":
        return (f"המקור {_ltr(entry['source'])} כבוי: "
                f"{_ltr(entry['env'])} לא מוגדר")
    if kind == "degraded":
        return (f"המקור {_ltr(entry['source'])} רץ על endpoint חלופי לא רשמי "
                f"מפני ש-{_ltr(entry['env'])} לא מוגדר — הוא עלול להישבר בלי התראה")
    if kind == "unresolved_ticker":
        return (f"הטיקר {_ltr(entry['ticker'])} לא נפתר ו<b>אינו נאסף</b> — "
                f"{_auto(entry.get('hint') or '', 140)}")
    if kind == "disabled":
        reason = (entry.get("reason") or "").strip()
        tail = f": {_auto(reason, 150)}" if reason else ""
        return f"המקור {_ltr(entry['source'])} מושבת בקונפיג{tail}"
    if kind == "failing":
        return (f"המקור {_ltr(entry['source'])} נכשל "
                f"{_ltr(entry['count'])} פעמים: {_auto(str(entry.get('error') or ''), 90)}")
    return _auto(str(entry))


def _section(title: str, items: list[dict[str, Any]], empty: str | None = None) -> str:
    if not items:
        # "run harel collect" was printed whenever a panel was empty, so a
        # working system with a quiet tape accused itself of never having
        # collected. Distinguishing the two is the entire point of this page.
        return (f"<section><h2>{html.escape(title)}</h2>"
                f"<div class='empty'>{html.escape(empty or he.UI['nothing_to_show'])}"
                f"</div></section>")

    rows = [f"<table><tr><th class='score'>{he.UI['col_score']}</th>"
            f"<th class='tkr'>{he.UI['col_symbol']}</th>"
            f"<th class='rel'>{he.UI['col_relation']}</th>"
            f"<th class='tm'>{he.UI['col_time']}</th>"
            f"<th>{he.UI['col_headline']}</th>"
            f"<th class='src'>{he.UI['col_source']}</th></tr>"]
    for it in items:
        tier_color = TIER_COLOR.get(it.get("tier", "NORMAL"), "#d9d9d9")
        rel = it.get("relation", "")
        rel_color = RELATION_COLOR.get(rel, "#909090")
        url = it.get("url") or "#"
        events = "".join(
            f"<span class='pill'>{html.escape(he.event_label(e))}</span>"
            for e in (it.get("events") or [])[:3]
        )
        corr = it.get("corroboration")
        corr_html = f"<span class='pill ltr'>x{corr}</span>" if corr else ""
        why_he, _ = he.why(it.get("why") or "")
        score_cell = _ltr(f"{it['score']:.0f}")
        rows.append(
            f"<tr class='item'>"
            f"<td class='score' style='color:{tier_color}'>"
            f"{score_cell}</td>"
            f"<td class='tkr'>{_ltr(it.get('ticker', ''))}</td>"
            f"<td class='rel' style='color:{rel_color}'>"
            f"{html.escape(RELATION_LABEL_HE.get(rel, rel))}</td>"
            f"<td class='tm'>{_time_cell(it)}</td>"
            f"<td><a href='{html.escape(url)}' target='_blank' rel='noreferrer' "
            f"dir='auto'>{html.escape(it['title'][:190])}</a> "
            f"{events}{corr_html}{_dig(it.get('uid'))}"
            f"<div class='why' dir='auto'>{why_he}</div></td>"
            f"<td class='src'>{_ltr(it['source'])}</td></tr>"
        )
    rows.append("</table>")
    return f"<section><h2>{html.escape(title)}</h2>{''.join(rows)}</section>"


def _background_section(items: list[dict[str, Any]], health: dict[str, Any]) -> str:
    """What to show when nothing cleared the bar: the bar, and the best of what
    is underneath it."""
    if not items:
        return _section(
            he.UI["feed"], [],
            empty=(he.UI["nothing_collected"] if health["db"]["items"]
                   else he.UI["db_empty"]),
        )
    top = items[0]["score"]
    note = (
        f"<div class='warn'>שום פריט לא עבר ציון {_ltr(20)} ב-24 השעות האחרונות — "
        f"הגבוה ביותר הוא {_ltr(f'{top:.1f}')}. זה <b>סל שקט, לא מערכת עיוורת</b>: "
        f"{_ltr(len(items))} פריטים נאספו וקושרו בחלון הזה. "
        f"הגבוהים ביותר מוצגים למטה כרקע; אף אחד מהם אינו סיבה לפעולה.</div>"
    )
    # One per name here: a dozen rows from one busy ticker would misrepresent a
    # quiet basket as a busy one.
    seen: set[str] = set()
    shown = []
    for item in items:
        key = str(item.get("ticker") or "")
        if key in seen:
            continue
        seen.add(key)
        shown.append(item)
    return note + _section(he.UI["feed_below"], shown[:12])


def _unexplained_section(alerts: list[dict[str, Any]]) -> str:
    """The tape moved and we found nothing. Stated as a question, not a cause."""
    if not alerts:
        return ""
    rows = []
    for a in alerts:
        bits = [f"{_ltr(a['ticker'])} {_pct(a['change_pct'])}"]
        if a.get("relative_pct") is not None:
            bench = f"{_ltr(a['benchmark'])} ({_pct(a.get('benchmark_pct') or 0)})"
            bits.append(f"{_pct(a['relative_pct'], suffix='pp')} מול {bench}")
        if a.get("volume_multiple"):
            bits.append("מחזור " + _ltr(f"{a['volume_multiple']:.1f}x"))
        headline = "תנועה חריגה ללא הסבר — " + " | ".join(bits)

        tail = ["לא נמצא קטליזטור שקדם לתנועה מעל ציון 20 ב-30 השעות האחרונות"]
        if a.get("post_move_commentary"):
            tail.append("(פרשנויות מחיר סוננו כתגובתיות)")
        detail = " ".join(tail)

        # A sector keyword match read as an explanation is worse than no
        # explanation: the same UFLPA notice was offered as the reason TSEM rose
        # 4% and as the reason CAMT fell 3.5% in the same session. Named here,
        # at its real strength, so the reader can weigh it - and so the move
        # stays a question instead of being quietly closed.
        for context in (a.get("possible_context") or [])[:2]:
            detail += (
                f"<br>הקשר רגולטורי אפשרי, בביטחון נמוך: "
                f"{_auto(context['title'], 90)} — "
                f"לא נמצאה חשיפה ספציפית לחברה")

        catalyst = a.get("next_catalyst")
        if catalyst and catalyst.get("strength") == "company":
            detail += (f"<br>התאריך הידוע הבא: <b>{_ltr(catalyst['date'])}</b> "
                       f"{_auto(he.calendar_label(catalyst['label']), 70)} — "
                       f"מיצוב לקראתו אפשרי, אך לא מאומת")
        elif catalyst:
            detail += (f"<br>קישור חלש: תאריך בסקטור, לא של החברה הזאת — "
                       f"{_ltr(catalyst['date'])} "
                       f"{_auto(he.calendar_label(catalyst['label']), 70)}")
        rows.append(f"<div class='tape'><b>{headline}</b>"
                    f"<div class='why'>{detail}</div></div>")
    return (f"<section><h2>{he.UI['unexplained']}</h2>" + "".join(rows)
            + "<p class='note'>אלה שאלות, לא ממצאים. התנועה ברחה מהסקטור שלה "
              "ושום דבר שקראנו לא מסביר אותה: בדוק את ספר הפקודות, זרימת "
              "האופציות וכל קטליזטור ממתין לפני שתניח שפספסנו ידיעה.</p></section>")


def _movers_section(movers: list[dict[str, Any]]) -> str:
    if not movers:
        return ""
    rows = [f"<table><tr><th class='tkr'>{he.UI['col_symbol']}</th>"
            f"<th class='chg'>{he.UI['col_change']}</th>"
            f"<th class='rel'>{he.UI['col_volume']}</th>"
            f"<th class='rel'>{he.UI['col_vs_sector']}</th>"
            f"<th>{he.UI['col_driver']}</th></tr>"]
    for m in movers:
        cls = "up" if m["change_pct"] >= 0 else "dn"
        vol = (f"<span class='ltr'>{m['volume_multiple']:.1f}x</span>"
               if m.get("volume_multiple") else "-")
        # Where the percentage came from. A trader reconciling against their own
        # screen needs to know it is a delayed Yahoo print, not a live quote.
        provenance = f"<div class='why'>{_quote_label(m.get('quote'))}</div>"

        rel_pct = m.get("relative_pct")
        if rel_pct is None:
            rel_cell = "<span class='muted'>-</span>"
        else:
            rel_cls = "up" if rel_pct >= 0 else "dn"
            # One island for the pair: two adjacent islands swap places in
            # RTL, so "ITA +0.6%" came out as "+0.6% ITA".
            bench = f"{m.get('benchmark') or ''} {(m.get('benchmark_pct') or 0):+.1f}%"
            rel_cell = (f"<span class='{rel_cls} ltr'>{rel_pct:+.1f}pp</span>"
                        f"<div class='why'>{_ltr(bench)}</div>")

        if m["drivers"]:
            top = m["drivers"][0]
            driver = (f"<a href='{html.escape(top.get('url') or '#')}' target='_blank' "
                      f"rel='noreferrer' dir='auto'>{html.escape(top['title'][:150])}</a>"
                      f"{_dig(top.get('uid'))}")
        elif rel_pct is not None and abs(rel_pct) < 2.0:
            # Most of the move is the group. Saying "no matching news" here
            # invites you to hunt for a company story that does not exist.
            bench = f"{m.get('benchmark') or ''} {(m.get('benchmark_pct') or 0):+.1f}%"
            driver = (f"<span class='muted'>עוקב אחרי הסקטור — {_ltr(bench)}, "
                      f"אין חדשות ספציפיות למניה</span>")
        else:
            driver = ("<span class='muted'>אין חדשות תואמות — זרימת הזמנות, "
                      "טכני, או פער בכיסוי</span>")
        # Published after the bell: cannot explain today's move, but it is the
        # next session's setup, so show it rather than dropping it.
        for late in (m.get("after_the_bell") or [])[:1]:
            driver += (
                f"<div class='why'>אחרי הפעמון &middot; לא הגורם לתנועה הזאת: "
                f"<a href='{html.escape(late.get('url') or '#')}' target='_blank' "
                f"rel='noreferrer' dir='auto'>{html.escape(late['title'][:120])}</a></div>"
            )
        # Written because the price moved. Shown, because a trader will find it
        # anyway and needs to know we classified it rather than missed it.
        for recap in (m.get("post_move_commentary") or [])[:1]:
            driver += (
                f"<div class='why'>פרשנות שלאחר התנועה &middot; תגובה, לא קטליזטור: "
                f"<a href='{html.escape(recap.get('url') or '#')}' target='_blank' "
                f"rel='noreferrer' dir='auto'>{html.escape(recap['title'][:120])}</a>"
                f"{_dig(recap.get('uid'))}</div>"
            )
        rows.append(
            f"<tr class='item'><td class='tkr'>{_ltr(m['ticker'])}</td>"
            f"<td class='chg {cls}'>{_pct(m['change_pct'])}{provenance}</td>"
            f"<td class='rel'>{vol}</td><td class='rel'>{rel_cell}</td>"
            f"<td>{driver}</td></tr>"
        )
    rows.append("</table>")
    return f"<section><h2>{he.UI['movers']}</h2>{''.join(rows)}</section>"


def _calendar_section(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    rows = [f"<table><tr><th class='tm'>{he.UI['col_date']}</th>"
            f"<th class='tkr'>{he.UI['col_symbol']}</th>"
            f"<th class='rel'>{he.UI['col_kind']}</th>"
            f"<th>{he.UI['col_event']}</th></tr>"]
    for e in entries:
        rows.append(
            f"<tr class='item'><td class='tm'>{_ltr(e['date'])}</td>"
            f"<td class='tkr'>{_ltr(e['ticker'])}</td>"
            f"<td class='rel'>{html.escape(he.event_label(e['kind']))}</td>"
            f"<td>{_auto(he.calendar_label(e['label']))}</td></tr>"
        )
    rows.append("</table>")
    return f"<section><h2>{he.UI['calendar']}</h2>{''.join(rows)}</section>"


# --------------------------------------------------------------------------- #
# Drill-down: one item, all of its evidence.
#
# The feed is a summary, and a summary is a claim. This page is the claim's
# working: which query found it, how much that source is trusted and why, when it
# was published against the bell, which rule attached it to which symbol, the
# arithmetic of the score, who else carried it, what the tape did - and a set of
# outside links so the whole thing can be checked without us.
# --------------------------------------------------------------------------- #
def render_item(views: Views, uid: str,
                collect: dict[str, Any] | None = None) -> str:
    data = views.explain(uid)
    if data.get("error"):
        return _document("לא נמצא", "", (
            f"<section><h2>לא נמצא</h2>"
            f"<div class='empty' dir='auto'>{html.escape(data['error'])}</div>"
            f"<p class='note'>מזהים הם sha1; קידומת ייחודית של 8 תווים ומעלה "
            f"מספיקה.</p></section>"
        ))

    origin = data["where_it_came_from"]
    when = data["when"]
    scored = data["how_it_scored"]
    parts: list[str] = []

    url = data.get("url") or "#"
    parts.append(
        f"<section><div class='big'><a href='{html.escape(url)}' target='_blank' "
        f"rel='noreferrer' dir='auto'>{html.escape(data['title'])}</a></div>"
        f"<div class='muted'>{_ltr(data['uid'])}</div></section>"
    )

    trust = origin.get("trust")
    parts.append(_kv("מאיפה זה הגיע", [
        ("מקור", f"{_ltr(origin['source'])} &middot; "
                 f"{_auto(origin.get('source_label') or '')}"),
        ("אמון", f"{_ltr(trust if trust is not None else '?')} &mdash; "
                 f"<span class='muted'>{html.escape(_trust_he(trust))}</span>"),
        ("נמצא על ידי", _auto(str(origin.get("found_by") or "-"))),
        ("פיד / שאילתה", _link(origin.get("feed_url"), 110)),
        ("מפרסם", _auto(str(origin.get("publisher") or "-"))),
        ("אספן", _ltr(origin.get("collector") or "-")),
        ("מזהה במקור", f"<span class='muted ltr'>"
                       f"{html.escape(str(origin.get('id_at_source') or '-')[:90])}</span>"),
    ]))

    lag_html = _detection_lag_he(when.get("detection_lag_minutes"))

    if when.get("undated"):
        pub_date = ("לא ידוע — הפיד לא נשא תאריך, ולכן החותמת למטה היא מתי "
                    "מצאנו אותו, לא מתי הוא פורסם")
    else:
        pub_date = "כפי שפורסם"

    if when.get("undated"):
        bell = "-"
    elif when.get("before_last_close"):
        bell = (f"פורסם לפני נעילת {_ltr((when.get('last_close_et') or '') + ' ET')} — "
                f"יכול להיות גורם לתנועה של אותו סשן")
    else:
        bell = (f"פורסם אחרי נעילת {_ltr((when.get('last_close_et') or '') + ' ET')} — "
                f"זה הסטאפ של הסשן הבא, ולא יכול להיות הגורם לתנועה ההיא")

    when_rows = [
        ("תאריך הפרסום", pub_date),
        ("פורסם", f"{_ltr(str(when.get('published_utc') or 'לא ידוע')[:16] + ' UTC')} "
                  f"&middot; {_ltr(when.get('published_et') or '')} "
                  f"&middot; {_ltr(when.get('published_israel') or '')}"),
        ("גיל", f"{_ltr(when.get('age_hours', '?'))} שעות"),
        ("סשן", html.escape(SESSION_LABEL_HE.get(
            str(when.get("session_at_publication") or ""),
            str(when.get("session_at_publication") or "-")))),
        ("מול הפעמון", bell),
    ]
    # Above "ראינו לראשונה" on purpose: it is the earlier of the two moments,
    # and the pair only reads as a gap when they are in order.
    if when.get("first_public_at"):
        when_rows.append((
            "זמין לציבור לראשונה",
            f"{_ltr(str(when['first_public_at'])[:16] + ' UTC')} &middot; "
            f"{_ltr(he.eastern_label(when['first_public_at']))}"))
    when_rows.append(
        ("ראינו לראשונה",
         _ltr(str(when.get('first_seen_by_us_utc') or '')[:16] + " UTC")))
    from_public = when.get("detection_lag_minutes_from_public")
    # On a public-inspection document `published_at` IS the filing moment, so
    # the two lags are the same number under two names and printing both invites
    # the reader to look for a difference that is not there. They diverge only
    # for a published document, where the filing came days before the date it
    # publishes under - which is the case the second row exists for.
    if from_public is None or from_public != when.get("detection_lag_minutes"):
        when_rows.append(("פיגור זיהוי", lag_html))
    if from_public is not None:
        when_rows.append(("פיגור גילוי", _public_lag_he(from_public)))
    parts.append(_kv("מתי", when_rows))

    link_rows = []
    for link in data["who_it_is_about"]:
        rel = link["relation"]
        colour = RELATION_COLOR.get(rel, "#909090")
        why_he, _ = he.why(link.get("why") or "")
        confidence = _ltr(f"{link['confidence']:.2f}")
        link_score = _ltr(f"{link['score']:.0f}")
        link_rows.append(
            f"<tr class='item'><td class='tkr'>{_ltr(link['ticker'])}</td>"
            f"<td class='rel' style='color:{colour}'>"
            f"{html.escape(RELATION_LABEL_HE.get(rel, rel))}</td>"
            f"<td class='rel'>{confidence}</td>"
            f"<td class='score' style='color:"
            f"{TIER_COLOR.get(link['tier'], '#d9d9d9')}'>"
            f"{link_score}</td>"
            f"<td dir='auto'>{why_he}"
            f"<div class='why'>{html.escape(RELATION_LABEL_HE.get(rel, rel))} = "
            f"{html.escape(RELATION_MEANING_HE.get(rel, ''))}</div></td></tr>"
        )
    parts.append(
        f"<section><h2>על מי זה</h2><table>"
        f"<tr><th class='tkr'>{he.UI['col_symbol']}</th>"
        f"<th class='rel'>{he.UI['col_relation']}</th>"
        f"<th class='rel'>{he.UI['col_link']}</th>"
        f"<th class='score'>{he.UI['col_score']}</th>"
        f"<th>{he.UI['col_why_symbol']}</th></tr>"
        + "".join(link_rows) + "</table></section>"
    )

    thresholds = scored["thresholds"]
    trace_rows = [_trace_row(s) for s in scored["trace"]["item"]]
    for ticker, steps in scored["trace"]["per_ticker"].items():
        trace_rows.append(f"<tr><td class='op'></td><td class='tkr'>"
                          f"{_ltr(ticker)}</td></tr>")
        trace_rows.extend(_trace_row(s) for s in steps)
    events_he = ", ".join(he.event_label(e) for e in scored["events"]) or "לא נמצאה התאמה"
    # One island for the whole run. Emitting "NORMAL {island} · HIGH {island}"
    # left the bare labels outside the isolates, so the bidi algorithm merged
    # them with their neighbours and reordered the lot: "75 NORMAL 35 · HIGH 55
    # · ALERT".
    tiers = _ltr(" · ".join(f"{name} {thresholds[name]:.0f}"
                            for name in ("NORMAL", "HIGH", "ALERT")))
    parts.append(
        f"<section><h2>איך נבנה הציון</h2>"
        f"<div class='big'><span class='total ltr'>{scored['score']:.1f}</span> "
        f"<span style='color:{TIER_COLOR.get(scored['tier'], '#d9d9d9')}'>"
        f"{_ltr(scored['tier'])}</span> "
        f"<span class='muted'>&nbsp;{tiers}</span></div>"
        f"<div class='why'>אירועים: {html.escape(events_he)}</div>"
        f"<table class='trace'>{''.join(trace_rows)}</table>"
        f"<p class='note'>העקבות כפי שנשמרו, לפי הסדר. המכפילים מצטברים; שורות "
        f"עם + מתווספות אחריהם; תקרה גוברת על הכול.</p></section>"
    )

    carried = data["who_else_carried_it"]
    if carried["members"]:
        member_rows = "".join(
            f"<tr class='item'><td class='src'>{_ltr(m['source'])}</td>"
            f"<td class='tm'>{_ltr(str(m.get('published_at') or '')[:16])}</td>"
            f"<td><a href='{html.escape(m.get('url') or '#')}' target='_blank' "
            f"rel='noreferrer' dir='auto'>{html.escape(m['title'][:150])}</a>"
            f"{_dig(m.get('uid'))}</td></tr>"
            for m in carried["members"]
        )
        body = f"<table>{member_rows}</table>"
    else:
        body = ("<div class='empty'>מקור יחיד — אף אחד אחר שאנחנו קוראים "
                "לא נשא את הסיפור הזה</div>")
    parts.append(
        f"<section><h2>מי עוד נשא את זה "
        f"<span class='muted'>{_ltr('x' + str(carried['corroboration']))} "
        f"(מקורות נבדלים, לא מסמכים)</span></h2>{body}</section>"
    )

    tape_rows = []
    for q in data["what_the_tape_did"]:
        cls = "up" if (q.get("change_pct") or 0) >= 0 else "dn"
        change = q.get("change_pct")
        vol = (f"<span class='ltr'>{q['volume_multiple']:.1f}x</span>"
               if q.get("volume_multiple") else "-")
        chg_cell = (f"<td class='chg {cls}'>{_pct(change, 2)}</td>"
                    if change is not None else "<td class='chg'>-</td>")
        tape_rows.append(
            f"<tr class='item'><td class='tkr'>{_ltr(q['ticker'])}</td>"
            f"{chg_cell}"
            f"<td class='rel'>{vol}</td>"
            f"<td>{_ltr(q.get('math') or '-')}"
            f"<div class='why'>{_ltr(q.get('provider') or '?')} &middot; "
            f"{html.escape(_provider_he(q.get('provider')))} &middot; "
            f"{_freshness_he(q)}</div></td></tr>"
        )
    if tape_rows:
        parts.append(
            f"<section><h2>מה עשה הטייפ</h2><table>"
            f"<tr><th class='tkr'>{he.UI['col_symbol']}</th>"
            f"<th class='chg'>{he.UI['col_change']}</th>"
            f"<th class='rel'>{he.UI['col_volume']}</th>"
            f"<th>{he.UI['col_arithmetic']}</th></tr>"
            + "".join(tape_rows) + "</table></section>"
        )

    check_rows = "".join(
        f"<tr class='item'><td><a href='{html.escape(c['url'])}' target='_blank' "
        f"rel='noreferrer'>{html.escape(c.get('label_he') or c['label'])}</a></td>"
        f"<td class='why'>{html.escape(c.get('checks_he') or c['checks'])}</td></tr>"
        for c in data["check_it_yourself"]
    )
    parts.append(
        f"<section><h2>תבדוק בעצמך</h2><table class='check'>{check_rows}</table>"
        f"<p class='note'>אף אחד מהקישורים האלה הוא לא אנחנו. אם המקור אומר משהו "
        f"אחר מהשורה שלמעלה — המקור צודק, ותגיד למערכת שהיא טעתה.</p></section>"
    )

    raw = data["raw"]
    raw_parts = []
    if raw.get("summary"):
        raw_parts.append(f"<pre class='raw' dir='auto'>{html.escape(raw['summary'])}</pre>")
    if raw.get("body_excerpt"):
        raw_parts.append(
            f"<pre class='raw' dir='auto'>{html.escape(raw['body_excerpt'])}</pre>")
    raw_parts.append(
        f"<pre class='raw'>{html.escape(json.dumps(raw.get('meta') or {}, ensure_ascii=False, indent=1, default=str))}</pre>"
    )
    parts.append(f"<section><h2>הרשומה הגולמית</h2>{''.join(raw_parts)}"
                 f"<p class='note'>אותה רשומה, ב-JSON: "
                 f"<a href='/api/explain/{html.escape(data['uid'])}' class='ltr'>"
                 f"/api/explain/{html.escape(data['uid'][:12])}</a></p></section>")

    return _document(f"{data['uid'][:8]} · {he.UI['brand']}", "", "".join(parts),
                     collect, views.last_update())


# --------------------------------------------------------------------------- #
def render_sources(views: Views, collect: dict[str, Any] | None = None) -> str:
    """Did the system even look? A quiet screen and a blind one look identical
    until you can see, per source, when it last returned anything."""
    report = views.sources_report()
    rows = []
    for s in report["sources"]:
        if not s["enabled"]:
            status, cls = "כבוי", "off"
        elif not s["available"]:
            status, cls = f"חסר {s['requires_key']}", "bad"
        elif s["failing_endpoints"]:
            status, cls = f"{s['failing_endpoints']} נכשלים", "bad"
        elif s["degraded"]:
            status, cls = "מוגבל", "bad"
        else:
            status, cls = "פעיל", "ok"
        lag = s.get("median_lag_minutes")
        if lag is None:
            lag_cell = "<span class='muted'>-</span>"
        else:
            lag_cls = "ok" if lag <= 20 else ("bad" if lag >= 90 else "")
            lag_cell = (f"<span class='{lag_cls} ltr'>{lag:.0f}m</span>"
                        f"<div class='why ltr'>p90 {s.get('p90_lag_minutes', 0):.0f}m "
                        f"&middot; n={s.get('lag_sample', 0)}</div>")
        trust_cell = _ltr(f"{s['trust']:.2f}")
        rows.append(
            f"<tr class='item'><td class='tkr'>{_ltr(s['source'])}</td>"
            f"<td class='rel {cls}'>{html.escape(status)}</td>"
            f"<td class='rel'>{trust_cell}</td>"
            f"<td class='rel'>{_ltr(s['items_last_run'] or '-')}</td>"
            f"<td class='rel'>{lag_cell}</td>"
            f"<td class='tm'>{_ltr(str(s['last_ok_at'] or '-')[:16])}</td>"
            f"<td>{_auto(s['label'])}"
            f"<div class='why'>{html.escape(_trust_he(s['trust']))} &middot; "
            f"השהיה {_ltr(s['latency'])}"
            + (f"<br>{_auto(s['last_error'] or '', 160)}" if s["last_error"] else "")
            + (f"<br>{_auto(s['note'][:200])}" if s["note"] else "")
            + "</div></td></tr>"
        )
    warn_html = "".join(
        f"<div class='warn'>{_coverage_warning_he(e)}</div>"
        for e in views.coverage_warning_entries())
    body = (
        f"<section><h2>{he.UI['coverage_warnings']}</h2>"
        f"{warn_html or '<div class=empty>אין</div>'}</section>"
        f"<section><h2>{he.UI['all_sources']}</h2><table>"
        f"<tr><th class='tkr'>{he.UI['col_source']}</th>"
        f"<th class='rel'>{he.UI['col_status']}</th>"
        f"<th class='rel'>{he.UI['col_trust']}</th>"
        f"<th class='rel'>{he.UI['col_items']}</th>"
        f"<th class='rel'>{he.UI['col_lag']}</th>"
        f"<th class='tm'>{he.UI['col_last_ok']}</th>"
        f"<th>{he.UI['col_what']}</th></tr>"
        + "".join(rows) + "</table>"
        "<p class='note'>עמודת <b>פריטים</b> היא מה שהמעבר האחרון החזיר, לכל מקור. "
        "מקור פעיל שמחזיר 0 במשך ימים אינו אותו דבר כמו שוק שקט — תבדוק את "
        "<b>הצלחה אחרונה</b> לפני שתסיק שאין חדשות.<br>"
        "עמודת <b>פיגור</b> היא החציון של הזמן בין הפרסום לבין הרגע שבו המערכת "
        "ראתה את הפריט, על כל מה שפורסם ב-6 השעות האחרונות. זה המספר שקובע אם "
        "אפשר לסחור על מקור או רק להתעדכן ממנו: ב-5 דקות אפשר לפעול, ב-90 אתה "
        "קורא היסטוריה. מיד אחרי איסוף ראשוני הוא נראה גבוה ליום, כי הפריטים "
        "באמת נראו באיחור.</p></section>"
    )
    return _document(f"{he.UI['nav_sources']} · {he.UI['brand']}", "", body, collect,
                     views.last_update())


# How often a page re-renders itself while a pass is running. A pass takes ~260s
# and a render costs ~54ms, so watching one costs about 2.6s of CPU in total -
# cheap enough that it is not worth a script to avoid.
COLLECT_REFRESH_SEC = 5


def _collect_control(collect: dict[str, Any] | None) -> str:
    """The button, and what it says while a pass is running.

    No JavaScript. The page is server-rendered and the test suite enforces that,
    which rules out the obvious design - POST, then poll from the browser. A
    pass takes ~260s, far past any request timeout, so the click cannot wait for
    it either: POST starts the work and redirects straight back, and while the
    pass runs the document carries a meta refresh so the page reports its own
    progress. That also means a pass started by the hourly scheduled task, or
    from another tab, shows up here without anything having to subscribe.
    """
    status = (collect or {}).get("status")

    if status == "running":
        elapsed = int((collect or {}).get("elapsed_sec") or 0)
        clock = f"{elapsed // 60}:{elapsed % 60:02d}"
        # Not a form: a second pass is a no-op, and offering the click invites
        # the reader to think the first one did not take.
        return (f"<span class='collect running'>{html.escape(he.UI['collect_running'])} "
                f"{_ltr(clock)}</span>")

    note = ""
    if status == "done":
        note = (f"<span class='collect ok'>{html.escape(he.UI['collect_done'])} "
                f"&middot; {html.escape(he.UI['collect_collected'])} "
                f"{_ltr((collect or {}).get('collected', 0))} &middot; "
                f"{html.escape(he.UI['collect_stored'])} "
                f"{_ltr((collect or {}).get('stored', 0))}</span>")
    elif status == "error":
        note = (f"<span class='collect bad' title="
                f"'{html.escape(str((collect or {}).get('error') or ''))}'>"
                f"{html.escape(he.UI['collect_failed'])}</span>")

    return (
        f"<form method='post' action='/collect' class='collect-form'>"
        f"<button id='collect' type='submit' "
        f"title='{html.escape(he.UI['collect_hint'])}'>"
        f"{html.escape(he.UI['collect_run'])}</button></form>{note}"
    )


# --------------------------------------------------------------------------- #
def _document(title: str, header_extra: str, body: str,
              collect: dict[str, Any] | None = None,
              updated: dict[str, Any] | None = None) -> str:
    """The RTL shell. `dir='rtl'` on <html> is what makes every logical CSS
    property in the sheet above resolve the right way round."""
    # Only while a pass is actually running: a page that reloads itself for ever
    # is worse than one that never does, and there is nothing to watch once the
    # pass is finished.
    refresh = (f"<meta http-equiv='refresh' content='{COLLECT_REFRESH_SEC}'>"
               if (collect or {}).get("status") == "running" else "")
    return (
        "<!doctype html><html lang='he' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"{refresh}"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
        f"<header><h1><a href='/'>{he.UI['brand']}</a></h1>"
        # Before anything else: how old is what you are looking at. Every other
        # number on the page is only as good as this one.
        f"{_last_update_he(updated)}"
        f"{header_extra}"
        f"<nav><a href='/'>{he.UI['nav_terminal']}</a>"
        f"<a href='/sources'>{he.UI['nav_sources']}</a>"
        f"<a href='/agent/manifest'>{he.UI['nav_manifest']}</a>"
        f"<a href='/api/morning'>{he.UI['nav_json']}</a></nav>"
        f"{_collect_control(collect)}"
        f"</header><main>{body}</main></body></html>"
    )


def _trust_he(trust: float | None) -> str:
    if trust is None:
        return "מקור לא מוכר - אינו ב-config/sources.yaml"
    for floor, meaning in TRUST_MEANING_HE:
        if trust >= floor:
            return meaning
    return ""


def _provider_he(provider: str | None) -> str:
    return PROVIDER_MEANING_HE.get(
        provider or "", "הספק לא נרשם (ההדפסה קודמת למעקב מקור)")


def _freshness_he(quote: dict[str, Any]) -> str:
    printed = quote.get("market_time")
    fetch_age = quote.get("fetch_age_minutes")
    fetched = (f"נשלף לפני {_ltr(fetch_age)} דק׳" if fetch_age is not None
               else "זמן השליפה לא ידוע")
    if not printed:
        return f"{fetched}; זמן ההדפסה בבורסה לא נרשם, ולכן הגיל האמיתי לא ידוע"
    if quote.get("session") == "closed":
        return (f"השוק סגור — ההדפסה האחרונה של סשן "
                f"{_ltr(he.day_label(printed))} ({_ltr(he.eastern_label(printed))}); "
                f"{fetched}")
    market_age = quote.get("market_age_minutes")
    return (f"עסקה אחרונה לפני {_ltr(market_age)} דק׳ "
            f"({_ltr(he.eastern_label(printed))}); {fetched}; הפיד מושהה מעבר לזה")


def _kv(title: str, pairs: list[tuple[str, str]]) -> str:
    rows = "".join(f"<tr><td class='k'>{html.escape(k)}</td><td>{v}</td></tr>"
                   for k, v in pairs)
    return f"<section><h2>{html.escape(title)}</h2><table class='kv'>{rows}</table></section>"


def _link(url: str | None, cut: int = 80) -> str:
    if not url:
        return "-"
    return (f"<a href='{html.escape(url)}' target='_blank' rel='noreferrer' "
            f"class='ltr'>{html.escape(url[:cut])}"
            f"{'&hellip;' if len(url) > cut else ''}</a>")


_OP = {"base": ("=", "op-base"), "multiply": ("&times;", ""),
       "add": ("+", "op-add"), "cap": ("!", "op-cap"), "note": ("", "")}


def _trace_row(step: dict[str, str]) -> str:
    symbol, cls = _OP.get(step["kind"], ("", ""))
    text = step["step"]
    # The sign is in the symbol column; leaving it in the text too reads as "++4".
    if step["kind"] == "add" and text.startswith("+"):
        text = text[1:]
    hebrew, _ = he.trace_step(text)
    return (f"<tr><td class='op {cls}'>{symbol}</td>"
            f"<td colspan='2' class='{cls}' dir='auto'>{html.escape(hebrew)}</td></tr>")


def _dig(uid: str | None) -> str:
    """The link that turns a headline into evidence."""
    if not uid:
        return ""
    return (f"<a class='dig' href='/item/{html.escape(uid)}' "
            f"title='{he.UI['why_link_title']}'>{he.UI['why_link']}</a>")


def _quote_label(quote: dict[str, Any] | None) -> str:
    """Price provenance, on two clocks.

    "בן 2 דק׳" was the fetch age wearing the price's name. On a Saturday it
    described Friday's closing print as two minutes old, which is the one thing
    a tape panel must never do. The observation time and the fetch time are
    different numbers and now say so separately.
    """
    if not quote:
        return "אין ציטוט שמור"
    provider = quote.get("provider") or "לא ידוע"
    printed = quote.get("market_time")
    fetch_age = quote.get("fetch_age_minutes")
    fetched = (f"נשלף {_ltr(he.ago(quote['fetched_at']))}"
               if quote.get("fetched_at") else
               (f"נשלף לפני {_ltr(fetch_age)} דק׳" if fetch_age is not None
                else "זמן השליפה לא ידוע"))

    if not printed:
        return (f"{_ltr(provider)} &middot; {fetched} &middot; "
                f"<span class='muted'>זמן ההדפסה לא נרשם</span>")

    session = quote.get("session")
    if session == "closed":
        return (f"<span class='muted'>השוק סגור &middot; סשן אחרון: "
                f"{_ltr(he.day_label(printed))}</span><br>"
                f"{_ltr(provider)} &middot; עסקה אחרונה "
                f"{_ltr(he.eastern_label(printed))} &middot; {fetched}")
    return (f"{_ltr(provider)} &middot; עסקה אחרונה "
            f"{_ltr(he.eastern_label(printed))} &middot; {fetched} &middot; מושהה")


def _first_public_he(item: dict[str, Any]) -> str:
    """The two clocks that bracket a coverage gap: when the document became
    readable by anyone, and when we actually fetched it.

    "נודע לנו לפני 5 שעות" is our collection time, and on its own it reads as a
    fresh item. The UFLPA entity list had been on public inspection since Friday
    08:45 ET and was not collected until Sunday: the cell reported the 5 hours
    and hid the 39. Both clocks are shown in ET so the gap between them is the
    obvious thing on the line.
    """
    public = item.get("first_public_at")
    if not public:
        return ""
    line = f"<div class='why'>זמין לציבור {_ltr(he.eastern_label(public))}"
    collected = item.get("discovered_at")
    if collected:
        lag = he.minutes_between(public, collected)
        # A negative gap here would mean we hold it before it was public, which
        # is not something this source can do - the PI filing IS the moment it
        # became public. Say nothing rather than print a lag backwards.
        tail = (f" &middot; פיגור גילוי {html.escape(he.duration(lag))}"
                if lag is not None and lag >= 0 else "")
        line += f"<br>נאסף {_ltr(he.eastern_label(collected))}{tail}"
    return line + "</div>"


def _time_cell(item: dict[str, Any]) -> str:
    """When it was published - or, honestly, when we merely found it."""
    if item.get("published_unknown"):
        seen = he.ago(item.get("discovered_at") or item["t"])
        return (f"<span class='muted'>תאריך לא ידוע</span>"
                f"<div class='why'>נראה {html.escape(seen)}</div>")
    if item.get("forthcoming"):
        # A Federal Register document is readable days before it publishes.
        # Dating it by that future date made it "הרגע"; dating it by when we
        # found it would hide the lead time, which is the only reason we hold
        # the document at all. Say both.
        head = (f"<span class='muted'>מתפרסם "
                f"{_ltr(he.day_label(item['publishes_on']))}</span>")
        public = _first_public_he(item)
        if public:
            return head + public
        seen = he.ago(item.get("discovered_at") or item["t"])
        return head + f"<div class='why'>נודע לנו {html.escape(seen)}</div>"
    # A published Federal Register document was on public inspection days before
    # the date it publishes under, so `t` understates how long the market has had
    # it. Only worth a line when that is materially earlier than `t` - on an
    # already-published PI copy the two are the same instant.
    gap = he.minutes_between(item.get("first_public_at"), item["t"])
    extra = _first_public_he(item) if gap is not None and gap >= 60 else ""
    return html.escape(he.ago(item["t"])) + extra
