"""`harel verify-feeds`.

The command exists because the old one printed "OK, 10 entries" for ORA's feed
on every pass for five weeks - a true statement, made while the collector
discarded all ten entries and ORA had no reporting date in the calendar at all.
Liveness is not the question. What comes out of the far end is.

Every test here drives the real RssCollector over recorded fixtures. A verifier
that needs the network to be verified is a verifier nobody runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from harel.cli import main

from conftest import FakeHttpClient, fixture_text


def _issuer_routes(published_days_ago: int = 32, report_in_days: int = 20):
    """An ORA-shaped title-only feed plus the release page behind it.

    Both dates are rendered relative to today, because both ends of what is
    under test are relative: the entry has to be older than the news window, and
    the date it announces has to be inside the extractor's 120-day horizon. The
    same shape as the collector tests use, rebuilt here rather than imported -
    a test module importing another test module breaks the moment either is
    renamed, and this one has to stay runnable on its own.
    """
    published = datetime.now(timezone.utc) - timedelta(days=published_days_ago)
    report = datetime.now(timezone.utc) + timedelta(days=report_in_days)
    call = report + timedelta(days=1)
    prior = published - timedelta(days=60)
    # %-d is not portable to Windows; build the day number by hand.
    page = fixture_text("ir_release_aspx.html").replace(
        "{PRIOR_DATE}", f"{prior:%B} {prior.day}, {prior.year}"
    ).replace(
        "{RELEASE_DAY}", f"{published.month}/{published.day}/{published.year}"
    ).replace(
        "{RELEASE_LONG}", f"{published:%B} {published.day}, {published.year}"
    ).replace(
        "{REPORT_DATE}", f"{report:%A}, {report:%B} {report.day}, {report.year}"
    ).replace(
        "{CALL_DATE}", f"{call:%A}, {call:%B} {call.day}, {call.year}"
    )
    feed = fixture_text("ir_feed_title_only.xml").replace(
        "{PUBLISHED}", published.strftime("%a, %d %b %Y %H:%M:%S +0000"))
    return ({"investor.ormat.com/rss": feed,
             "investor.ormat.com/news-events": page},
            report.date().isoformat())


def _verify(monkeypatch, capsys, routes, *argv, as_json=True):
    """Run the command end to end. Returns (payload or text, client, exit code)."""
    clients: list[FakeHttpClient] = []

    def build(**kwargs):
        clients.append(FakeHttpClient(routes))
        return clients[-1]

    # Imported inside the command as `from .http import HttpClient`, so it is
    # looked up on the module at call time.
    monkeypatch.setattr("harel.http.HttpClient", build)
    code = main((["--json"] if as_json else []) + ["verify-feeds", *argv])
    out = capsys.readouterr().out
    return (json.loads(out) if as_json else out), clients[0], code


def _feed(payload, label: str) -> dict:
    return next(f for f in payload["feeds"] if f["label"] == label)


# ------------------------------------------------------ the ORA incident -- #
def test_a_live_feed_is_reported_by_what_reaches_the_calendar(monkeypatch, capsys):
    """The whole point. ORA's feed answered, and the one entry that mattered was
    five weeks old, carried no summary, and named its reporting date only in the
    page behind the link. "10 entries" said none of that."""
    routes, expected = _issuer_routes()
    payload, _, code = _verify(monkeypatch, capsys, routes, "--only", "ormat")

    feed = _feed(payload, "ORA IR")
    assert feed["verdict"] == "OK"
    assert feed["fetch_ok"] and feed["http_status"] == 200
    assert feed["entries_seen"] == 2
    assert feed["bodies_fetched"] == 1, "the feed withholds the release; one fetch"
    assert feed["emitted"] == 1 and feed["linked"] == 1
    assert feed["dates_parsed"] == 1
    assert feed["future_events_emitted"] == 1
    assert feed["first_party_events_emitted"] == 1, \
        "the issuer's own feed, so the date is company-announced and not hearsay"
    assert payload["calendar"] == [
        {"ticker": "ORA", "date": expected, "label": "Q2 results",
         "first_party": True, "feed": "ORA IR"}
    ]
    assert code == 0


def test_the_report_agrees_with_the_collector_it_claims_to_describe(
        monkeypatch, capsys, config, db):
    """The design rests on driving the real RssCollector rather than a second
    copy of its rules, so assert exactly that: the entries the report calls
    emitted are the ones a collection pass yields from the same feed. A parallel
    reimplementation would pass every other test in this file and still be wrong
    about the only thing this command is for."""
    from harel.collect.base import CollectorContext
    from harel.collect.rss import RssCollector

    routes, _ = _issuer_routes()
    payload, _, _ = _verify(monkeypatch, capsys, routes, "--only", "ormat")

    collector = RssCollector(
        config.sources["company_ir_rss"],
        CollectorContext(config=config, client=FakeHttpClient(routes), db=db,
                         lookback_hours=72.0),
    )
    collected = {item.title for item in collector.collect()
                 if "ORA" in item.seed_tickers}
    reported = {r["title"] for r in _feed(payload, "ORA IR")["records"]
                if r["emitted"]}
    assert reported == collected != set()


def test_a_date_that_is_read_and_then_thrown_away_is_reported_loudly(
        monkeypatch, capsys):
    """The regression alarm. Push the announcement past the issuer back-read and
    the collector drops it - which is the state ORA was in - while the release
    body still says when the company reports. A calendar that is missing a date
    the system HAD is invisible everywhere else in the terminal."""
    routes, expected = _issuer_routes(published_days_ago=100, report_in_days=20)
    payload, _, _ = _verify(monkeypatch, capsys, routes, "--only", "ormat")

    feed = _feed(payload, "ORA IR")
    assert feed["verdict"] == "MUTE", "alive, and emitting nothing"
    assert feed["future_events_emitted"] == 0
    lost = payload["dates_not_reaching_the_calendar"]
    assert len(lost) == 1, lost
    assert lost[0]["date"] == expected
    assert "issuer back-read" in lost[0]["reason"]


def test_every_discarded_record_says_why(monkeypatch, capsys):
    """A discard without a reason is the old report with more columns."""
    routes, _ = _issuer_routes()
    payload, _, _ = _verify(monkeypatch, capsys, routes, "--only", "ormat")

    records = _feed(payload, "ORA IR")["records"]
    assert len(records) == 2, "one record per entry, whatever became of it"
    dropped = [r for r in records if r["outcome"] == "dropped"]
    assert [r["reason"] for r in dropped] and all(r["reason"] for r in dropped)
    assert dropped[0]["title"].startswith("Ormat Commences Commercial Operation")
    # Both legs named: an issuer entry this old is kept only when it announces a
    # date, and "too old" alone would not say which of the two it failed.
    assert dropped[0]["reason"] == ("outside the 72h news window and its headline "
                                    "announces no results date")


def test_the_reason_carries_no_age_so_like_discards_group(monkeypatch, capsys):
    """Eight entries dropped for one reason have to print as one line. With the
    age inside the reason string they were eight lines differing only in "137d",
    and the finding was buried by the evidence for it."""
    routes, _ = _issuer_routes()
    payload, _, _ = _verify(monkeypatch, capsys, routes, "--only", "ormat")

    dropped = [r for r in _feed(payload, "ORA IR")["records"]
               if r["outcome"] == "dropped"]
    assert dropped[0]["age"], "the age travels beside the reason"
    assert dropped[0]["age"] not in dropped[0]["reason"]


# ------------------------------------------------------- fetch vs silence -- #
def test_a_dead_feed_is_not_reported_as_a_quiet_one(monkeypatch, capsys):
    """`_read_feed` swallows 403/404/410 itself - it warns and returns - so
    without the client tap a feed that has MOVED and a feed that is merely quiet
    both arrive as "no entries", which is the conflation this command ends."""
    payload, _, code = _verify(monkeypatch, capsys, {}, "--only", "ormat")

    feed = _feed(payload, "ORA IR")
    assert feed["verdict"] == "DEAD"
    assert feed["fetch_ok"] is False and feed["http_status"] == 404
    assert feed["entries_seen"] == 0
    assert code == 1, "an operator's script has to be able to notice"


def test_a_feed_that_parses_to_nothing_is_empty_not_dead(monkeypatch, capsys):
    """Different fault, different fix: EMPTY is a feed whose publisher stopped
    filling it, DEAD is a URL that no longer exists."""
    hollow = ("""<?xml version="1.0"?><rss version="2.0"><channel>"""
              """<title>Ormat</title><link>https://investor.ormat.com/</link>"""
              """<description>x</description></channel></rss>""")
    payload, _, _ = _verify(monkeypatch, capsys, {"investor.ormat.com": hollow},
                            "--only", "ormat")

    feed = _feed(payload, "ORA IR")
    assert feed["verdict"] == "EMPTY"
    assert feed["fetch_ok"] is True and feed["entries_seen"] == 0


