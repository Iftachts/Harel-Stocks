"""SQLite storage.

Single-user system, so SQLite is the right call: zero ops, one file to back up,
and FTS5 gives the LLM agent a real search index for free.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .models import CalendarEntry, Link, PriceSnapshot, RawItem, ScoredItem

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS items (
    uid            TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    source_kind    TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT,
    summary        TEXT,
    body           TEXT,
    lang           TEXT,
    published_at   TEXT NOT NULL,
    collected_at   TEXT NOT NULL,
    meta_json      TEXT,
    events_json    TEXT,
    score          REAL DEFAULT 0,
    tier           TEXT,
    reasons_json   TEXT,
    dedupe_key     TEXT,
    cluster_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_score     ON items(score DESC);
CREATE INDEX IF NOT EXISTS idx_items_cluster   ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_items_dedupe    ON items(dedupe_key);

CREATE TABLE IF NOT EXISTS item_tickers (
    uid        TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    relation   TEXT NOT NULL,
    confidence REAL NOT NULL,
    why        TEXT,
    score      REAL DEFAULT 0,
    PRIMARY KEY (uid, ticker, relation),
    FOREIGN KEY (uid) REFERENCES items(uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_it_ticker ON item_tickers(ticker, score DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, summary, body,
    content='items', content_rowid='rowid', tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT NOT NULL,
    asof        TEXT NOT NULL,   -- when we fetched
    market_time TEXT,            -- when the exchange printed
    extended_last       REAL,    -- last pre/post-market print
    extended_change_pct REAL,    -- that print against the regular close
    extended_time       TEXT,
    last        REAL,
    prev_close  REAL,
    change_pct  REAL,
    volume      REAL,
    adv20       REAL,
    vol_mult    REAL,
    day_high    REAL,
    day_low     REAL,
    session     TEXT,
    provider    TEXT,
    PRIMARY KEY (ticker, asof)
);

CREATE TABLE IF NOT EXISTS bars (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS calendar (
    ticker     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    date       TEXT NOT NULL,
    label      TEXT NOT NULL,
    source     TEXT,
    confidence REAL,
    url        TEXT,
    relation   TEXT,
    -- One source asserting one date for one name is ONE fact. `label` used to
    -- be part of this key, and the label carries the provenance - "(company-
    -- announced date)" against "(reported by google_news)" - so when that
    -- wording was corrected the fixed row inserted BESIDE the stale one instead
    -- of replacing it. PERI then held the same 10 August date twice, and the
    -- reader keeps the higher confidence, so the stale row claiming the issuer
    -- had announced it beat the correct one saying an aggregator had.
    PRIMARY KEY (ticker, kind, date, source)
);

-- Per-source bookkeeping: conditional GETs, cursors, and honest health state.
CREATE TABLE IF NOT EXISTS source_state (
    source        TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    cursor        TEXT,
    last_run_at   TEXT,
    last_ok_at    TEXT,
    last_error    TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    items_last_run INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    mode       TEXT, sources INTEGER,
    collected  INTEGER, stored  INTEGER, deduped INTEGER,
    errors_json TEXT
);
"""

TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, summary, body)
  VALUES (new.rowid, new.title, new.summary, new.body);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary, body)
  VALUES('delete', old.rowid, old.title, old.summary, old.body);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary, body)
  VALUES('delete', old.rowid, old.title, old.summary, old.body);
  INSERT INTO items_fts(rowid, title, summary, body)
  VALUES (new.rowid, new.title, new.summary, new.body);
