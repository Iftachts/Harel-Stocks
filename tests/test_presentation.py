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


# ------------------------------------------------- collect-on-demand ------- #
def _runner(monkeypatch, duration=0.0, boom=None):
    """A CollectRunner whose pass is instant and never touches the network."""
    import time as _time

    from harel.serve import api as api_mod

    class FakeReport:
        collected, stored, deduped, duration_sec = 147, 75, 72, 1.5
        by_source, warnings, errors = {"globes": 12}, [], []

    class FakePipeline:
        def __init__(self, **kw):
            self.kw = kw

        def run(self):
            if duration:
                _time.sleep(duration)
            if boom:
                raise RuntimeError(boom)
            return FakeReport()

    monkeypatch.setattr(api_mod, "Database", lambda path: type(
        "D", (), {"close": lambda self: None})())
    import harel.pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "Pipeline", FakePipeline)
    return api_mod.CollectRunner(db_path=":memory:")


def test_a_second_click_does_not_start_a_second_pass(monkeypatch):
    """A pass takes ~260s, so the button is clickable again long before the
    first one finishes. Two passes at once is wasted fetching, which is what
    `-MultipleInstances IgnoreNew` prevents for the scheduled task."""
    runner = _runner(monkeypatch, duration=1.0)
    first = runner.start()
    second = runner.start()

    assert first["status"] == "running"
    assert second["already_running"] is True
    assert second["started_at"] == first["started_at"], "the first pass must still own the run"


def test_a_finished_pass_reports_what_it_collected(monkeypatch):
    runner = _runner(monkeypatch)
    runner.start()
    runner._thread.join(timeout=10)

    state = runner.status()
    assert state["status"] == "done"
    assert (state["collected"], state["stored"]) == (147, 75)
    assert state["finished_at"]


def test_a_pass_that_raises_is_reported_not_swallowed(monkeypatch):
    """The thread is the only place this runs, so an exception there would
    otherwise vanish and leave the button saying 'running' for ever."""
    runner = _runner(monkeypatch, boom="the network went away")
    runner.start()
    runner._thread.join(timeout=10)

    state = runner.status()
    assert state["status"] == "error"
    assert "the network went away" in state["error"]
    assert state["finished_at"], "a failed pass still has to stop being 'running'"


def test_the_page_refreshes_itself_only_while_a_pass_is_running():
    """The terminal is script-free, so a meta refresh is how it watches. A page
    that reloads for ever is worse than one that never does, so the tag has to
    be absent in every other state."""
    from harel.serve.terminal import _document

    running = _document("t", "", "", {"status": "running", "elapsed_sec": 63})
    assert "http-equiv='refresh'" in running
    assert "1:03" in running, "the elapsed clock must be readable"

    for state in ({"status": "idle"}, {"status": "done", "collected": 1, "stored": 1},
                  {"status": "error", "error": "x"}, None):
        assert "http-equiv='refresh'" not in _document("t", "", "", state)


def test_the_collect_button_posts_and_never_gets():
    """A GET that collects would be fired by a link prefetcher or a browser
    preview, and would rewrite the database without anyone clicking."""
    from harel.serve.terminal import _collect_control

    idle = _collect_control({"status": "idle"})
    assert "method='post'" in idle and "action='/collect'" in idle

    # While running there is no form at all - a click would be a no-op, and
    # offering it invites the reader to think the first one did not take.
    assert "<form" not in _collect_control({"status": "running", "elapsed_sec": 5})


def test_the_collect_result_isolates_its_numbers():
    """Same rule as every other number on an RTL page."""
    from harel.serve.terminal import _collect_control

    done = _collect_control({"status": "done", "collected": 147, "stored": 75})
    assert "<span class='ltr'>147</span>" in done
    assert "<span class='ltr'>75</span>" in done
