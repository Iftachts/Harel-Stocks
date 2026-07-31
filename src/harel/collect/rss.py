"""Generic RSS/Atom collector.

Drives four different kinds of feed, all of which happen to speak RSS:

* ``feeds:``            static list (FDA, EMA, DoD contracts, FERC, Globes…)
* ``feeds_from:``       per-ticker issuer feeds declared in universe.yaml
* ``base_url`` w/ {q}   query-per-entity feeds (Google News), expanded into one
                        request per ticker plus one per sector theme cluster
"""

from __future__ import annotations

import calendar
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

# Google News dedupes poorly across near-identical queries, so we keep the
# theme queries deliberately few and high-signal.
MAX_THEME_QUERIES_PER_SECTOR = 3
# Rival-product terms are specific enough to query directly ("Genesys Cloud CX",
# "NeuroStar", "Hughes JUPITER"). Bare peer *company* names are not - querying
# "Microsoft" or "Salesforce" for NICE would bury the feed - so peers stay on
# the existing match-in-collected-content path.
MAX_RIVAL_TERMS_PER_QUERY = 6


@register("rss")
class RssCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        for feed_url, seed_tickers, seed_relation, label in self._feed_plan():
            try:
                yield from self._read_feed(feed_url, seed_tickers, seed_relation, label)
            except HttpError as exc:
                self.warn(f"{label}: {exc}")
            except Exception as exc:  # a single broken feed must not kill the run
                self.warn(f"{label}: unexpected {type(exc).__name__}: {exc}")

    # -- planning ---------------------------------------------------------- #
    def _feed_plan(self) -> list[tuple[str, list[str], str, str]]:
        raw = self.source.raw
        plan: list[tuple[str, list[str], str, str]] = []

        for url in self.source.feeds:
            plan.append((url, [], "SECTOR_THEME", url))

        if raw.get("feeds_from") == "universe.ir_feeds":
            for ticker in self.active_tickers:
                tc = self.cfg.ticker(ticker)
                if not tc:
                    continue
                for url in tc.ir_feeds:
                    plan.append((url, [ticker], "DIRECT", f"{ticker} IR"))

        base = self.source.base_url
        if base and "{q}" in base:
            plan.extend(self._query_plan(base))

        return plan

    def _required_terms(self, seed_tickers: list[str], seed_relation: str) -> list[str]:
        """Terms a rival-product result must actually contain to keep its tag."""
        if seed_relation != "PRODUCT_RIVAL" or len(seed_tickers) != 1:
            return []
        tc = self.cfg.ticker(seed_tickers[0])
        if not tc:
            return []
        return [r.lower() for r in tc.competitor_products
                if len(r) >= 5][:MAX_RIVAL_TERMS_PER_QUERY]

    def _query_plan(self, base: str) -> list[tuple[str, list[str], str, str]]:
        hebrew = "hl=iw" in base or self.source.key.endswith("_he")
        out: list[tuple[str, list[str], str, str]] = []

        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            if hebrew:
                terms = [a for a in tc.aliases if _is_hebrew(a)]
                if not terms:
                    continue
                query = " OR ".join(f'"{t}"' for t in terms[:3])
            else:
                # Ticker alone is too noisy (ICL, ORA, KEN, NICE are real words),
                # so we anchor on the company name and only add the bare ticker
                # for symbols that are not English words.
                terms = [f'"{tc.name}"']
                terms += [f'"{a}"' for a in tc.aliases[:2] if not _is_hebrew(a)]
                if not _is_wordlike(ticker):
                    terms.append(f'"{ticker}" stock')
                query = " OR ".join(terms)
            out.append((base.replace("{q}", quote_plus(query)), [ticker], "DIRECT",
                        f"{ticker} news"))

            # Analyst actions are a top-3 intraday mover and the paid feed for
            # them is the largest documented gap in this system. They do reach
            # us free through the wires - 13 were already classified as
            # rating_change - but only by accident, whenever an aggregator
            # happened to surface one. Asking for them directly turns that from
            # incidental into deliberate coverage for all 22 names.
            if not hebrew:
                rating_q = (f'"{tc.name}" (upgrade OR downgrade OR '
                            f'"price target" OR "initiated coverage")')
                out.append((base.replace("{q}", quote_plus(rating_q)), [ticker],
                            "DIRECT", f"{ticker} analyst actions"))

            # Cross-read needs competitor CONTENT, not just competitor rules.
            # Every other query here is seeded from our own names, so the only
            # rival stories we ever saw were ones that already mentioned us -
            # which is why PRODUCT_RIVAL stayed empty for every name whose
            # rivals do not file with the SEC or register trials.
            if not hebrew:
                rivals = [r for r in tc.competitor_products
                          if len(r) >= 5][:MAX_RIVAL_TERMS_PER_QUERY]
                if rivals:
                    rival_q = " OR ".join(f'"{r}"' for r in rivals)
                    out.append((base.replace("{q}", quote_plus(rival_q)), [ticker],
                                "PRODUCT_RIVAL", f"{ticker} rival products"))

        if not hebrew:
            for sector_key in {self.cfg.ticker(t).sector for t in self.active_tickers
                               if self.cfg.ticker(t)}:
                sector = self.cfg.sector(sector_key)
                tickers = [t for t in self.active_tickers
                           if self.cfg.ticker(t) and self.cfg.ticker(t).sector == sector_key]
                for term in sector.high_impact_events[:MAX_THEME_QUERIES_PER_SECTOR]:
                    out.append(
                        (base.replace("{q}", quote_plus(f'"{term}"')), tickers,
                         "SECTOR_THEME", f"{sector_key}: {term}")
                    )
        return out

    # -- fetching ---------------------------------------------------------- #
    def _read_feed(self, url: str, seed_tickers: list[str], seed_relation: str,
                   label: str) -> Iterator[RawItem]:
        state_key = f"{self.source.key}:{url}"
        prev = self.db.get_source_state(state_key)
        resp = self.client.get(
            url,
            etag=prev.get("etag"),
            last_modified=prev.get("last_modified"),
            allow_status=(404, 403, 410),
        )
        now = datetime.now(timezone.utc).isoformat()

        if resp.not_modified:
            self.db.set_source_state(state_key, last_run_at=now, last_ok_at=now,
                                     consecutive_failures=0, items_last_run=0)
            return
        if resp.status >= 400:
            self.db.set_source_state(
                state_key, last_run_at=now, last_error=f"HTTP {resp.status}",
                consecutive_failures=int(prev.get("consecutive_failures") or 0) + 1,
            )
            self.warn(f"{label}: HTTP {resp.status} - feed may have moved")
            return

        parsed = feedparser.parse(resp.content)
        if parsed.get("bozo") and not parsed.entries:
            self.warn(f"{label}: unparseable feed ({parsed.get('bozo_exception')})")
            return

        # Google News answers an OR query loosely, so a rival-product search
        # returns plenty of stories that mention none of the terms. Tagging
        # those PRODUCT_RIVAL would assert a competitor link that does not
        # exist, so require the term to actually appear.
        required = self._required_terms(seed_tickers, seed_relation)

        count = 0
        for entry in parsed.entries:
            try:
                item = self._entry_to_item(entry, url, seed_tickers, seed_relation, label)
            except Exception as exc:
                self.warn(f"{label}: bad entry ({type(exc).__name__}: {exc})")
                continue
            if item is None or item.published_at < self.ctx.since:
                continue
            if required:
                haystack = f"{item.title} {item.summary}".lower()
                hit = next((t for t in required if t in haystack), None)
                if hit is None:
                    continue
                # Record the term that earned the tag. Without it the link reads
                # "collected from google_news as product_rival", which asks the
                # reader to take the competitor claim on trust.
                item.meta["matched_term"] = hit
                item.meta["seed_why"] = (
                    f'the story names "{hit}", tracked as a rival product for '
                    f'{seed_tickers[0]}'
                )
            elif seed_tickers:
                item.meta["seed_why"] = f'found by our "{label}" search'
            count += 1
            yield item

        self.db.set_source_state(
            state_key,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
            last_run_at=now, last_ok_at=now, last_error=None,
            consecutive_failures=0, items_last_run=count,
        )

    def _entry_to_item(self, entry, feed_url: str, seed_tickers: list[str],
                       seed_relation: str, label: str) -> RawItem | None:
        title = (entry.get("title") or "").strip()
        if not title:
            return None
        link = entry.get("link") or entry.get("id") or ""
        published = _entry_datetime(entry)

        summary = entry.get("summary") or ""
        if not summary and entry.get("content"):
            summary = entry["content"][0].get("value", "")

        external_id = entry.get("id") or link or f"{feed_url}#{title}"

        return self.make_item(
            external_id=external_id,
            title=title,
            url=link,
            summary=summary,
            published_at=published,
            lang="he" if _is_hebrew(title) else "en",
            seed_tickers=list(seed_tickers),
            seed_relation=seed_relation,
            meta={
                "feed": feed_url,
                "feed_label": label,
                "publisher": (entry.get("source") or {}).get("title")
                if isinstance(entry.get("source"), dict) else entry.get("author"),
            },
        )


def _entry_datetime(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    # No date at all: assume "now" so a live item is not silently dropped by the
    # lookback filter. Marked in meta so the scorer can distrust it if needed.
    return datetime.fromtimestamp(time.time(), tz=timezone.utc)


def _is_hebrew(text: str) -> bool:
    return any("֐" <= ch <= "׿" for ch in text or "")


# Symbols that are also ordinary English words, so `"<TICKER>" stock` returns
# somebody else's story. GILT earned its place the hard way: the drill-down page
# showed "REG - FTSE Russell - 0 1/8% Index-linked Treasury Gilt 2041" tagged
# DIRECT for Gilat Satellite. A gilt is a UK government bond.
_ENGLISH_WORD_TICKERS = {"ICL", "ORA", "KEN", "NICE", "ALLT", "PERI", "ONE", "ALL",
                         "GILT"}


def _is_wordlike(ticker: str) -> bool:
    return ticker.upper() in _ENGLISH_WORD_TICKERS
