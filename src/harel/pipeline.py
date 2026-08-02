"""Collection pipeline: collect -> link -> classify -> score -> dedupe -> store."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .collect import CollectorContext, build_collectors
from .config import Config, get_config
from .db import Database
from .collect.rss import _is_placeholder_title
from .dedupe import Clusterer
from .enrich.linker import EntityLinker, direct_evidence
from .enrich.materiality import MaterialityScorer, PriceContext
from .http import HttpClient
from .models import FIELD_SEP, CalendarEntry, RawItem, ScoredItem

log = logging.getLogger("harel.pipeline")

# How far back `rescore` re-judges by default. It was 168h, and a week is not the
# corpus: a tightened rule reached only what had been collected since Tuesday, so
# the false links it was written to withdraw sat on the terminal until somebody
# remembered a flag - which is the exact opposite of "without it tuning is
# unfalsifiable". Twelve weeks covers what the database actually holds and costs
# about twice the runtime, still well under a minute.
RESCORE_DEFAULT_HOURS = 2000.0


@dataclass(slots=True)
class RunReport:
    started_at: datetime
    finished_at: datetime | None = None
    collected: int = 0
    stored: int = 0
    # Stored for the first time, as opposed to refreshed. `upsert_item` has
    # always returned this and the caller has always thrown it away, so the
    # field was serialised into every run report and every run_log row as a
    # permanent zero.
    new: int = 0
    # NOT deduplication - it counts items dropped because nothing in the
    # universe was touched. The CLI has printed the honest wording all along
    # ("dropped, no universe link") while the field, the dict key and the
    # run_log column all said something that never happened.
    dropped_unlinked: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": (self.finished_at or datetime.now(timezone.utc)).isoformat(),
            "duration_sec": round(self.duration_sec, 1),
            "collected": self.collected,
            "stored": self.stored,
            "new": self.new,
            "dropped_unlinked": self.dropped_unlinked,
            "by_source": self.by_source,
            "warnings": self.warnings,
            "errors": self.errors,
            "alerts": self.alerts,
        }


class Pipeline:
    def __init__(
        self,
        config: Config | None = None,
        db: Database | None = None,
        lookback_hours: float = 72.0,
        client: HttpClient | None = None,
    ) -> None:
        self.config = config or get_config()
        self.db = db or Database()
        self.lookback_hours = lookback_hours
        self.client = client
        self.linker = EntityLinker(self.config)
        self.scorer = MaterialityScorer(self.config)

    # ------------------------------------------------------------------ run --
    def run(self, only: list[str] | None = None) -> RunReport:
        report = RunReport(started_at=datetime.now(timezone.utc))
        client = self.client or HttpClient(
            user_agent=self.config.user_agent(),
            timeout=int(self.config.defaults.get("timeout_sec", 25)),
            max_retries=int(self.config.defaults.get("max_retries", 3)),
            backoff_base=float(self.config.defaults.get("backoff_base_sec", 2)),
        )
        ctx = CollectorContext(
            config=self.config, client=client, db=self.db,
            lookback_hours=self.lookback_hours,
        )
        collectors = build_collectors(ctx, only=only)
        if not collectors:
            report.errors.append("no collectors available - check config/sources.yaml")
            report.finished_at = datetime.now(timezone.utc)
            return report

        clusterer = Clusterer()
        # A story that arrived last pass is still the same story. Without this
        # the near-duplicate matcher starts blind every run, so a second source
        # carrying the same headline an hour later was counted as a fresh item
        # rather than as corroboration - which feeds the score.
        seeded = clusterer.seed(self.db.cluster_seed(since_hours=36.0))
        log.info("clusterer seeded with %d recent items", seeded)
        prices = self._price_context()

        for collector in collectors:
            key = collector.source.key
            # `last_error` describes the pass that is running now, so it starts
            # empty. Otherwise "the collector recorded a fault this time" cannot
            # be told from "the same string has been sitting there since
            # Tuesday", and a fault that repeats verbatim would flap on and off
            # every other run.
            prior = self.db.get_source_state(key)
            if prior.get("last_error"):
                self.db.set_source_state(key, last_error=None)

            started = time.monotonic()
            count = 0
            failure: str | None = None
            try:
                for item in collector.collect():
                    count += 1
                    report.collected += 1
                    try:
                        if self._process(item, clusterer, prices, report):
                            report.stored += 1
                    except Exception as exc:
                        report.errors.append(f"{key}: failed to store an item: {exc}")
                        log.exception("store failed for %s", key)
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                report.errors.append(f"{key}: collector aborted: {failure}")
                log.exception("collector %s aborted", key)

            report.by_source[key] = count
            report.warnings.extend(f"{key}: {w}" for w in collector.warnings)

            # A pass that finds nothing is a SUCCESSFUL pass. Recording "ok" only
            # when items came back - and writing last_ok_at=None otherwise, which
            # `set_source_state` merges in and so *erases* the previous success -
            # made a quiet source indistinguishable from a dead one. openFDA and
            # ClinicalTrials are quiet for days at a time and were reading as
            # "never worked", which is the precise confusion this bookkeeping
            # exists to prevent.
            #
            # But quiet is not the same as clean. A collector can record its own
            # failure and still return without raising - maya writes
            # save_state(last_error="TEVA: 0 parseable records") and stops - and
            # the success branch below then overwrote it with a fresh last_ok_at
            # and a null error, so a MAYA schema break read as healthy-and-quiet
            # in `harel doctor`. We do not erase an error the collector recorded
            # during the pass we just ran.
            recorded = self.db.get_source_state(key).get("last_error")
            now = datetime.now(timezone.utc).isoformat()
            state: dict[str, Any] = {"last_run_at": now, "items_last_run": count}
            if failure or recorded:
                state["last_error"] = str(failure or recorded)[:400]
                state["consecutive_failures"] = int(
                    prior.get("consecutive_failures") or 0) + 1
            else:
                state.update(last_ok_at=now, last_error=None, consecutive_failures=0)
            self.db.set_source_state(key, **state)
            log.info("%s: %d items in %.1fs", key, count, time.monotonic() - started)

        self.db.conn.commit()
        report.finished_at = datetime.now(timezone.utc)
        report.alerts = self.db.feed(min_score=self.config.scoring.tiers.get("alert", 75),
                                     since_hours=self.lookback_hours, limit=25)
        self.db.log_run(
            started_at=report.started_at.isoformat(),
            finished_at=report.finished_at.isoformat(),
            mode="collect", sources=len(collectors),
            collected=report.collected, stored=report.stored,
            dropped_unlinked=report.dropped_unlinked, errors=report.errors[:50],
        )
        self.db.checkpoint()
        return report

    # ------------------------------------------------------------- rescore --
    def rescore(self, since_hours: float = RESCORE_DEFAULT_HOURS) -> dict[str, Any]:
        """Re-run linking and scoring over what is already stored.

        Scores are computed at collection time, so a change to scoring.yaml or
        universe.yaml only reaches items collected afterwards. That makes tuning
        impossible to judge: you change a noise cap because one headline is
        ranked too high, and that exact headline keeps its old score until it
        happens to be re-collected - which for a filing is never.

        Clustering is left alone. Dedupe keys are assigned across a run, and
        recomputing them here would split clusters that were correctly merged
        when their members arrived together.
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        rows = self.db.conn.execute(
            "SELECT * FROM items WHERE published_at >= ?", (since,)
        ).fetchall()
        prices = self._price_context()
        live_feeds = self._configured_feed_urls()
        changed = dropped = purged = 0

        for row in rows:
            # Retiring a feed in config did nothing to what it had already
            # collected. Gilat's site feed was removed for publishing marketing
            # as issuer news, and its case studies stayed in the ranked feed at
            # trust 1.0 regardless - as did an entry whose headline was the word
            # "Title". A source we have decided not to believe must not keep
            # its old items standing, and a headline that says nothing was never
            # a story.
            meta = json.loads(row["meta_json"] or "{}")
            feed_url = meta.get("feed")
            known = live_feeds.get(row["source"])
            if _is_placeholder_title(row["title"]) or (
                    feed_url and known and feed_url not in known):
                self.db.conn.execute("DELETE FROM items WHERE uid = ?", (row["uid"],))
                purged += 1
                continue

            item = RawItem(
                source=row["source"], source_kind=row["source_kind"],
                external_id=row["external_id"], title=row["title"],
                url=row["url"] or "", summary=row["summary"] or "",
                body=row["body"] or "", lang=row["lang"] or "en",
                published_at=datetime.fromisoformat(row["published_at"]),
                meta=meta,
                # The collector's own knowledge - "I polled TEVA's CIK", "this
                # came off the KEN rival-product query" - is half the linking
                # input and is not a property of the text. Rebuilding without it
                # silently unlinks every item whose only tie to a ticker was the
                # query that found it.
                seed_tickers=self._trusted_seeds(row, meta),
                seed_relation=meta.get("seed_relation") or "DIRECT",
            )
            links = self.linker.link(item)
            if not links:
                # Do NOT delete the row. Re-linking is a re-derivation from less
                # information than the collector had, so "no links now" does not
                # mean "should not have been stored": treating it that way took
                # 185 items whose only tie was the collector's seed and deleted
                # them. Deletion is reserved for the two explicit purges above.
                #
                # The false CLAIM does go, though. An item with no links is
                # simply invisible, which is the right outcome for "PH, US allot
                # P42b" having been filed under Allot Communications.
                self.db.conn.execute(
                    "DELETE FROM item_tickers WHERE uid = ?", (row["uid"],))
                dropped += 1
                continue
            scored = self.scorer.score(
                item, links,
                price_by_ticker={ln.ticker: prices[ln.ticker]
                                 for ln in links if ln.ticker in prices},
                cluster_max_trust=self._cluster_trust(row["cluster_id"], row["source"]),
            )
            if abs((scored.score or 0) - (row["score"] or 0)) > 0.05:
                changed += 1
            self.db.upsert_item(scored, row["dedupe_key"], row["cluster_id"])
            # Re-harvest dates too: the calendar is built by the same pass, so a
            # new extractor (or a corrected relation on an existing entry) has
            # to be applied to the back catalogue as well, not only to whatever
            # happens to be collected next.
            self._extract_calendar(scored)

        # After the loop, and over the WHOLE table rather than the rows just
        # re-judged: a link withdrawn above leaves its calendar date standing,
        # and the dates most likely to be stale are the oldest - the ones any
        # window stops examining first.
        calendar_purged = self.db.purge_orphan_calendar()

        self.db.conn.commit()
        return {"examined": len(rows), "rescored": changed, "dropped": dropped,
                "purged": purged, "calendar_purged": calendar_purged}

    def _trusted_seeds(self, row, meta: dict[str, Any]) -> list[str]:
        """The collector's seeds, minus any a search engine merely guessed at.

        A per-ticker Google query seeds DIRECT on the strength of the query
        alone. That is fine at collection time - the collector now checks the
        text before emitting - but items collected before that check still hold
        seeds the text never supported.
        """
        seeds = list(meta.get("seed_tickers") or [])
        source = self.config.sources.get(row["source"])
        if not seeds or not source:
            return seeds
        # A regulator document's seed said only "the API returned this for one
        # of the sector's terms", which the linker then honoured at 0.92. The
        # collector no longer makes that claim - but stored items still carry
        # the old seed list, so without this the back catalogue re-links to it
        # for ever and a fixed false positive is only fixed for the future.
        # Rescore exists precisely so that tuning reaches what is already here.
        if source.kind in _REGULATOR_KINDS:
            return []
        if "{q}" not in (source.base_url or ""):
            return seeds
        if (meta.get("seed_relation") or "DIRECT") != "DIRECT":
            return seeds
        # Joined as RawItem.text joins it. A plain space here would let the last
        # word of the title and the first of the summary read as one phrase, and
        # this call decides whether to KEEP a stored seed - so the separator is
        # the difference between withdrawing an unsupported claim and confirming
        # it against a sentence that was never written.
        text = FIELD_SEP.join((row["title"], row["summary"] or ""))
        return [t for t in seeds
                if (tc := self.config.ticker(t)) and direct_evidence(tc, text)]

    def _configured_feed_urls(self) -> dict[str, set[str]]:
        """Per source, the feed URLs we still poll - but ONLY for sources whose
        feed list is finite and knowable.

        Query-driven sources (Google News: one request per ticker per theme,
        `base_url` with `{q}`) are deliberately absent. Their `meta.feed` is the
        search URL, which never appears in any static list, so treating absence
        as "retired" reads every single aggregator item as orphaned. It did
        exactly that here and deleted 978 rows before the count gave it away.
        """
        out: dict[str, set[str]] = {}
        for source in self.config.sources.values():
            if not source.enabled or "{q}" in (source.base_url or ""):
                continue
            urls = set(source.feeds)
            if source.raw.get("feeds_from") == "universe.ir_feeds":
                for ticker in self.config.active_tickers:
                    tc = self.config.ticker(ticker)
                    if tc:
                        urls |= set(tc.ir_feeds)
            if urls:
                out[source.key] = urls
        return out

    # -------------------------------------------------------------- process --
    def _process(self, item, clusterer: Clusterer, prices: dict[str, PriceContext],
                 report: RunReport) -> bool:
        # Persist what the collector knew, so a later rescore can rebuild the
        # same links instead of re-deriving them from the text alone.
        if item.seed_tickers:
            item.meta.setdefault("seed_tickers", list(item.seed_tickers))
            item.meta.setdefault("seed_relation", item.seed_relation)

        links = self.linker.link(item)
        if not links:
            # Nothing in our universe is touched - correct behaviour is to drop
            # it, not to store noise. Counted so `harel doctor` can show the
            # collect/keep ratio per source.
            report.dropped_unlinked += 1
            return False

        # The linked tickers are what stop two similar headlines that share no
        # company from being merged into one story.
        dedupe_key, cluster_id = clusterer.assign(item, {ln.ticker for ln in links})
        cluster_trust = self._cluster_trust(cluster_id, item.source)

        scored: ScoredItem = self.scorer.score(
            item, links,
            price_by_ticker={ln.ticker: prices[ln.ticker]
                             for ln in links if ln.ticker in prices},
            cluster_max_trust=cluster_trust,
        )
        if self.db.upsert_item(scored, dedupe_key, cluster_id):
            report.new += 1
        self._extract_calendar(scored)
        return True

    def _extract_calendar(self, scored: ScoredItem) -> None:
        """Harvest dated future events out of what we just collected.

        Free sources do not publish an earnings calendar, but they do leak dates:
        a trial's primary-completion date, a rule's effective date, a comment
        deadline. Those are exactly the "do not be short into this" dates.
        """
        meta = scored.raw.meta
        today = datetime.now(timezone.utc).date().isoformat()
        entries: list[CalendarEntry] = []

        def add(kind: str, date: Any, label: str, confidence: float) -> None:
            date_str = str(date or "")[:10]
            if len(date_str) != 10 or date_str <= today:
                return
            for link in scored.links:
                if link.relation in ("DIRECT", "SUBSIDIARY", "PRODUCT_RIVAL", "SECTOR_REG"):
                    entries.append(CalendarEntry(
                        ticker=link.ticker, kind=kind, date=date_str, label=label,
                        source=scored.raw.source, confidence=confidence,
                        url=scored.raw.url,
                        # How this date reaches this ticker. A rule taking effect
                        # for Textron Aviation is a real date and a real sector
                        # link, but it is not TAT Technologies' next catalyst,
                        # and it was being offered as exactly that.
                        relation=link.relation,
                    ))

        # An issuer announcing when it will report is the single most valuable
        # forward date a short-term trader has, and it is published weeks ahead
        # in plain English: "will issue its second quarter 2026 earnings release
        # on Tuesday, August 4, 2026". docs/LIMITATIONS.md lists the missing
        # earnings calendar as gap #4 and prices it at a paid feed; the date was
        # sitting in the press release the whole time.
        reported = _earnings_date(scored.raw)
        if reported:
            date_str, label = reported
            # An issuer stating its own date and an aggregator restating it are
            # not equally good. Both are worth having - the aggregator is the
            # only channel for a name with no IR feed, which is how GILT and
            # NICE went missing - but the calendar has to say which it was.
            first_party = scored.raw.source in _FIRST_PARTY_SOURCES
            label = (f"{label} (company-announced date)" if first_party
                     else f"{label} (reported by {scored.raw.source})")
            for link in scored.links:
                if link.relation in ("DIRECT", "SUBSIDIARY"):
                    entries.append(CalendarEntry(
                        ticker=link.ticker, kind="earnings", date=date_str,
                        label=label, source=scored.raw.source,
                        confidence=0.95 if first_party else 0.8,
                        url=scored.raw.url, relation=link.relation,
                    ))

        if meta.get("primary_completion"):
            add("trial_completion", meta["primary_completion"],
                f"{meta.get('sponsor', '')}: {meta.get('nct_id', '')} primary completion "
                f"({'/'.join(meta.get('phases') or [])})", 0.7)
        if meta.get("effective_on"):
            add("rule_effective", meta["effective_on"],
                f"Rule effective: {scored.raw.title[:90]}", 0.9)
        if meta.get("comments_close_on"):
            add("comment_deadline", meta["comments_close_on"],
                f"Comment deadline: {scored.raw.title[:90]}", 0.9)
        if meta.get("scheduled_report_on"):
            # TASE-published expected results date. These are the dates a
            # short-term trader must not be caught short into; they are official
            # but can still move, hence 0.9 rather than 1.0.
            clock = meta.get("scheduled_time")
            add("earnings", meta["scheduled_report_on"],
                "Expected results: "
                + (meta.get("schedule_label") or scored.raw.title[:90])
                + (f" at {clock}" if clock else ""),
                0.9)

        if entries:
            self.db.save_calendar(entries)

    def _cluster_trust(self, cluster_id: str, current_source: str) -> float:
        """Highest source trust already present in this story's cluster."""
        rows = self.db.conn.execute(
            "SELECT DISTINCT source FROM items WHERE cluster_id = ?", (cluster_id,)
        ).fetchall()
        trusts = [
            self.config.sources[r["source"]].trust
            for r in rows
            if r["source"] in self.config.sources
        ]
        current = self.config.sources.get(current_source)
        if current:
            trusts.append(current.trust)
        return max(trusts) if trusts else 0.6

    def _price_context(self) -> dict[str, PriceContext]:
        out: dict[str, PriceContext] = {}
        for ticker in self.config.active_tickers:
            row = self.db.latest_price(ticker)
            if row:
                out[ticker] = PriceContext(
                    change_pct=row.get("change_pct"),
                    volume_multiple=row.get("vol_mult"),
                    session=row.get("session") or "unknown",
                )
        return out


