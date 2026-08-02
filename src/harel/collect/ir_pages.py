"""Issuer IR pages for names that publish no feed at all.

Two of the twenty-two declare no RSS anywhere: AudioCodes (checked 2026-08-02 -
no feed on audiocodes.com, and the GlobeNewswire organisation feed that carries
its wire copy never answers this host at all) and NICE (no feed on nice.com, and
its IR site *is* nice.com - Q4 hosts only the webcasts). Both publish their
reporting date on their own site as ordinary HTML, weeks ahead:

    AUDC  "will release financial results for its Second Quarter 2026 on
           August 4, 2026, before the market open"
    NICE  "Q2 2026 Earnings Release Conference Call / Date: Wednesday,
           August 5, 2026"

Until this collector existed those two dates reached us only through
google_news, which returns a volatile subset - the date was in the feed one hour
and gone the next, so the calendar entry appeared and disappeared with it, and
it arrived attributed to an aggregator rather than to the issuer. An issuer's
own site is a first-party source; that is the whole point of coming here.

This does NOT parse dates itself. It emits the row's text and lets
`pipeline._earnings_date` read it, so there is one definition of "an issuer
announcing when it will report" and not two that drift.

Marked ``fragile`` for the obvious reason: these are marketing sites that
re-theme without notice. The design rule that follows from that is that a
layout change must be loud - see `_read_page`, which warns when a page yields no
dated rows and again when the row count collapses against the last good pass.
A silent zero here would look exactly like a quiet quarter.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register
from .fda import _stored_dt
from .rss import IR_CALENDAR_LOOKBACK_DAYS, _is_hebrew, _page_text

# AudioCodes' financial-news page carries its entire history - 227 rows on
# 2026-08-02 - so the parse is bounded. The rows are sorted by date before the
# cap is applied, never taken in page order: a page that lists oldest first
# would otherwise lose exactly the row that matters.
MAX_ROWS_PER_PAGE = 400
# Following a row to its release page is the only way to read AudioCodes' date:
# the listing says "Announces Second Quarter 2026 Reporting Date" and the date
# itself - August 4, 2026 - is in the release. Same budget reasoning as
# rss.MAX_BODY_LOOKUPS_PER_RUN: a page of forty announcements must not turn one
# collection pass into forty fetches. Two pages, one live announcement each.
MAX_BODY_LOOKUPS_PER_RUN = 6
# Enough for the lede, where a release states its reporting date. Same cap and
# same reason as rss.MAX_BODY_CHARS.
MAX_BODY_CHARS = 4000
MAX_SUMMARY_CHARS = 600
# How far above a date we will look for the headline that belongs to it. NICE's
# events cards put the title *before* the date ("Q2 2026 Earnings Release
# Conference Call" then "Date: Wednesday, August 5, 2026"); AudioCodes' rows put
# it after. Three blocks is enough for the first and small enough that we do not
# reach back into the previous row.
LOOKBEHIND_BLOCKS = 3
# A row that yields fewer rows than half of the last good pass is reported. A
# re-theme rarely takes the whole page to zero - it takes it from forty rows to
# the two that happen to still match, which reads as "quiet quarter" and is the
# single most repeated failure in this repo's history.
COLLAPSE_RATIO = 0.5


@register("ir_page")
class IrPageCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        self._body_lookups = 0
        self._check_attribution()
        for url, ticker in self._page_plan():
            label = f"{ticker} IR page"
            try:
                yield from self._read_page(url, ticker, label)
            except HttpError as exc:
                self.warn(f"{label}: {exc}")
            except Exception as exc:  # one broken page must not kill the run
                self.warn(f"{label}: unexpected {type(exc).__name__}: {exc}")

    def _check_attribution(self) -> None:
        """The reason for coming here is that the ISSUER said it.

        `pipeline._FIRST_PARTY_SOURCES` is what turns that into "Q2 results
        (company-announced date)" at 0.95 instead of "reported by
        company_ir_pages" at 0.8. It is a set of source keys, so renaming this
        source in sources.yaml quietly downgrades every date it finds to
        aggregator standing - a wrong label on a right date, which is the kind
        of fault nobody looks for. Assert it out loud instead.
        """
        from ..pipeline import _FIRST_PARTY_SOURCES

        if self.source.key not in _FIRST_PARTY_SOURCES:
            self.warn(f"{self.source.key} is not in pipeline._FIRST_PARTY_SOURCES, "
                      f"so dates read straight off an issuer's own site will be "
                      f"filed as second-hand")

    def _page_plan(self) -> list[tuple[str, str]]:
        """(page url, ticker). Configured in universe.yaml next to ir_feeds, so
        adding a name is a config change - the two URLs here are not the point,
        the shape is."""
        if self.source.raw.get("pages_from") != "universe.ir_pages":
            return []
        plan: list[tuple[str, str]] = []
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            for url in tc.raw.get("ir_pages") or []:
                if url:
                    plan.append((str(url), ticker))
        return plan

    # -- fetching ---------------------------------------------------------- #
    def _read_page(self, url: str, ticker: str, label: str) -> Iterator[RawItem]:
        state_key = f"{self.source.key}:{url}"
        prev = self.db.get_source_state(state_key)
        # No conditional GET, for the reason issuer feeds are also read
        # unconditionally (see rss._read_feed): "nothing new" is the right answer
        # for news and the wrong one for the calendar. NICE's events page does
        # not change between the day it announces 5 August and the day it
        # reports, so honouring an ETag would mean that date could only ever be
        # missed, never re-read.
        resp = self.client.get(url, allow_status=(403, 404, 410))
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()

        if resp.status >= 400:
            self.db.set_source_state(
                state_key, last_run_at=stamp, last_error=f"HTTP {resp.status}",
                consecutive_failures=int(prev.get("consecutive_failures") or 0) + 1,
            )
            self.warn(f"{label}: HTTP {resp.status} for {url} - page moved or "
                      f"now blocks us")
            return

        rows = _page_rows(resp.text or "", now.date())
        # The loud failure. A scrape that finds nothing has not had a quiet day.
        if not rows:
            self.warn(f"{label}: no dated rows found on {url} - the page layout "
                      f"has changed and this name now has no first-party date")
            # Also recorded against the SOURCE, not only the page, so it shows in
            # `harel doctor` and not just in one run's warning list. The pipeline
            # honours an error a collector recorded during the pass it just ran
            # (see its `recorded` check), which is what keeps a broken parser
            # from reading as healthy-and-quiet.
            self.save_state(last_error=f"{label}: {url} parsed into no rows")
        else:
            before = int(prev.get("items_last_run") or 0)
            if before and len(rows) < before * COLLAPSE_RATIO:
                # Not an error, because a company is allowed to prune its own
                # archive. A halving is still the shape a re-theme takes - forty
                # rows down to the two that happen to still match - and that
                # reads as a quiet quarter to anyone not counting.
                self.warn(f"{label}: {url} parsed {len(rows)} rows, down from "
                          f"{before} - the layout has probably changed")

        # `items_last_run` here counts rows PARSED, not items emitted. Emission
        # is dominated by the age window and swings between 0 and 1 all quarter;
        # the row count is the number that says whether the parser still
        # understands the page, which is what the collapse check above needs.
        self.db.set_source_state(
            state_key, last_run_at=stamp, last_ok_at=stamp, last_error=None,
            consecutive_failures=0, items_last_run=len(rows),
        )

        calendar_since = now - timedelta(days=IR_CALENDAR_LOOKBACK_DAYS)
        newest_first = sorted(rows, key=lambda r: r.date, reverse=True)
        for row in newest_first[:MAX_ROWS_PER_PAGE]:
            try:
                item = self._row_to_item(row, url, ticker, now)
            except Exception as exc:
                self.warn(f"{label}: bad row ({type(exc).__name__}: {exc}) "
                          f"- {row.title[:80]!r}")
                continue
            if item is None:
                continue
            if row.event_dated:
                # An events card states when the event IS, not when the page
                # said so. It is undated (see `_row_to_item`) and therefore
                # ageless, so the age window below cannot judge it; the date it
                # names is exactly what we came for.
                yield item
                continue
            if item.published_at < calendar_since:
                continue
            self._add_release_body(item, label)
            if item.published_at < self.ctx.since:
                # Too old to be news. Kept only for a reporting date that has
                # not happened yet - the rule rss already applies to issuer
                # feeds, and the reason AudioCodes' 6 July announcement of a
                # 4 August date is visible at all on 2 August.
                if not _future_results_date(item):
                    continue
                item.meta["calendar_backfill"] = True
            yield item

    def _row_to_item(self, row: _Row, page_url: str, ticker: str,
                     now: datetime) -> RawItem | None:
        if not row.title:
            return None
        # The row's own identity, not its links. An events card's only href is a
        # webcast registration URL that is re-issued per quarter and points at a
        # third party; the headline is what the row IS.
        external_id = f"{page_url}#{_slug(row.title)}"
        url = urljoin(page_url, row.href) if row.href else page_url

        if row.event_dated:
            # A date in the future is the event's date, not a publication date -
            # the same refusal fda.HtmlListingCollector._row_date makes about a
            # warning letter's response deadline. Stamping an item "published"
            # three days from now would make it permanently the newest thing we
            # hold. So: the undated convention, with the first stamp we invented
            # frozen, exactly as the scraped-warning-letter path does.
            prior = self.db.stored_meta(self.source.key, external_id) or {}
            published = _stored_dt(prior.get("first_seen")) or now
            dating = {"undated": True, "first_seen": published.isoformat(),
                      "event_date": row.date.isoformat()}
        else:
            published = datetime.combine(row.date, datetime.min.time(),
                                         tzinfo=timezone.utc)
            dating = {"listing_date": row.date.isoformat()}

        return self.make_item(
            external_id=external_id,
            title=row.title[:300],
            url=url,
            summary=row.summary[:MAX_SUMMARY_CHARS],
            published_at=published,
            lang="he" if _is_hebrew(row.title) else "en",
            seed_tickers=[ticker],
            seed_relation="DIRECT",
            meta={
                "ir_page": page_url,
                "scraped": True,
                "fragile_source": True,
                "seed_why": f"published on {ticker}'s own IR page",
                **dating,
            },
        )

    def _add_release_body(self, item: RawItem, label: str) -> None:
        """Follow a row to its release when the listing withholds the date.

        AudioCodes' listing row is "Jul 06, 2026 | Financial - AudioCodes
        Announces Second Quarter 2026 Reporting Date". It names the quarter and
        it names that a date exists; the date itself, 4 August, is only in the
        release. Restricted to headlines that read like a reporting-date
        announcement, so a listing of forty releases costs one fetch.
        """
        if item.body or not item.url or item.url == item.meta.get("ir_page"):
            return
        if self._body_lookups >= MAX_BODY_LOOKUPS_PER_RUN:
            return
        if not _announces_a_reporting_date(item.title):
            return
        self._body_lookups += 1
        try:
            resp = self.client.get(item.url, allow_status=(403, 404, 410))
        except Exception as exc:
            self.warn(f"{label}: could not read the release behind "
                      f"{item.title[:60]!r} ({type(exc).__name__}: {exc})")
            return
        if resp.status >= 400:
            self.warn(f"{label}: HTTP {resp.status} for the release behind "
                      f"{item.title[:60]!r} - its date is unreadable")
            return
        item.body = _page_text(resp.text or "")[:MAX_BODY_CHARS]


# --------------------------------------------------------------------------- #
# Parsing. Deliberately structure-blind: no CSS class or selector appears here.
#
# The obvious parser keys on `li.item-news` and `div.prev-events-card`, which is
# what those two pages call their rows on 2026-08-02 and nothing either company
# has promised to keep calling them. What the two layouts genuinely have in
# common is not their classes - it is that an entry is a date sitting next to a
# headline. So: find the dates, and pair each with the nearest block of text
# that reads like a headline. A re-theme then still parses; a rename of the
# markup does not quietly become a quarter with no announcements.
# --------------------------------------------------------------------------- #
_STRIPPED_TAGS = re.compile(
    r"(?is)<(script|style|noscript|nav|header|footer|select|option|svg)[^>]*>.*?</\1>")
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_TAG = re.compile(r"<[^>]+>")
_ANCHOR_OPEN = re.compile(r"""(?is)^<a\s[^>]*?href=["']?\s*([^"'>\s]+)""")
_ANCHOR_CLOSE = re.compile(r"(?i)^</a")

_MONTHS = ("january february march april may june july august september october "
           "november december").split()
# Longest form first: an alternation takes the FIRST branch that matches, so
# "sep" listed before "sept" leaves a stray "t" and throws the date away. Same
# trap, and the same fix, as pipeline._MONTH_FORMS.
_MONTH_ALT = "|".join(
    form for month in _MONTHS
    for form in ((month, "sept", month[:3]) if month == "september"
                 else (month, month[:3]) if len(month) > 3 else (month,))
)
# A four-digit year is REQUIRED here, unlike the extractor in pipeline.py, and
# the difference is deliberate: that one reads a sentence which has already been
# established to be an earnings announcement, this one decides whether a piece
# of page furniture is a row at all. "Aug 5" appears in navigation chrome, in
# copyright lines and in body copy; "Aug 5, 2026" in a block of its own is a
# listing row. The cost is a page that dates its rows numerically, which yields
# no rows and therefore a warning - a documented gap, not a wrong date.
_DATE_MDY = re.compile(
    r"\b(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b",
    re.IGNORECASE)
_DATE_DMY = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_ALT + r")\.?,?\s+(20\d{2})\b",
    re.IGNORECASE)
