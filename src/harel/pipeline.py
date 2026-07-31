"""Collection pipeline: collect -> link -> classify -> score -> dedupe -> store."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .collect import CollectorContext, build_collectors
from .config import Config, get_config
from .db import Database
from .dedupe import Clusterer
from .enrich.linker import EntityLinker
from .enrich.materiality import MaterialityScorer, PriceContext
from .http import HttpClient
from .models import CalendarEntry, RawItem, ScoredItem

log = logging.getLogger("harel.pipeline")


@dataclass(slots=True)
class RunReport:
    started_at: datetime
    finished_at: datetime | None = None
    collected: int = 0
    stored: int = 0
    new: int = 0
    deduped: int = 0
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
            "deduped": self.deduped,
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
        prices = self._price_context()

        for collector in collectors:
            key = collector.source.key
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
            now = datetime.now(timezone.utc).isoformat()
            state: dict[str, Any] = {"last_run_at": now, "items_last_run": count}
            if failure:
                prior = self.db.get_source_state(key)
                state["last_error"] = failure[:400]
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
            deduped=report.deduped, errors=report.errors[:50],
        )
        return report

    # ------------------------------------------------------------- rescore --
    def rescore(self, since_hours: float = 168.0) -> dict[str, Any]:
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
        changed = dropped = 0

        for row in rows:
            item = RawItem(
                source=row["source"], source_kind=row["source_kind"],
                external_id=row["external_id"], title=row["title"],
                url=row["url"] or "", summary=row["summary"] or "",
                body=row["body"] or "", lang=row["lang"] or "en",
                published_at=datetime.fromisoformat(row["published_at"]),
                meta=json.loads(row["meta_json"] or "{}"),
            )
            links = self.linker.link(item)
            if not links:
                self.db.conn.execute("DELETE FROM items WHERE uid = ?", (row["uid"],))
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

        self.db.conn.commit()
        return {"examined": len(rows), "rescored": changed, "dropped": dropped}

    # -------------------------------------------------------------- process --
    def _process(self, item, clusterer: Clusterer, prices: dict[str, PriceContext],
                 report: RunReport) -> bool:
        links = self.linker.link(item)
        if not links:
            # Nothing in our universe is touched - correct behaviour is to drop
            # it, not to store noise. Counted so `harel doctor` can show the
            # collect/keep ratio per source.
            report.deduped += 1
            return False

        dedupe_key, cluster_id = clusterer.assign(item)
        cluster_trust = self._cluster_trust(cluster_id, item.source)

        scored: ScoredItem = self.scorer.score(
            item, links,
            price_by_ticker={ln.ticker: prices[ln.ticker]
                             for ln in links if ln.ticker in prices},
            cluster_max_trust=cluster_trust,
        )
        self.db.upsert_item(scored, dedupe_key, cluster_id)
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