# --------------------------------------------------------------------------- #
# Earnings dates, read out of the announcement that carries them.
#
# docs/LIMITATIONS.md gap #4 is "no official earnings calendar" and prices it at
# a paid feed. But every issuer publishes the date weeks ahead, in a press
# release, in plain English:
#
#   "Tower Semiconductor ... will issue its second quarter 2026 earnings release
#    on Tuesday, August 4, 2026."
#
# We were already collecting that release and scoring it as a low-value
# "conference call" item, then throwing the date away.
# --------------------------------------------------------------------------- #
_EARNINGS_ANNOUNCEMENT = re.compile(
    r"\b(will (issue|report|release|host|announce)|to (report|announce|release|host)|"
    r"schedules?|has scheduled|announces? (the )?(date|timing)|"
    # Aggregators rewrite the issuer's headline and drop the announcing verb.
    # Gilat's date reached us only as "Gilat CEO and CFO to Take Questions on Q2
    # Results Aug. 5", which named the quarter and the day and still failed all
    # three legs. The date is not less true for having been reworded.
    r"to (take questions|discuss|present)|(earnings|conference) call)\b",
    re.IGNORECASE)
_EARNINGS_SUBJECT = re.compile(
    r"\b((first|second|third|fourth)[- ]quarter|q[1-4]|full[- ]year|annual|"
    r"half[- ]year)\b.{0,40}?\b(results|earnings)\b|"
    r"\b(results|earnings)\b.{0,40}?\b((first|second|third|fourth)[- ]quarter|q[1-4])\b|"
    # An announcement that never names the quarter. AudioCodes' 4 August date
    # reached us only as "Earnings Preview: AudioCodes to Report Financial
    # Results Pre-market on August 04" - announcing verb, day of the month, and
    # not one word saying WHICH quarter, so the subject leg failed and the date
    # was thrown away. "Financial results" is specific enough on its own; the
    # announcement leg and the 120-day horizon still have to agree.
    r"\b(financial|quarterly)\s+results\b",
    re.IGNORECASE | re.DOTALL)
