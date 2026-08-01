"""Hebrew for the terminal.

The HTML terminal is Hebrew and right-to-left. The REST API and the MCP tools
stay English, because that is the language the downstream agent is instructed
in - so this module is a presentation layer, not a translation of the data
model. Nothing here changes what is stored or scored.

Two kinds of string arrive from the pipeline in English and have to be spoken
Hebrew on screen:

* link explanations (``why``) - "names \"Teva\" in headline"
* scoring-trace steps          - "source trust x0.60 (google_news)"

Both are generated from a small, stable set of shapes, so they are rewritten by
an ordered pattern table rather than translated as prose. Anything unmatched
falls through unchanged and is rendered inside an LTR island, which is ugly but
honest - better a legible English fragment than a wrong Hebrew one.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..views import ISRAEL_TZ

# --------------------------------------------------------------- chrome ---- #
UI = {
    "brand": "טרמינל הראל",
    "nav_terminal": "טרמינל",
    "nav_sources": "מקורות",
    "nav_manifest": "מניפסט",
    "nav_json": "JSON",

    "coverage_warnings": "אזהרות כיסוי",
    "unexplained": "תנועות ללא הסבר",
    "news_alerts": "התראות חדשות (24 שעות)",
    "movers": "מנייעים",
    "feed": "פיד",
    "feed_below": "פיד (מתחת לסף)",
    "tase_overnight": "מאיה — דיווחי לילה",
    "calendar": "לוח אירועים (7 ימים)",
    "all_sources": "כל המקורות",

    "col_score": "ציון",
    "col_symbol": "סימבול",
    "col_relation": "קשר",
    "col_time": "זמן",
    "col_headline": "כותרת",
    "col_source": "מקור",
    "col_change": "שינוי",
    "col_volume": "מחזור",
    "col_vs_sector": "מול הסקטור",
    "col_driver": "גורם",
    "col_date": "תאריך",
    "col_kind": "סוג",
    "col_event": "אירוע",
    "col_status": "מצב",
    "col_trust": "אמון",
    "col_items": "פריטים",
    "col_lag": "פיגור",
    "col_last_ok": "הצלחה אחרונה",
    "col_what": "מה זה",
    "col_link": "קישור",
    "col_why_symbol": "למה הסימבול הזה",
    "col_arithmetic": "החשבון, ומאיפה הוא הגיע",

    "items": "פריטים",
    "alerts_24h": "התראות/24ש",
    "sources_live": "מקורות פעילים",

    "no_alerts": "אין התראות חדשות ב-24 השעות האחרונות",
    "db_empty": "המסד ריק — הרץ harel collect",
    "nothing_collected": "לא נאסף שום דבר ב-24 השעות האחרונות — בדוק את עמוד "
                         "המקורות לפני שתניח שהשוק היה שקט",
    "nothing_to_show": "אין מה להציג",
    "why_link": "למה?",
    "why_link_title": "מאיפה זה הגיע ואיך הוא קיבל את הציון",
}

EVENT_LABEL = {
    "merger_acquisition": "מיזוג/רכישה",
    "regulatory_decision_primary": "החלטת רגולטור",
    "clinical_readout": "תוצאות קליניות",
    "short_seller_report": "דוח שורט",
    "guidance_change": "שינוי תחזית",
    "equity_offering": "הנפקת מניות",
    "earnings": "דוחות",
    "major_contract": "חוזה משמעותי",
    "litigation_outcome": "תוצאת הליך משפטי",
    "compliance_action": "פעולת אכיפה",
    "index_event": "אירוע מדד",
    "rating_change": "שינוי המלצה",
    "listing_compliance": "עמידה בכללי רישום",
    "operational_disruption": "שיבוש תפעולי",
    "macro_sector_policy": "מדיניות מאקרו/סקטור",
    "commodity_move": "תנועת סחורה",
    "partnership_licensing": "שותפות/רישוי",
    "management_change": "שינוי הנהלה",
    "capital_return": "החזר הון",
    "insider_activity": "פעילות בעל עניין",
    "product_launch": "השקת מוצר",
    "patent_grant": "רישום פטנט",
    "conference_presentation": "הצגה בכנס",
    "award_recognition": "פרס/הכרה",
}


def event_label(key: str) -> str:
    return EVENT_LABEL.get(key, key)


# Relations, for the places they appear inside a sentence rather than a column.
RELATION_INLINE = {
    "direct": "ישיר", "subsidiary": "חברה־בת", "product_rival": "מוצר מתחרה",
    "customer": "לקוח", "peer": "מתחרה", "supplier": "ספק",
    "sector_reg": "רגולציה סקטוריאלית", "sector_theme": "תמה סקטוריאלית",
    "macro": "מאקרו",
}

# Calendar labels are composed in English by the pipeline. Only the prefix is
# ours - the tail is usually a headline, which stays in its own language.
_CALENDAR_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^Q([1-4]) results \(company-announced date\)$"),
     r"תוצאות Q\1 (תאריך שהחברה פרסמה)"),
    (re.compile(r"^Results results \(company-announced date\)$"),
     r"תוצאות (תאריך שהחברה פרסמה)"),
    # A date restated by an aggregator is still a date, but the reader has to be
    # able to tell it apart from one the issuer published itself.
    (re.compile(r"^Q([1-4]) results \(reported by (.+)\)$"),
     r"תוצאות Q\1 (לפי \2, לא מהחברה)"),
    (re.compile(r"^Results results \(reported by (.+)\)$"),
     r"תוצאות (לפי \1, לא מהחברה)"),
    (re.compile(r"^Rule effective: (.+)$"), r"תקנה נכנסת לתוקף: \1"),
    (re.compile(r"^Comment deadline: (.+)$"), r"מועד אחרון להערות: \1"),
    (re.compile(r"^Expected results: (.+)$"), r"תוצאות צפויות: \1"),
    (re.compile(r"^(.*?): (\S+) primary completion \((.*)\)$"),
     r"\1: סיום ראשי של \2 (\3)"),
]


def calendar_label(text: str) -> str:
    rendered, _ = _apply(_CALENDAR_RULES, text or "")
    return rendered


# ------------------------------------------------------------------ time --- #
# Sunday-first, matching datetime.isoweekday() % 7.
_WEEKDAY_HE = ("א׳", "ב׳", "ג׳", "ד׳", "ה׳", "ו׳", "ש׳")


def _parse(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Hebrew counts one and two as words rather than as digits, and the dual is not
# optional politeness: "לפני 2 שעות" is what a translation engine writes, not
# what a reader writes. Note that a dual carries no digit at all, so a caller
# must not wrap these in a numeric LTR island - there is nothing Latin in them.
_DUAL = {"שעות": "שעתיים", "ימים": "יומיים"}


def _count(whole: int, unit: str) -> str:
    return _DUAL[unit] if whole == 2 else f"{whole} {unit}"


def _calendar_days(earlier: datetime, later: datetime) -> int:
    """Whole days between two instants on the reader's calendar.

    "אתמול" and "מחר" are calendar words and were being computed from an elapsed
    24-hour bucket, where `int(hours // 24) == 1` covers 24h through 47.99h. On
    Saturday evening a story from Thursday read "אתמול", and the 13 Federal
    Register documents dated Monday read "מחר".

    The calendar is Tel Aviv's, not UTC's, because the reader is: a MAYA
    disclosure filed 01:30 Israel time is Sunday's news to a trader here and
    Saturday's to UTC, and on Monday morning that is the difference between
    "אתמול" and a false "לפני יומיים".
    """
    return (later.astimezone(ISRAEL_TZ).date()
            - earlier.astimezone(ISRAEL_TZ).date()).days


def ago(iso: str, now: datetime | None = None) -> str:
    """Relative time, Hebrew, with the grammar the numbers actually need."""
    dt = _parse(iso)
    if dt is None:
        return str(iso)[:16]
    now = now or datetime.now(timezone.utc)
    minutes = (now - dt).total_seconds() / 60
    # A timestamp in the future used to fall through to "הרגע", because -2880
    # minutes is also less than one. That is how a Federal Register document
    # scheduled to publish on Monday was presented on Saturday night as having
    # just come out. A future date is a schedule, not an age, and has to say so.
    if minutes < 0:
        return in_future(dt, now)
    if minutes < 1:
        return "הרגע"
    # No dual here: Hebrew has שעתיים and יומיים but no "דקותיים", so the
    # minutes bucket keeps its digit.
    if minutes < 60:
        return f"לפני {int(minutes)} דק׳"
    hours = minutes / 60
    if hours < 24:
        whole = int(hours)
        return "לפני שעה" if whole == 1 else "לפני " + _count(whole, "שעות")
    days = _calendar_days(dt, now)
    # 24 elapsed hours cannot land on the same calendar date, so the floor here
    # is yesterday.
    return "אתמול" if days <= 1 else "לפני " + _count(days, "ימים")


def in_future(dt: datetime, now: datetime | None = None) -> str:
    # `ago` read the clock and then this function read it again, so the two
    # disagreed by the microseconds between the calls and an event exactly 48h
    # out floored to one day - "מחר" for the day after tomorrow. One clock,
    # passed in.
    now = now or datetime.now(timezone.utc)
    minutes = (dt - now).total_seconds() / 60
    if minutes < 60:
        return "בעוד פחות משעה"
    hours = minutes / 60
    if hours < 24:
        whole = int(hours)
        return "בעוד שעה" if whole == 1 else "בעוד " + _count(whole, "שעות")
    days = _calendar_days(now, dt)
    return "מחר" if days <= 1 else "בעוד " + _count(days, "ימים")


def weekday(dt: datetime) -> str:
    return _WEEKDAY_HE[dt.isoweekday() % 7]


def day_label(iso: str) -> str:
    """`ו׳ 31.7` - the weekday a trader thinks in, plus the date."""
    dt = _parse(iso)
    if dt is None:
        return str(iso)[:10]
    return f"{weekday(dt)} {dt.day}.{dt.month}"


def eastern_label(iso: str) -> str:
    """`ו׳ 16:00 ET` - the exchange's own clock, which is the one that decides
    whether a print is a closing print.

    Month-based DST approximation, as in `collect.prices.current_session`: ET is
    UTC-4 from March to November and UTC-5 otherwise. Wrong for a few days a
    year on either side of the switch, by one hour, on a label.
    """
    dt = _parse(iso)
    if dt is None:
        return str(iso)[:16]
    et = dt - timedelta(hours=4 if 3 <= dt.month <= 11 else 5)
    return f"{weekday(et)} {et:%H:%M} ET"


# ------------------------------------------------- explanations & traces --- #
# Every rule was written against the strings actually present in the database,
# not against the code that emits them - which is how the "in headline" /
# "in body" suffix and the compound base reasons were found.
#
# Ordered: first match wins, so the specific shapes come before the general.
_WHY_RULES: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r'^the story names "(.+?)", tracked as a rival product for (\w+)$'),
     r'הידיעה מזכירה "\1", מוצר מתחרה שאנחנו עוקבים אחריו עבור \2'),
    (re.compile(r'^the story names "(.+?)", tracked as a peer company for (\w+)$'),
     r'הידיעה מזכירה "\1", חברה מתחרה שאנחנו עוקבים אחריה עבור \2'),
    (re.compile(r'^found by our "(.+?)" search$'), r'נמצא בחיפוש "\1" שלנו'),
    # The relation token is data, but inside a Hebrew sentence it reads as an
    # untranslated leftover, so it is spelled out.
    (re.compile(r"^collected from (\S+) as (\S+)$"),
     lambda m: f"נאסף מ-{m.group(1)} בתור "
               f"{RELATION_INLINE.get(m.group(2).lower(), m.group(2))}"),
    (re.compile(r"^(\S+) matched entity directly$"), r"\1 זיהה את הישות ישירות"),
    # EDGAR full-text: somebody else's filing names us. The linker joins this
    # with "; " so it arrives here already split into two chunks.
    # The form type can contain spaces - "DEF 14A", "NT 10-Q", "EX-99.1".
    (re.compile(r"^(.+?)'s own ([\w./-]+(?: [\w./-]+)?) filing contains (.+)$"),
     r'ההגשה (\2) של \1 מכילה \3'),
    (re.compile(r"^this is somebody else's document, not (\w+)'s$"),
     r"זה מסמך של מישהו אחר, לא של \1"),
    (re.compile(r'^(.+?): regulator document mentions "(.+?)"$'),
     r'\1: מסמך רגולטורי מזכיר "\2"'),
    (re.compile(r'^names "(.+?)"$'), r'מזכיר "\1"'),
    (re.compile(r"^symbol (\S+)$"), r"הסימבול \1"),
    (re.compile(r"^peer symbol (\S+)$"), r"סימבול של מתחרה: \1"),
    (re.compile(r'^competitor "(.+?)"$'), r'מתחרה: "\1"'),
    (re.compile(r'^our product "(.+?)"$'), r'המוצר שלנו: "\1"'),
    (re.compile(r'^product "(.+?)"$'), r'מוצר: "\1"'),
    (re.compile(r'^rival product "(.+?)"$'), r'מוצר מתחרה: "\1"'),
    (re.compile(r'^rival program "(.+?)"$'), r'תוכנית מתחרה: "\1"'),
    (re.compile(r'^theme "(.+?)"$'), r'תמה: "\1"'),
    (re.compile(r'^dependency "(.+?)"$'), r'תלות: "\1"'),
    (re.compile(r'^demand driver "(.+?)" \((.+?)\)$'), r'מנוע ביקוש: "\1" (\2)'),
    (re.compile(r'^regulator for (.+)$'), r'רגולטור של \1'),
]

# Where the match was found - appended by the linker to most of the above.
_WHERE = [(" in headline", " בכותרת"), (" in body", " בגוף הידיעה")]

_TRACE_RULES: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"^event=(\S+) base=(\S+) \((.+)\)$"),
     r"אירוע=\1 בסיס=\2 (התאמה: \3)"),
    (re.compile(r"^no taxonomy match, default base=(\S+)$"),
     r"אין התאמה בטקסונומיה — בסיס ברירת מחדל=\1"),
    (re.compile(r"^source trust x(\S+) \((.+)\)$"), r"אמון המקור ×\1 (\2)"),
    (re.compile(r"^age (\S+)h -> recency x(\S+)$"), r"גיל \1 ש׳ ← דעיכת זמן ×\2"),
    (re.compile(r"^relation (\S+) override x(\S+)$"),
     lambda m: f"קשר {RELATION_INLINE.get(m.group(1).lower(), m.group(1))} "
               f"(דריסה) ×{m.group(2)}"),
    (re.compile(r"^relation (\S+) x(\S+)$"),
     lambda m: f"קשר {RELATION_INLINE.get(m.group(1).lower(), m.group(1))} "
               f"×{m.group(2)}"),
    (re.compile(r"^float (\S+) x(\S+) \(dilution-sensitive\)$"),
     r"סחירות \1 ×\2 (רגיש לדילול)"),
    (re.compile(r"^float (\S+) x(\S+)$"), r"סחירות \1 ×\2"),
    (re.compile(r"^link confidence x(\S+)$"), r"ביטחון הקישור ×\1"),
    (re.compile(r"^\+?(\S+) published pre-market \((.+?)\)$"),
     r"+\1 פורסם בטרום-מסחר (\2)"),
    (re.compile(r"^\+?(\S+) published intraday \((.+?)\)$"),
     r"+\1 פורסם תוך-יומי (\2)"),
    (re.compile(r"^\+?(\S+) tape confirms \((.+?)\)$"), r"+\1 אישור מהטייפ (\2)"),
    (re.compile(r"^\+?(\S+) volume (\S+) ADV$"), r"+\1 מחזור \2 מ-ADV"),
    (re.compile(r"^\+?(\S+) ticker keyword boost$"), r"+\1 בונוס מילת מפתח לטיקר"),
    (re.compile(r"^noise cap (\S+) applied$"), r"תקרת רעש \1 הופעלה"),
    (re.compile(r"^capped at (\S+): low-trust source with no corroboration$"),
     r"נחסם ב-\1: מקור בעל אמון נמוך, ללא אישוש ממקור אחר"),
    (re.compile(r"^forced floor (\S+) \(synthetic tape alert\)$"),
     r"רצפה כפויה \1 (התראת טייפ סינתטית)"),
    # Tails the scorer appends to the base reason.
    (re.compile(r"^raised to (\S+) by 8-K severity '(.+?)' \((.*)\)$"),
     r"הועלה ל-\1 בגלל חומרת סעיפי 8-K '\2' (\3)"),
    (re.compile(r"^capped at (\S+): only low-severity 8-K items \((.*)\)$"),
     r"נחסם ב-\1: רק סעיפי 8-K בחומרה נמוכה (\2)"),
]

_TRACE_TAILS = [
    (" +6 (public inspection: ~1 business day of lead time)",
     " ‎+6 (public inspection: יום עסקים אחד של הקדמה)"),
    (" +5 (TASE disclosure ahead of the US session)",
     " ‎+5 (דיווח לבורסה בת״א לפני הסשן האמריקאי)"),
]


def _apply(rules: list[tuple[re.Pattern[str], Any]], text: str) -> tuple[str, bool]:
    for pattern, replacement in rules:
        new, count = pattern.subn(replacement, text.strip())
        if count:
            return new, True
    return text, False


def _why_chunk(chunk: str) -> tuple[str, bool]:
    suffix = ""
    for english, hebrew in _WHERE:
        if chunk.endswith(english):
            chunk, suffix = chunk[: -len(english)], hebrew
            break
    rendered, hit = _apply(_WHY_RULES, chunk)
    return rendered + suffix, hit


def why(text: str) -> tuple[str, bool]:
    """(Hebrew explanation, was_every_part_translated).

    The linker joins multi-part reasons with '; ', so each part is rewritten on
    its own and one unknown shape does not force the whole line back to English.
    """
    if not text:
        return "", True
    parts, all_hit = [], True
    for chunk in text.split("; "):
        rendered, hit = _why_chunk(chunk)
        parts.append(rendered)
        all_hit = all_hit and hit
    return "; ".join(parts), all_hit


def trace_step(text: str) -> tuple[str, bool]:
    """(Hebrew trace line, was_it_fully_translated).

    Base reasons are compound - "no taxonomy match, default base=28 -> raised to
    72 by 8-K severity 'high' (…)" - so the arrow-joined segments and the
    appended tails are handled piece by piece.
    """
    text = (text or "").strip()
    if not text:
        return "", True
    for english, hebrew in _TRACE_TAILS:
        if text.endswith(english):
            head, hit = trace_step(text[: -len(english)])
            return head + hebrew, hit

    # Whole line first: "age 81.1h -> recency x0.25" contains an arrow of its
    # own, and splitting on it before matching broke every recency step.
    whole, hit = _apply(_TRACE_RULES, text)
    if hit:
        return whole, True

    segments, all_hit = [], True
    for segment in text.split(" -> "):
        rendered, hit = _apply(_TRACE_RULES, segment)
        segments.append(rendered)
        all_hit = all_hit and hit
    return " ← ".join(segments), all_hit
