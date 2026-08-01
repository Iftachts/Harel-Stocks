"""The presentation layer's arithmetic of time, and its escaping.

Everything here is a rendering fault rather than a data fault: the pipeline had
the right number and the screen said something else. They are grouped because
they share one root - a duration was being treated as a fact about the calendar,
or a byte count as a fact about characters.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from harel.cli import _ago as cli_ago
from harel.serve import hebrew as he
from harel.serve.terminal import _auto, _detection_lag_he

# A Saturday night in Israel, which is where the calendar-vs-elapsed bug shows:
# 23:11 local means "two days ago" and "48 hours ago" name different days.
NOW = datetime(2026, 8, 1, 20, 11, tzinfo=timezone.utc)


# --------------------------------------------------------- calendar words --- #
def test_yesterday_is_a_calendar_day_not_a_24_hour_bucket():
    """`int(hours // 24) == 1` covers 24h through 47.99h, so a Thursday story
    read "אתמול" on Saturday night. אתמול is a claim about the calendar."""
    assert he.ago((NOW - timedelta(hours=30)).isoformat(), NOW) == "אתמול"
    assert he.ago((NOW - timedelta(hours=47)).isoformat(), NOW) == "אתמול"
    # 47.9h back crosses one more midnight in Tel Aviv than 47h does.
    assert he.ago((NOW - timedelta(hours=47.9)).isoformat(), NOW) == "לפני יומיים"
    assert he.ago((NOW - timedelta(hours=72)).isoformat(), NOW) == "לפני 3 ימים"


def test_tomorrow_is_a_calendar_day_and_reads_one_clock():
    """`ago` and `in_future` each called datetime.now(), so an event exactly 48h
    out floored to one day and announced itself as "מחר" - the day after
    tomorrow. 13 Federal Register documents in the live database did this.

    Anchored in the morning, because from a Saturday night the "מחר" window is
    an hour wide and the interesting cases all land past it.
    """
    morning = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)   # Sat 09:00 IL
    assert he.ago((morning + timedelta(hours=25)).isoformat(), morning) == "מחר"
    assert he.ago((morning + timedelta(hours=40)).isoformat(), morning) == "בעוד יומיים"
    assert he.ago((morning + timedelta(hours=48)).isoformat(), morning) == "בעוד יומיים"


def test_the_reader_s_calendar_is_tel_aviv_s():
    """A MAYA disclosure filed 01:30 Israel time is Sunday's news to a trader
    here and Saturday's to UTC. The terminal is read in Israel."""
    filed = datetime(2026, 8, 1, 22, 30, tzinfo=timezone.utc)   # 2 Aug 01:30 IL
    monday = datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc)    # 3 Aug 09:00 IL
    assert he.ago(filed.isoformat(), monday) == "אתמול"


# ------------------------------------------------------------- dual form --- #
def test_two_takes_the_hebrew_dual_and_carries_no_digit():
    """"לפני 2 שעות" is what a translation engine writes. Hebrew counts two as a
    word, and a word has no Latin run to isolate."""
    assert he.ago((NOW - timedelta(hours=2)).isoformat(), NOW) == "לפני שעתיים"
    assert he.ago((NOW - timedelta(hours=49)).isoformat(), NOW) == "לפני יומיים"
    assert he.ago((NOW + timedelta(hours=2)).isoformat(), NOW) == "בעוד שעתיים"
    for dual in ("לפני שעתיים", "בעוד שעתיים", "לפני יומיים"):
        assert not any(ch.isdigit() for ch in dual)


def test_minutes_keep_their_digit_because_hebrew_has_no_dual_for_them():
    """There is שעתיים and יומיים, but no "דקותיים"."""
    assert he.ago((NOW - timedelta(minutes=2)).isoformat(), NOW) == "לפני 2 דק׳"


def test_one_still_reads_as_a_word():
    assert he.ago((NOW - timedelta(hours=1)).isoformat(), NOW) == "לפני שעה"
    assert he.ago((NOW + timedelta(hours=1, minutes=5)).isoformat(), NOW) == "בעוד שעה"


# -------------------------------------------------------- detection lag ---- #
def test_a_negative_detection_lag_is_lead_time_and_never_reads_as_an_age():
    """collected_at - published_at goes negative for a document that publishes
    on a future date. It was rendering "-220 דקות אחרי הפרסום": a negative count
    of minutes after an event that had not happened yet."""
    rendered = _detection_lag_he(-220)
    assert "-220" not in rendered
    assert "220" in rendered and "לפני" in rendered
    assert "אחרי הפרסום" not in rendered


def test_a_negative_lag_is_not_quietly_classified_as_timely():
    """The old `lag < 30` test was true for every negative number, so the one
    case that most deserved a label got the same silent treatment as a fast
    detection."""
    assert "זמן קדימה" in _detection_lag_he(-1898)


def test_a_real_lag_still_reports_and_still_flags_lateness():
    assert "באיחור" not in _detection_lag_he(12)
    assert "באיחור" in _detection_lag_he(400)
    assert _detection_lag_he(None) == "-"


# ------------------------------------------------------------- cli._ago ---- #
def test_cli_ago_has_the_defences_the_hebrew_parser_has():
    """Both functions read the same column; only one had been hardened."""
    assert cli_ago(None) == "-"
    assert cli_ago("") == "-"
    # A naive timestamp cannot be subtracted from an aware `now` at all.
    assert cli_ago("2026-08-01T10:00:00").endswith(("m", "h", "d"))
    assert cli_ago("garbage") == "garbage"


def test_cli_ago_never_prints_a_negative_age():
    """`harel feed` was printing '-1659m' against real rows: a scheduled
    Federal Register date read as an age."""
    future = (datetime.now(timezone.utc) + timedelta(hours=27)).isoformat()
    rendered = cli_ago(future)
    assert not rendered.startswith("-")
    assert rendered.startswith("in ")


# -------------------------------------------------- escape vs truncate ----- #
def test_a_headline_is_cut_before_it_is_escaped_never_after():
    """Escaping first lets the cut land inside an entity: "AT&T earnings" came
    out as "AT&a", and an apostrophe is six characters once escaped, so the
    length asked for was not the length delivered either."""
    assert _auto("AT&T earnings", 4) == "<span dir='auto'>AT&amp;T</span>"
    assert "AT&a<" not in _auto("AT&T earnings", 4)
    assert _auto("<b>hi", 2) == "<span dir='auto'>&lt;b</span>"


def test_cutting_first_does_not_stop_anything_being_escaped():
    """The fix reorders escape and truncate. It must not drop the escape."""
    dangerous = "<img src=x onerror=alert(1)>"
    rendered = _auto(dangerous)
    assert "<img" not in rendered
    assert html.escape(dangerous) in rendered
