"""Flatline detection: a source can die without ever failing.

A collector that answers empty 200s is stamped fully healthy on every pass -
fresh last_ok_at, zero consecutive_failures - so nothing in source_state can
ever notice it. The one witness that cannot be fooled is the items table:
a source that used to produce and has stored nothing for longer than its
cadence explains is flat, and health() has to say so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harel.views import Views


def _add_item(db, source: str, uid: str, hours_ago: float,
              source_kind: str = "rss") -> None:
    now = datetime.now(timezone.utc)
    db.conn.execute(
        "INSERT INTO items (uid, source, source_kind, external_id, title, "
        "published_at, collected_at) VALUES (?,?,?,?,?,?,?)",
        (uid, source, source_kind, uid, "t",
         (now - timedelta(hours=hours_ago + 1)).isoformat(),
         (now - timedelta(hours=hours_ago)).isoformat()),
    )
    db.conn.commit()


def _health_lists(config, db):
    health = Views(db=db, config=config).health()
    return {f["source"]: f for f in health["flatlined"]}, health["never_produced"]


def test_a_fast_source_silent_beyond_its_cadence_is_flagged(config, db):
    """google_news polls every five minutes; a hundred silent hours from it is
    not a quiet tape, whatever its failure counter says."""
    _add_item(db, "google_news", "old", hours_ago=100.0)
    flat, never = _health_lists(config, db)

    assert "google_news" in flat
    entry = flat["google_news"]
    assert entry["silent_hours"] == pytest.approx(100.0, abs=0.5)
    assert entry["threshold_hours"] == 72
    assert entry["last_item_at"]
    assert "google_news" not in never


def test_a_source_inside_its_threshold_is_not_flagged(config, db):
    _add_item(db, "google_news", "fresh", hours_ago=1.0)
    flat, _ = _health_lists(config, db)
    assert "google_news" not in flat


def test_the_threshold_follows_the_sources_stated_cadence(config, db):
    """openFDA is routinely quiet for days and means nothing by it; a daily
    source only turns red after two weeks, not after three days."""
    _add_item(db, "fda_enforcement", "quiet", hours_ago=5 * 24.0,
              source_kind="openfda")
    _add_item(db, "ema_news", "gone", hours_ago=15 * 24.0)
    flat, _ = _health_lists(config, db)

    assert "fda_enforcement" not in flat
    assert "ema_news" in flat
    assert flat["ema_news"]["threshold_hours"] == 14 * 24


def test_a_source_that_never_produced_is_informational_not_red(config, db):
    """Zero items ever is a coverage fact - a key not yet supplied, a feed not
    yet live - and turning it red would make doctor permanently alarming."""
    flat, never = _health_lists(config, db)
    assert flat == {}
    assert "google_news" in never


def test_price_sources_are_exempt(config, db):
    """Prices write snapshots, not items, so an items-table silence says
    nothing about them - in either direction."""
    _add_item(db, "prices_yahoo", "px", hours_ago=1000.0, source_kind="prices")
    flat, never = _health_lists(config, db)

    assert "prices_yahoo" not in flat
    assert "prices_yahoo" not in never


def test_a_disabled_source_cannot_flatline(config, db):
    """calcalist is off on purpose, with the finding written up in the config.
    Its old items must not buy a permanent red line for expected silence."""
    _add_item(db, "calcalist", "off", hours_ago=5000.0)
    flat, never = _health_lists(config, db)

    assert "calcalist" not in flat
    assert "calcalist" not in never


def test_doctor_prints_the_flatline_and_still_exits_zero(db, tmp_path, capsys):
    """The finding has to reach the operator's screen, but doctor's contract is
    exit 0 - a standing exit 1 would break every script that wraps it."""
    from harel.cli import main

    _add_item(db, "google_news", "old", hours_ago=200.0)
    code = main(["--db", str(tmp_path / "test.db"), "doctor"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Flatlined sources - nothing stored for longer than " \
           "their cadence explains" in out
    assert "google_news" in out and "silent 200h" in out and "threshold 72h" in out
    assert "have never stored an item" in out