# "Date:", "Time:", "Webcast Registration Link:" - a label, not a headline. The
# label is stripped before a block is measured, so "Time: 8:30 ET, 15:30 IL"
# shrinks to something too short to be mistaken for the event's name. Without
# this it was five words long and sat one block closer to NICE's date than the
# real title did.
_LABEL_PREFIX = re.compile(r"^[^:]{1,45}:\s*")
MIN_TITLE_WORDS = 4
MIN_TITLE_CHARS = 20


class _Row:
    """One entry on a listing page: a date, the headline beside it, a link.

    ``event_dated`` says which kind of date it is. A press-release listing
    states when the release went out; an events page states when the event will
    be, which is not a publication date and must never be stamped as one.
    """

    __slots__ = ("date", "event_dated", "title", "summary", "href")

    def __init__(self, date_: date, event_dated: bool, title: str, summary: str,
                 href: str) -> None:
        self.date = date_
        self.event_dated = event_dated
        self.title = title
        self.summary = summary
        self.href = href


class _Block:
    __slots__ = ("text", "href")

    def __init__(self, text: str, href: str) -> None:
        self.text = text
        self.href = href


def _text_blocks(page: str) -> list[_Block]:
    """The page as a flat list of text runs, each remembering the link it sits
    in. Comments go first: NICE's server-rendered markup separates text nodes
    with a bare `<!-- -->`, so "Date: <!-- -->Wednesday, August 5, 2026" is one
    sentence only after they are removed."""
    cleaned = _STRIPPED_TAGS.sub(" ", _HTML_COMMENT.sub("", page))
    blocks: list[_Block] = []
    href = ""
    pos = 0
    for tag in _TAG.finditer(cleaned):
        run = cleaned[pos:tag.start()]
        pos = tag.end()
        text = " ".join(html.unescape(run).split())
        if text:
            blocks.append(_Block(text, href))
        raw_tag = tag.group(0)
        opened = _ANCHOR_OPEN.match(raw_tag)
        if opened:
            href = html.unescape(opened.group(1))
        elif _ANCHOR_CLOSE.match(raw_tag):
            href = ""
    return blocks