_MONTH = ("january february march april may june july august september october "
          "november december").split()
# Abbreviated forms, and a year that is allowed to be absent. "Aug. 5" is how a
# headline writes a date; requiring "August 5, 2026" meant the only place the
# date was ever written in full was the issuer's own release - exactly the feed
# GILT and NICE do not have. A missing year is resolved to the next occurrence
# inside the 120-day horizon below, so it cannot silently mean last year.
_MONTH_FORMS: dict[str, tuple[str, ...]] = {
    m: (m,) if len(m) <= 3 else (m, m[:3]) for m in _MONTH
}
# September is the one month AP style cuts to four letters, and "Sept." is how a
# US press release writes it. Listed longest first because an alternation takes
# the FIRST branch that matches, not the longest: built as `sep(?:tember)?\.?`,
# September matched the "Sep" of "Sept. 5", left "t. 5" for the `\s+` that
# follows, and threw the whole date away. Every AP-dated Q3 announcement went
# unextracted - and September is a quarter end. Same hole as "Aug. 5", one month
# later.
_MONTH_FORMS["september"] = ("september", "sept", "sep")
_MONTH_ALT = "|".join(form + r"\.?" for m in _MONTH for form in _MONTH_FORMS[m])
_DATE_MDY = re.compile(
    r"\b(" + _MONTH_ALT + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE)