# ------------------------------------------------ it is only a diagnostic -- #
def test_the_check_never_opens_the_database(monkeypatch, capsys, tmp_path):
    """Two reasons this must not share `source_state`. It would stamp
    last_ok_at / items_last_run for a pass that stored nothing - and, worse for
    this command, `_read_feed` sends the stored ETag, so a healthy feed that has
    not changed since the last `collect` answers 304 and the report would say
    "0 entries" about a feed that is fine."""
    routes, _ = _issuer_routes()
    db_path = tmp_path / "never-created.db"
    clients: list[FakeHttpClient] = []
    monkeypatch.setattr("harel.http.HttpClient",
                        lambda **kw: clients.append(FakeHttpClient(routes))
                        or clients[-1])
    main(["--db", str(db_path), "--json", "verify-feeds", "--only", "ormat"])
    capsys.readouterr()

    assert not db_path.exists(), "a diagnostic that writes is not a diagnostic"
    assert not any("If-None-Match" in headers
                   for headers in clients[0].headers_seen), \
        "a conditional GET here would report a healthy feed as empty"


def test_a_source_that_is_off_in_config_is_not_fetched(monkeypatch, capsys):
    """dsca_fms, echa_reach and calcalist are disabled BECAUSE the host blocks
    us, with the finding written up in sources.yaml. Re-checking them bought
    three permanent red lines, and a report that is always partly red is a
    report nobody reads to the bottom."""
    routes, _ = _issuer_routes()
    payload, client, _ = _verify(monkeypatch, capsys, routes)

    assert set(payload["sources_off"]) == {"dsca_fms", "echa_reach", "calcalist"}
    fetched = " ".join(client.calls)
    assert "calcalist.co.il" not in fetched and "dsca.mil" not in fetched
    assert not any(f["source"] in payload["sources_off"] for f in payload["feeds"])