def _page_rows(page: str, today: date) -> list[_Row]:
    blocks = _text_blocks(page)
    dated = [(i, found) for i, block in enumerate(blocks)
             if (found := _block_date(block.text))]
    rows: list[_Row] = []
    for n, (index, found) in enumerate(dated):
        previous = dated[n - 1][0] if n else -1
        # The row runs from this date to the next one, plus a short lookbehind
        # for the layouts that put the headline above the date. The lookbehind
        # never crosses the previous date, so no row can inherit a neighbour's.
        start = max(previous + 1, index - LOOKBEHIND_BLOCKS)
        end = dated[n + 1][0] if n + 1 < len(dated) else len(blocks)

        title, href = _row_title(blocks, start, index, end)
        if not title:
            continue
        # The summary is the row PROPER - date block to next date block, no
        # lookbehind. It is what `_earnings_date` reads for a date the headline
        # does not carry ("Date: Wednesday, August 5, 2026"), so letting it
        # reach back above the date would let a row quote the row above it.
        summary = " ".join(b.text for b in blocks[index:end])
        rows.append(_Row(found, found > today, title, summary, href))
    return rows


def _row_title(blocks: list[_Block], start: int, index: int,
               end: int) -> tuple[str, str]:
    """The headline nearest this row's date, and the link it carries.

    Nearest, not first: AudioCodes writes date-then-headline and NICE's upcoming
    card writes headline-then-date, and both are satisfied by distance. Ties go
    to the block after the date, which is the more common shape.
    """
    best: tuple[int, int, _Block] | None = None
    for offset in list(range(index + 1, end)) + list(range(start, index)):
        block = blocks[offset]
        if not _headline_shaped(block.text):
            continue
        rank = (abs(offset - index), 0 if offset > index else 1)
        if best is None or rank < best[:2]:
            best = (*rank, block)
    if best is None:
        return "", ""
    title_block = best[2]
    return title_block.text, _row_href(blocks, start, end, title_block)