_DATE_DMY = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_ALT + r"),?(?:\s+(\d{4}))?\b",
    re.IGNORECASE)
_QUARTER_LABEL = re.compile(
    r"\b(first|second|third|fourth)[- ]quarter\b|\bq([1-4])\b", re.IGNORECASE)
# Sources where the issuer is speaking for itself.
_FIRST_PARTY_SOURCES = frozenset({
    # `company_ir_pages` is the issuer speaking as much as its feed is - it is
    # only read off HTML because AudioCodes and NICE publish no feed at all. It
    # needs its own key rather than borrowing `company_ir_rss`: rescore purges
    # any item whose `meta.feed` is absent from that key's finite feed list,
    # which is the rule that once deleted 978 rows, and an IR page URL would
    # never be in it.
    "company_ir_rss", "company_ir_pages",
    "maya_tase", "maya_schedule", "sec_edgar_submissions",
})

# Collectors that fetch regulator documents by *sector query* and so cannot know
# who a document is about - only its text can say. See `_trusted_seeds`.
#
# openFDA and html_table are deliberately absent: they seed from an entity
# matcher that found a company, product or named peer in the record, which is
# the evidence standard being enforced here rather than a violation of it.
_REGULATOR_KINDS = frozenset({"federal_register", "federal_register_pi"})