END;
"""


def default_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "harel.db"


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # `serve` keeps a connection open for as long as the terminal runs, so a
        # collection pass is always writing against a live reader. WAL lets them
        # coexist, but the default 5s busy timeout does not survive a checkpoint
        # stall - the collector died mid-pass with "database is locked" and left
        # the feed a day stale. Thirty seconds is longer than any write here.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                    timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executescript(TRIGGERS)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns that `CREATE TABLE IF NOT EXISTS` cannot add to a database
        that already exists. Every entry here must be nullable: an older row
        simply reads back as None, which the surfaces render as "unknown"."""
        added: list[tuple[str, str, str]] = [
            ("prices", "provider", "TEXT"),
            ("calendar", "relation", "TEXT"),
            # The exchange's own timestamp. Rows written before this column
            # existed read back None, which the surfaces render as "the print
            # time was not recorded" rather than inventing one.
            ("prices", "market_time", "TEXT"),
            # The extended-hours print, kept apart from the session return.
            # Rows written before these existed read back None, which the
            # surfaces render as "no post-market print" rather than as zero.
            ("prices", "extended_last", "REAL"),
            ("prices", "extended_change_pct", "REAL"),
            ("prices", "extended_time", "TEXT"),
        ]
        for table, column, decl in added:
            cols = {r["name"] for r in
                    self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        self._migrate_calendar_key()

    def _migrate_calendar_key(self) -> None:
        """Re-key `calendar` on the source rather than on the label.

        SQLite cannot alter a primary key, and `CREATE TABLE IF NOT EXISTS`
        leaves an existing table alone, so a database created before this keeps
        the old key for ever unless it is rebuilt. Duplicates are collapsed to
        the most recently written row per key; whichever survives is corrected
        by the next harvest, which now REPLACES rather than accumulating.
        """
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='calendar'"
        ).fetchone()
        if not sql or "PRIMARY KEY (ticker, kind, date, label)" not in (sql[0] or ""):
            return
        self.conn.executescript("""
            CREATE TABLE calendar_rekeyed (
                ticker TEXT NOT NULL, kind TEXT NOT NULL, date TEXT NOT NULL,
                label TEXT NOT NULL, source TEXT, confidence REAL, url TEXT,
                relation TEXT,
                PRIMARY KEY (ticker, kind, date, source)
            );
            INSERT OR REPLACE INTO calendar_rekeyed
                SELECT ticker, kind, date, label, source, confidence, url, relation
                FROM calendar WHERE rowid IN (
                    SELECT MAX(rowid) FROM calendar
                    GROUP BY ticker, kind, date, COALESCE(source, '')
                );
            DROP TABLE calendar;
            ALTER TABLE calendar_rekeyed RENAME TO calendar;
        """)

    # -- plumbing ---------------------------------------------------------- #
    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def checkpoint(self) -> int:
        """Fold the WAL back into the database file and truncate it.

        SQLite only checkpoints automatically when no reader is active. The
        terminal is a permanent reader, so on this machine the WAL grew to 89MB
        against a 5MB database - every write walking a log seventeen times the
        size of the data. Call this at the end of a pass, when the collector is
        the only writer; it is a no-op if a reader is mid-transaction.

        Returns the number of pages left in the WAL (0 means fully truncated).
        """
        try:
            _busy, _log, remaining = self.conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return int(remaining)
        except sqlite3.OperationalError:
            return -1  # a reader held it; the next pass will try again

    def close(self) -> None:
        self.conn.close()

    # -- writes ------------------------------------------------------------ #
    def upsert_item(self, item: ScoredItem, dedupe_key: str, cluster_id: str) -> bool:
        """Insert or refresh one scored item. Returns True if it was new."""
        raw = item.raw
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute("SELECT uid FROM items WHERE uid = ?", (raw.uid,))
        is_new = cur.fetchone() is None

        self.conn.execute(
            """
            INSERT INTO items (uid, source, source_kind, external_id, title, url,
                               summary, body, lang, published_at, collected_at,
                               meta_json, events_json, score, tier, reasons_json,
                               dedupe_key, cluster_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                title=excluded.title, summary=excluded.summary, body=excluded.body,
                meta_json=excluded.meta_json, events_json=excluded.events_json,
                score=excluded.score, tier=excluded.tier,
                reasons_json=excluded.reasons_json, cluster_id=excluded.cluster_id,
                -- published_at was absent here, so a date this system got wrong
                -- was permanent: re-collection could correct the title, the
                -- score and the links, but never the timestamp. Twenty-two
                -- Federal Register items stayed dated in the future through a
                -- fix that stopped producing future dates at all.
                --
                -- collected_at stays out on purpose. It means "when we first
                -- saw this", it is what the latency panel measures against, and
                -- refreshing it on every pass would reset that to zero.
                published_at=excluded.published_at,
                dedupe_key=excluded.dedupe_key
            """,
            (
                raw.uid, raw.source, raw.source_kind, raw.external_id, raw.title,
                raw.url, raw.summary, raw.body[:200_000], raw.lang,
                raw.published_at.isoformat(), now,
                json.dumps(raw.meta, ensure_ascii=False, default=str),
                json.dumps(item.events), item.score, item.tier,
                json.dumps(item.reasons, ensure_ascii=False),
                dedupe_key, cluster_id,
            ),
        )
        self.conn.execute("DELETE FROM item_tickers WHERE uid = ?", (raw.uid,))
        self.conn.executemany(
            """INSERT OR REPLACE INTO item_tickers (uid, ticker, relation, confidence, why, score)
               VALUES (?,?,?,?,?,?)""",
            [
                (raw.uid, ln.ticker, ln.relation, ln.confidence, ln.why,
                 item.per_ticker_score.get(ln.ticker, item.score))
                for ln in item.links
            ],
        )
        return is_new

    def save_price(self, snap: PriceSnapshot) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO prices
               (ticker, asof, market_time, last, prev_close, change_pct, volume,
                adv20, vol_mult, day_high, day_low, session, provider,
                extended_last, extended_change_pct, extended_time)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snap.ticker, snap.asof.isoformat(),
             snap.market_time.isoformat() if snap.market_time else None,
             snap.last, snap.prev_close,
             snap.change_pct, snap.volume, snap.adv20, snap.volume_multiple,
             snap.day_high, snap.day_low, snap.session, snap.provider,
             snap.extended_last, snap.extended_change_pct,
             snap.extended_time.isoformat() if snap.extended_time else None),
        )

    def save_bars(self, ticker: str, bars: Iterable[dict[str, Any]]) -> int:
        rows = [
            (ticker, b["date"], b.get("open"), b.get("high"), b.get("low"),
             b.get("close"), b.get("volume"))
            for b in bars
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO bars (ticker,date,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def save_calendar(self, entries: Iterable[CalendarEntry]) -> int:
        rows = [(e.ticker, e.kind, e.date, e.label, e.source, e.confidence, e.url,
                 e.relation) for e in entries]
        self.conn.executemany(
            "INSERT OR REPLACE INTO calendar "
            "(ticker,kind,date,label,source,confidence,url,relation) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    # -- source state ------------------------------------------------------ #
    def get_source_state(self, source: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return dict(row) if row else {}

    def set_source_state(self, source: str, **fields: Any) -> None:
        current = self.get_source_state(source)
        merged = {**current, **fields, "source": source}
        cols = ["source", "etag", "last_modified", "cursor", "last_run_at",
                "last_ok_at", "last_error", "consecutive_failures", "items_last_run"]
        values = [merged.get(c) for c in cols]
        self.conn.execute(
            f"INSERT OR REPLACE INTO source_state ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            values,
        )
        self.conn.commit()

    def source_health(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM source_state ORDER BY consecutive_failures DESC, source"
        ).fetchall()
        return [dict(r) for r in rows]

    def detection_lag(self, hours: float = 6.0) -> dict[str, dict[str, Any]]:
        """Per source: how long we typically take to see something after it was
        published.

        This is the number that decides whether a source is tradeable. A feed
        that reaches us a median of three minutes late is an edge; the same feed
        at ninety minutes is a history lesson, and until now nothing on any
        screen distinguished the two.

        The window is on **publication**, not collection. Windowing on collection
        put every item of the first two-week backfill in the sample - each of
        them "late" by up to fourteen days purely because it predated the
        install - and reported medians of four days for sources that are in fact
        minutes behind. A measurement that misleading is worse than none.

        Median and p90 rather than a mean, so one slow straggler cannot make a
        fast source look broken.
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT source, (julianday(collected_at) - julianday(published_at)) * 1440 "
            "AS lag FROM items WHERE published_at >= ?",
            (since,),
        ).fetchall()

        buckets: dict[str, list[float]] = {}
        for row in rows:
            lag = row["lag"]
            if lag is None or lag < 0:
                # Negative means the source stamped it in the future - a clock or
                # timezone problem at the far end, not a measurement of our speed.
                continue
            buckets.setdefault(row["source"], []).append(float(lag))

        out: dict[str, dict[str, Any]] = {}
        for source, lags in buckets.items():
            lags.sort()
            mid = len(lags) // 2
            median = (lags[mid] if len(lags) % 2
                      else (lags[mid - 1] + lags[mid]) / 2)
            out[source] = {
                "items": len(lags),
                "median_minutes": round(median, 1),
                "p90_minutes": round(lags[min(len(lags) - 1, int(len(lags) * 0.9))], 1),
            }
        return out

    def log_run(self, **fields: Any) -> None:
        self.conn.execute(
            """INSERT INTO run_log (started_at, finished_at, mode, sources,
                                    collected, stored, deduped, errors_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (fields.get("started_at"), fields.get("finished_at"), fields.get("mode"),
             fields.get("sources", 0), fields.get("collected", 0),
             fields.get("stored", 0), fields.get("deduped", 0),
             json.dumps(fields.get("errors", []), ensure_ascii=False, default=str)),
        )
        self.conn.commit()

    def last_run(self, mode: str = "collect") -> dict[str, Any] | None:
        """The most recent finished pass. Written since the beginning and read
        by nothing until now, which is why the terminal could show a clock in
        its header and still not tell you how old the data under it was."""
        row = self.conn.execute(
            "SELECT * FROM run_log WHERE mode = ? AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1",
            (mode,),
        ).fetchone()
        return dict(row) if row else None

    # -- reads ------------------------------------------------------------- #
    def find_cluster(self, dedupe_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT cluster_id FROM items WHERE dedupe_key = ? LIMIT 1", (dedupe_key,)
        ).fetchone()
        return row["cluster_id"] if row else None

    def feed(
        self,
        tickers: Sequence[str] | None = None,
        min_score: float = 35.0,
        since_hours: float = 48.0,
        limit: int = 60,
        relations: Sequence[str] | None = None,
        events: Sequence[str] | None = None,
        collapse_clusters: bool = True,
        include_tape: bool = True,
        max_per_ticker: int | None = None,
    ) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        where = ["i.published_at >= ?", "it.score >= ?"]
        params: list[Any] = [since, min_score]

        if not include_tape:
            # Excluded in SQL rather than after the fetch: these carry a forced
            # score of 70, so on a busy tape they occupy every top slot and a
            # post-filter would return an empty page for any small limit.
            where.append("i.external_id NOT LIKE 'unexplained:%'")

        if tickers:
            where.append(f"it.ticker IN ({','.join('?' * len(tickers))})")
            params.extend([t.upper() for t in tickers])
        if relations:
            where.append(f"it.relation IN ({','.join('?' * len(relations))})")
            params.extend(relations)

        sql = f"""
            SELECT i.*, it.ticker, it.relation, it.confidence, it.why,
                   it.score AS ticker_score
            FROM item_tickers it JOIN items i ON i.uid = it.uid
            WHERE {' AND '.join(where)}
            ORDER BY it.score DESC, i.published_at DESC
            LIMIT ?
        """
        params.append(limit * 4 if collapse_clusters else limit)
        rows = [self._row_to_dict(r) for r in self.conn.execute(sql, params).fetchall()]

        if events:
            wanted = set(events)
            rows = [r for r in rows if wanted & set(r["events"])]

        if collapse_clusters:
            rows = _collapse(rows)
            self._attach_event_start(rows)
        if max_per_ticker:
            rows = _cap_per_ticker(rows, max_per_ticker)
        return rows[:limit]

    def _attach_event_start(self, rows: list[dict[str, Any]]) -> None:
        """When each row's event first became knowable, cluster-wide.

        Asked of the whole cluster and not of the rows in hand, because the two
        copies of one Federal Register document sit days apart: a 30-hour feed
        window returns the publication copy and leaves the public-inspection
        copy - the earlier one, the one that answers "could this have moved
        Friday's close" - outside the window entirely. Collapsing what happens
        to be in the window is not the same question.
        """
        cluster_ids = {r["cluster_id"] for r in rows if r.get("cluster_id")}
        if not cluster_ids:
            return
        ids = list(cluster_ids)
        starts = {
            r["cluster_id"]: r["first"]
            for r in self.conn.execute(
                f"SELECT cluster_id, MIN(published_at) AS first FROM items "
                f"WHERE cluster_id IN ({','.join('?' * len(ids))}) GROUP BY cluster_id",
                ids,
            ).fetchall()
        }
        for row in rows:
            first = starts.get(row.get("cluster_id"))
            if first and first < str(row.get("published_at") or ""):
                row["first_published_at"] = first

    def search(self, query: str, limit: int = 40, since_hours: float | None = None,
               tickers: Sequence[str] | None = None) -> list[dict[str, Any]]:
        where = ["items_fts MATCH ?"]
        params: list[Any] = [query]
        if since_hours:
            where.append("i.published_at >= ?")
            params.append(
                (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
            )
        join = ""
        if tickers:
            join = "JOIN item_tickers it ON it.uid = i.uid"
            where.append(f"it.ticker IN ({','.join('?' * len(tickers))})")
            params.extend([t.upper() for t in tickers])

        sql = f"""
            SELECT DISTINCT i.*, bm25(items_fts) AS rank
            FROM items_fts JOIN items i ON i.rowid = items_fts.rowid {join}
            WHERE {' AND '.join(where)}
            ORDER BY rank LIMIT ?
        """
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = self._row_to_dict(r)
            d["tickers"] = self.tickers_for(d["uid"])
            out.append(d)
        return out

    def stored_meta(self, source: str, external_id: str) -> dict[str, Any] | None:
        """Meta we already hold for this exact item, if any.

        Lets a collector skip an expensive detail fetch on a later pass *and*
        carry the earlier result forward. Skipping alone is not enough: upsert
        replaces the row, so an item rebuilt without its enrichment overwrites
        the enriched one and the detail decays away pass by pass.
        """
        row = self.conn.execute(
            "SELECT meta_json FROM items WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ).fetchone()
        if not row or not row["meta_json"]:
            return None
        try:
            return json.loads(row["meta_json"])
        except (TypeError, ValueError):
            return None

    def item(self, uid: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM items WHERE uid = ?", (uid,)).fetchone()
        if not row:
            return None
        d = self._row_to_dict(row)
        d["tickers"] = self.tickers_for(uid)
        d["cluster"] = self.cluster_members(d.get("cluster_id"), exclude=uid)
        return d

    def resolve_uid(self, uid: str) -> str | None:
        """Full uid for an exact id or an unambiguous prefix.

        uids are 40-char sha1; nobody retypes one. A prefix is what you actually
        copy off a screen, and an ambiguous prefix returns nothing rather than
        guessing at which item you meant.
        """
        uid = (uid or "").strip().lower()
        if not uid:
            return None
        row = self.conn.execute(
            "SELECT uid FROM items WHERE uid = ?", (uid,)).fetchone()
        if row:
            return row["uid"]
        rows = self.conn.execute(
            "SELECT uid FROM items WHERE uid LIKE ? LIMIT 2", (uid + "%",)).fetchall()
        return rows[0]["uid"] if len(rows) == 1 else None

    def tickers_for(self, uid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ticker, relation, confidence, why, score FROM item_tickers "
            "WHERE uid = ? ORDER BY score DESC", (uid,)
        ).fetchall()
        return [dict(r) for r in rows]

    def cluster_members(self, cluster_id: str | None, exclude: str = "") -> list[dict[str, Any]]:
        if not cluster_id:
            return []
        rows = self.conn.execute(
            "SELECT uid, source, title, url, published_at FROM items "
            "WHERE cluster_id = ? AND uid != ? ORDER BY published_at",
            (cluster_id, exclude),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_price(self, ticker: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM prices WHERE ticker = ? ORDER BY asof DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        return dict(row) if row else None

    def recent_bars(self, ticker: str, n: int = 25) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM bars WHERE ticker = ? ORDER BY date DESC LIMIT ?",
            (ticker.upper(), n),
        ).fetchall()
        return [dict(r) for r in rows][::-1]

    def calendar(self, tickers: Sequence[str] | None = None,
                 days_ahead: int = 45) -> list[dict[str, Any]]:
        today = datetime.now(timezone.utc).date().isoformat()
        end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).date().isoformat()
        where = ["date >= ?", "date <= ?"]
        params: list[Any] = [today, end]
        if tickers:
            where.append(f"ticker IN ({','.join('?' * len(tickers))})")
            params.extend([t.upper() for t in tickers])
        rows = self.conn.execute(
            f"SELECT * FROM calendar WHERE {' AND '.join(where)} ORDER BY date, ticker",
            params,
        ).fetchall()
        # The primary key includes the label, so one earnings date announced by
        # the issuer and restated by an aggregator lands as two rows. That is
        # one date; printing it twice makes a seven-line calendar look like a
        # fourteen-line one and invites the reader to wonder which is real.
        best: dict[tuple[str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str]] = []
        for row in rows:
            entry = dict(row)
            key = (entry["ticker"], entry["kind"], entry["date"])
            current = best.get(key)
            if current is None:
                entry["also_reported_by"] = []
                best[key] = entry
                order.append(key)
                continue
            weaker, stronger = (
                (current, entry) if (entry.get("confidence") or 0) >
                (current.get("confidence") or 0) else (entry, current))
            stronger["also_reported_by"] = (
                current.get("also_reported_by") or []) + [weaker.get("source")]
            best[key] = stronger
        return [best[k] for k in order]

    def counts(self) -> dict[str, Any]:
        c = self.conn.execute
        return {
            "items": c("SELECT COUNT(*) n FROM items").fetchone()["n"],
            "links": c("SELECT COUNT(*) n FROM item_tickers").fetchone()["n"],
            "alerts_24h": c(
                "SELECT COUNT(DISTINCT uid) n FROM items "
                "WHERE tier='ALERT' AND published_at >= ?",
                [(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()],
            ).fetchone()["n"],
            "by_source": {
                r["source"]: r["n"]
                for r in c("SELECT source, COUNT(*) n FROM items "
                           "GROUP BY source ORDER BY n DESC").fetchall()
            },
        }

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["meta"] = json.loads(d.pop("meta_json", None) or "{}")
        d["events"] = json.loads(d.pop("events_json", None) or "[]")
        d["reasons"] = json.loads(d.pop("reasons_json", None) or "[]")
        return d


def _cap_per_ticker(rows: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """Stop one busy name owning the whole page.

    Clustering is SimHash over titles, which catches a syndicated copy but not
    the same event told five different ways: "Teva Delivers Strong Q2 Results",
    "Teva Lifts UZEDY Outlook", "Branded drugs boost Teva revenue" and the 8-K
    behind all of them landed in four separate clusters, and on results day Teva
    held five of the top twelve slots while eleven other names showed nothing.

    Rows arrive already ranked, so this keeps each name's best and drops its
    tail. Nothing is lost that the reader cannot reach: `harel brief TEVA` and a
    ticker-filtered feed both bypass the cap.
    """
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("ticker") or "")
        if key:
            if seen.get(key, 0) >= cap:
                continue
            seen[key] = seen.get(key, 0) + 1
        out.append(row)
    return out


def _collapse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the highest-scoring item per (cluster, ticker); attach the rest as `also`."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (row.get("cluster_id") or row["uid"], row.get("ticker", ""))
        current = best.get(key)
        if current is None:
            row["also"] = []
            best[key] = row
            order.append(key)
        elif row["ticker_score"] > current["ticker_score"]:
            row["also"] = current["also"] + [
                {"source": current["source"], "title": current["title"], "url": current["url"]}
            ]
            best[key] = row
        else:
            current["also"].append(
                {"source": row["source"], "title": row["title"], "url": row["url"]}
            )
    return [best[k] for k in order]
