"""Generic RSS/Atom collector.

Drives four different kinds of feed, all of which happen to speak RSS:

* ``feeds:``            static list (FDA, EMA, DoD contracts, FERC, Globes…)
* ``feeds_from:``       per-ticker issuer feeds declared in universe.yaml
* ``base_url`` w/ {q}   query-per-entity feeds (Google News), expanded into one
                        request per ticker plus one per sector theme cluster
"""

from __future__ import annotations

import calendar
import html
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser

from ..enrich.linker import direct_evidence
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
# A feed emitting placeholders must not turn one pass into a hundred fetches.
MAX_TITLE_LOOKUPS_PER_RUN = 5

# Headlines that carry no information - CMS templating leftovers, almost always.
_PLACEHOLDER_TITLES = {
    "title", "untitled", "no title", "notitle", "post title", "page title",
    "default title", "sample", "test", "rss", "feed", "news", "item",
}
_OG_TITLE = re.compile(
    r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']{4,300})",
    re.IGNORECASE)
_HTML_TITLE = re.compile(r"<title[^>]*>([^<]{4,300})</title>", re.IGNORECASE)


@register("rss")
class RssCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        self._title_lookups = 0
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
        """Terms a cross-read result must actually contain to keep its tag."""
        if seed_relation not in ("PRODUCT_RIVAL", "PEER") or len(seed_tickers) != 1:
            return []
        tc = self.cfg.ticker(seed_tickers[0])
        if not tc:
            return []
        return _cross_read_terms(tc, seed_relation)

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
                # Named rival PRODUCTS where they exist. Where they do not - a
                # merchant power generator has peers, not product rivals - fall
                # back to the peer companies and label the result PEER, which
                # carries a lower multiplier. The alternative, listing peer
                # companies as "products", scored an NRG earnings preview as a
                # Kenon product-rival event at 36.
                relation = "PRODUCT_RIVAL" if tc.competitor_products else "PEER"
                terms = _cross_read_terms(tc, relation)
                if terms:
                    rival_q = " OR ".join(f'"{t}"' for t in terms)
                    out.append((base.replace("{q}", quote_plus(rival_q)), [ticker],
                                relation,
                                f"{ticker} rival products" if relation == "PRODUCT_RIVAL"
                                else f"{ticker} peer companies"))

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
        # Only for the query-driven per-ticker searches. An issuer's own feed is
        # authoritative even when a post never spells the company's name out.
        query_direct = (seed_relation == "DIRECT" and len(seed_tickers) == 1
                        and "{q}" in (self.source.base_url or ""))

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
                hit = next((t for t in required if t.lower() in haystack), None)
                if hit is None:
                    continue
                # Record the term that earned the tag. Without it the link reads
                # "collected from google_news as product_rival", which asks the
                # reader to take the competitor claim on trust.
                kind = ("a rival product" if seed_relation == "PRODUCT_RIVAL"
                        else "a peer company")
                item.meta["matched_term"] = hit
                item.meta["seed_why"] = (
                    f'the story names "{hit}", tracked as {kind} for '
                    f'{seed_tickers[0]}'
                )
            elif query_direct:
                # A search engine answering loosely is not evidence. Without
                # this the Allot query's "PH, US allot P42b for anti-TB, HIV
                # drive" was DIRECT news about Allot Communications at 0.92.
                tc = self.cfg.ticker(seed_tickers[0])
                if tc and not direct_evidence(tc, f"{item.title} {item.summary}"):
                    continue
                item.meta["seed_why"] = f'found by our "{label}" search'
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
        link = entry.get("link") or entry.get("id") or ""
        if _is_placeholder_title(title):
            # BrainsWay's feed emitted an entry whose title was literally
            # "Title". It linked to BWAY, scored 32 and sat in the feed as
            # company news. A headline that says nothing is a parser problem,
            # not a story: try the page's own title, then give up loudly.
            recovered = self._title_from_page(link)
            if not recovered:
                self.warn(f"{label}: entry has no usable title "
                          f"({title!r}) - skipped: {link[:120]}")
                return None
            title = recovered
        published, dated = _entry_datetime(entry)

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
                **({} if dated else {"undated": True}),
                "publisher": (entry.get("source") or {}).get("title")
                if isinstance(entry.get("source"), dict) else entry.get("author"),
            },
        )


    def _title_from_page(self, url: str) -> str | None:
        """Last resort for an entry with no usable headline: ask the page.

        Bounded per run - a feed that emits nothing but placeholders must not
        turn one collection pass into a hundred page fetches.
        """
        if not url or self._title_lookups >= MAX_TITLE_LOOKUPS_PER_RUN:
            return None
        self._title_lookups += 1
        try:
            resp = self.client.get(url, allow_status=(403, 404, 410))
        except Exception:
            return None
        if resp.status >= 400:
            return None
        for pattern in (_OG_TITLE, _HTML_TITLE):
            match = pattern.search(resp.text or "")
            if match:
                candidate = html.unescape(match.group(1)).strip()
                if not _is_placeholder_title(candidate):
                    return candidate[:300]
        return None


def _is_placeholder_title(title: str) -> bool:
    """A headline that carries no information. Templating leftovers, mostly."""
    cleaned = re.sub(r"[^\w\s]", "", (title or "").strip()).strip().lower()
    return cleaned in _PLACEHOLDER_TITLES or len(cleaned) < 3


def _entry_datetime(entry) -> tuple[datetime, bool]:
    """(timestamp, was_it_actually_dated).

    An undated entry used to be stamped "now" and nothing recorded that we had
    invented the timestamp. That is how BrainsWay's site feed put an entry
    literally titled "Title" into the feed as breaking news, one minute old, at
    issuer trust - and how any evergreen page becomes today's headline. We still
    stamp it, so it is not silently dropped, but the flag travels with it and
    the scorer caps it.
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc), True
    return datetime.fromtimestamp(time.time(), tz=timezone.utc), False


def _cross_read_terms(tc, relation: str) -> list[str]:
    """The search terms behind a cross-read query, in their original case so the
    stated reason can quote them back.

    Terms shorter than five characters are dropped: a three-letter brand is a
    false-positive machine in a headline.
    """
    source = tc.competitor_products if relation == "PRODUCT_RIVAL" else tc.peer_names
    return [t for t in source if len(t) >= 5][:MAX_RIVAL_TERMS_PER_QUERY]


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