def _earnings_date(item) -> tuple[str, str] | None:
    """(ISO date, label) if this item announces when results will be published.

    Deliberately strict on all three legs - it must look like an announcement,
    be about results, and carry a parseable date - because a wrong earnings date
    is worse than no earnings date: it invites a trader to hold through what
    they think is a quiet session.
    """
    # The body is read too. Four of the eleven issuer feeds (TEVA, ORA, ICL,
    # CGEN - all Q4's `pressrelease.aspx`) publish a headline and nothing else,
    # so the reporting date is only ever in the release itself: Ormat's feed
    # said "to Host Conference Call Announcing Second Quarter 2026 Financial
    # Results" and the "Wednesday, August 5, 2026" was in the first paragraph
    # of the page. The collector fetches that body; this is where it is spent.
    # Still capped, and a press release states its reporting date in the lede -
    # reading further only invites some other future date to be picked up.
    text = f"{item.title}\n{item.summary}\n{item.body}"[:2000]
    if not (_EARNINGS_ANNOUNCEMENT.search(text) and _EARNINGS_SUBJECT.search(text)):
        return None

    today = datetime.now(timezone.utc).date()
    best: date | None = None
    for pattern, month_first in ((_DATE_MDY, True), (_DATE_DMY, False)):
        for match in pattern.finditer(text):
            raw_month = match.group(1) if month_first else match.group(2)
            raw_day = match.group(2) if month_first else match.group(1)
            month = _month_index(raw_month)
            if month is None:
                continue
            found = _resolve_date(month, int(raw_day), match.group(3), today)
            if found and today <= found <= today + timedelta(days=120):
                best = found if best is None else min(best, found)
    if best is None:
        return None

    quarter = _QUARTER_LABEL.search(text)
    if quarter:
        word = quarter.group(1) or ""
        label = (f"Q{['first', 'second', 'third', 'fourth'].index(word.lower()) + 1}"
                 if word else f"Q{quarter.group(2)}")
    else:
        label = "Results"
    return best.isoformat(), f"{label} results"


def _month_index(token: str) -> int | None:
    """`Aug.` / `august` / `Sept` -> 8 / 8 / 9."""
    token = token.lower().rstrip(".")
    for index, name in enumerate(_MONTH):
        if name.startswith(token):
            return index + 1
    return None


def _resolve_date(month: int, day: int, year: str | None, today: date) -> date | None:
    """A date with no year means the next one, not an ambiguous one.

    "Aug. 5" in a July headline is this August. Resolving to the current year
    unconditionally would put a January date eleven months in the past, where
    the horizon check below silently drops it - a date lost rather than a date
    wrong, but lost all the same.
    """
    if year:
        return _safe_date(int(year), month, day)
    for candidate in (today.year, today.year + 1):
        found = _safe_date(candidate, month, day)
        if found and found >= today:
            return found
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
