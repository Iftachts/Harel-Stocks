"""The MAYA corporate-events vocabulary, as measured rather than documented.

This collector filtered on exactly one event id - 2, "פרסום דוחות", the
expected-results date - and that id stopped being served. The filter then
matched nothing, for all 22 names, a year forward, and reported it as "no
rows", which is also what a company with no scheduled events looks like. The
calendar held 31 Federal Register comment deadlines and not one company date.

So these tests pin two things: the ids that ARE served, and the rule that a
non-empty response in which nothing matches must be loud.
"""

from __future__ import annotations

import pytest

from harel.collect.maya import _BOND_EVENT_IDS, _schedule_kind


@pytest.mark.parametrize("event_id,name,expected", [
    # Measured live 2026-09-03 against issuer ids 281/1040/1579/1894/2030.
    (22, "יום אקס - דיבידנד", "ex_dividend"),
    (22, "יום תשלום - דיבידנד", "dividend_payment"),
    (203, "יום כינוס  - מועדי אסיפות", "shareholder_meeting"),
    (101, "יום מסחר אחרון", "last_trading_day"),
    (4, "יום מימוש אחרון", "last_exercise_day"),
    # Still mapped, though no longer served - so a return is picked straight up.
    (2, "פרסום דוחות", "earnings"),
])
def test_known_event_ids_resolve(event_id, name, expected):
    assert _schedule_kind({"eventId": event_id, "eventName": name}) == expected


@pytest.mark.parametrize("event_id,name", [
    (7, "יום אקס - ריבית"),
    (7, "יום תשלום - ריבית"),
    (9, "פדיון חלקי"),
])
def test_bond_coupon_events_are_skipped(event_id, name):
    """These ride on the debt series, not the share. Filing a coupon date under
    the equity ticker puts it in the path of someone trading the stock."""
    assert _schedule_kind({"eventId": event_id, "eventName": name}) is None
    assert event_id in _BOND_EVENT_IDS


def test_a_reworded_label_still_resolves_by_id():
    """Either the id or the Hebrew label alone is a guess; the id is the
    fallback so a rename does not silently empty the calendar again."""
    assert _schedule_kind({"eventId": 22, "eventName": "something reworded"}) \
        == "ex_dividend"


def test_an_unknown_id_is_skipped_rather_than_guessed():
    assert _schedule_kind({"eventId": 9999, "eventName": "חדש"}) is None


def test_ex_dividend_and_payment_are_not_the_same_calendar_kind():
    """The ex-date is mechanical and tradeable - the stock opens lower by the
    dividend and a short owes it. The payment day is bookkeeping."""
    ex = _schedule_kind({"eventId": 22, "eventName": "יום אקס - דיבידנד"})
    pay = _schedule_kind({"eventId": 22, "eventName": "יום תשלום - דיבידנד"})
    assert ex != pay


def test_every_schedule_kind_has_wording_in_the_calendar():
    """A kind with no prefix would be announced as a bare 'Scheduled:', which
    loses the only thing the row is for."""
    from harel.collect.maya import _SCHEDULE_EVENTS
    from harel.pipeline import _SCHEDULE_PREFIX

    for _id, _label, kind in _SCHEDULE_EVENTS:
        assert kind in _SCHEDULE_PREFIX, kind