# ------------------------------------------- reasons the collector really uses #
def test_a_loose_search_result_is_reported_as_one(monkeypatch, capsys):
    """Google News answers a query loosely. "PH, US allot P42b for anti-TB, HIV
    drive" was collected as DIRECT news about Allot Ltd until the collector
    started checking the text; the discard is right, and silence about it is
    how a search that has stopped returning anything real looks identical to a
    search that is working."""
    fresh = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <title>Google News</title><link>https://news.google.com/</link>
      <description>x</description>
      <item><title>PH, US allot P42b for anti-TB, HIV drive</title>
      <link>https://example.com/ph</link>
      <description>Health department budget</description>
      <pubDate>{fresh}</pubDate></item></channel></rss>"""
    payload, _, _ = _verify(monkeypatch, capsys, {"news.google.com": feed},
                            "--queries", "--only", "ALLT news")

    feed_report = _feed(payload, "ALLT news")
    assert feed_report["entries_seen"] == 1 and feed_report["emitted"] == 0
    assert feed_report["verdict"] == "MUTE"
    reason = feed_report["records"][0]["reason"]
    assert "never names Allot Ltd" in reason, reason


def test_the_google_news_searches_are_opt_in(monkeypatch, capsys):
    """Seventy extra requests to a host that rate-limits us to two a second is
    a different command from a thirty-second health check."""
    routes, _ = _issuer_routes()
    payload, client, _ = _verify(monkeypatch, capsys, routes)

    assert not any("news.google.com" in call for call in client.calls)
    assert not any(f["source"] == "google_news" for f in payload["feeds"])


def test_an_item_the_pipeline_would_drop_is_not_counted_as_delivered(
        monkeypatch, capsys):
    """A wire feed's job is items that touch the universe. Emitting fifteen
    stories that link to nobody is a feed doing nothing, and `emitted` alone
    would have called it a good day."""
    fresh = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel>
      <title>FDA</title><link>https://fda.gov</link><description>x</description>
      <item><title>FDA Grants Marketing Authorization for a Diagnostic Test</title>
      <link>https://www.fda.gov/x</link>
      <description>No company in our universe is named here.</description>
      <pubDate>{fresh}</pubDate></item></channel></rss>"""
    # Narrow enough to pick one feed: "press-releases" alone also matches TATT's
    # and ORMP's WordPress IR feeds, whose URLs end in /category/press-releases/.
    payload, _, _ = _verify(monkeypatch, capsys, {"fda.gov/about-fda": feed},
                            "--only", "rss-feeds/press-releases")

    feed_report = payload["feeds"][0]
    assert feed_report["emitted"] == 1 and feed_report["linked"] == 0
    assert feed_report["records"][0]["outcome"] == "dropped"
    assert "nothing in the universe" in feed_report["records"][0]["reason"]