def _row_href(blocks: list[_Block], start: int, end: int,
              title_block: _Block) -> str:
    """The row's own link, or nothing.

    Only same-site links count. NICE's events cards link out to
    events.q4inc.com and registrations.events - a registration form is not the
    announcement, and pointing the feed at one would send a reader to a signup
    page instead of to the issuer's statement.
    """
    for candidate in (title_block, *blocks[start:end]):
        href = candidate.href
        if href and not urlsplit(href).netloc:
            return href
    return ""


def _headline_shaped(text: str) -> bool:
    stripped = _LABEL_PREFIX.sub("", text).strip()
    return len(stripped) >= MIN_TITLE_CHARS and len(stripped.split()) >= MIN_TITLE_WORDS


def _block_date(text: str) -> date | None:
    """The date this block states, if it states one."""
    for pattern, month_first in ((_DATE_MDY, True), (_DATE_DMY, False)):
        match = pattern.search(text)
        if not match:
            continue
        raw_month = match.group(1) if month_first else match.group(2)
        raw_day = match.group(2) if month_first else match.group(1)
        month = _month_index(raw_month)
        if month is None:
            continue
        try:
            return date(int(match.group(3)), month, int(raw_day))
        except ValueError:
            return None
    return None


def _month_index(token: str) -> int | None:
    token = token.lower().rstrip(".")
    for index, name in enumerate(_MONTHS):
        if name.startswith(token):
            return index + 1
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:120]


# A reporting-date announcement as an IR *listing* writes it. rss's
# `_announces_results` is the shared definition and is tried first, but it was
# tuned on wire headlines, which carry the announcing verb: "AudioCodes
# Announces Second Quarter 2026 Reporting Date" has none, fails both of its
# legs, and is the exact headline this collector exists to follow.
_REPORTING_DATE = re.compile(
    r"\b(reporting|earnings|results|report)\s+date\b|"
    r"\bdate\s+(of|for)\s+(its\s+)?(first|second|third|fourth)[- ]quarter\b",
    re.IGNORECASE)


def _announces_a_reporting_date(title: str) -> bool:
    from .rss import _announces_results

    return bool(_announces_results(title) or _REPORTING_DATE.search(title))


def _future_results_date(item: RawItem) -> bool:
    """Deferred, because `pipeline` imports this package. Imported rather than
    reimplemented for the reason stated at the top of the module."""
    from ..pipeline import _earnings_date

    return _earnings_date(item) is not None