# ---------------------------------------------------------- what it prints -- #
def test_the_human_line_reads_as_the_chain_it_is(monkeypatch, capsys):
    """One operator, one terminal. The line has to answer "is this feed doing
    its job" without being re-read."""
    routes, expected = _issuer_routes()
    text, _, _ = _verify(monkeypatch, capsys, routes, "--only", "ormat",
                         as_json=False)

    assert "OK    ORA IR" in text
    assert ("2 entries · 1 body · 1 emitted · 1 linked · 1 date · 1 event "
            "(1 first-party)") in text
    assert "1 x outside the 72h news window and its headline announces" in text
    assert f"ORA   {expected}  Q2 results" in text
    assert "company-announced" in text


def test_reasons_switches_from_grouped_to_one_line_per_record(
        monkeypatch, capsys):
    """Grouping is right for a fifty-entry wire and wrong when the operator is
    looking at one name and wants the entries themselves."""
    routes, _ = _issuer_routes()
    text, _, _ = _verify(monkeypatch, capsys, routes, "--only", "ormat",
                         "--reasons", as_json=False)

    assert "Ormat Commences Commercial Operation" in text
    assert " x outside the 72h news window" not in text, "grouped form, not this"


def test_json_is_utf8_whatever_the_console_thinks(capsys):
    """`harel --json verify-feeds > out.json` wrote the Windows ANSI codepage -
    cp1255 on this box - and `json.load` then refused to read its own file. Both
    Hebrew wires are in this report's records, so the first Globes headline in a
    discard list is enough to produce a file no consumer can parse."""
    from harel.cli import _emit

    class _JsonArgs:
        json = True

    assert _emit(_JsonArgs(), {"title": "אורמת מדווחת על תוצאות"}) is True
    assert json.loads(capsys.readouterr().out) == {"title": "אורמת מדווחת על תוצאות"}


@pytest.mark.parametrize("bad", ["nosuchfeed", "zzz"])
def test_an_only_filter_that_matches_nothing_says_so(monkeypatch, capsys, bad):
    """Rather than print an empty report, which reads as "all clear"."""
    routes, _ = _issuer_routes()
    text, _, code = _verify(monkeypatch, capsys, routes, "--only", bad,
                            as_json=False)

    assert f"no feed matches --only '{bad}'" in text
    assert code == 1
