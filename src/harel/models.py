"""Normalized data model.

Every collector, regardless of source, emits :class:`RawItem`. The pipeline
enriches it into a :class:`ScoredItem` which is what the DB and the LLM agent
actually see.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# --------------------------------------------------------------------------- #
# Relations: how an item connects to one of our tickers.
# --------------------------------------------------------------------------- #
RELATIONS = (
    "DIRECT",         # the issuer itself
    "SUBSIDIARY",     # a controlled entity reporting under another name
    "PRODUCT_RIVAL",  # same molecule / mechanism / design socket
    "PEER",           # named competitor
    "CUSTOMER",       # a customer whose spend drives our revenue
    "SUPPLIER",
    "SECTOR_REG",     # regulator action on our sector
    "SECTOR_THEME",   # thematic sector story
    "MACRO",
)


@dataclass(slots=True)
class Link:
    """A (ticker, relation) edge with an explanation the LLM agent can quote."""

    ticker: str
    relation: str
    confidence: float
    why: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "relation": self.relation,
            "confidence": round(self.confidence, 3),
            "why": self.why,
        }


# What separates title from summary from body in `RawItem.text`. It has to be a
# character no matcher can step over: term regexes join the words of a phrase
# with `\s+`, and `\s` matches a newline, so a two-word term used to be
# satisfiable by one word at the end of one field and one at the start of the
# next. "Analysts turn cautious on Nice" + "Results from the survey are due"
# matched "Nice\nResults" and reached NICE at DIRECT 0.90 - the highest
# confidence the linker can assign - on a sentence that does not exist. Google
# News RSS produces that shape all day. The newlines around it keep `.` from
# crossing too, for the scoring patterns that are compiled without DOTALL.
# NUL and not one of the ASCII separator characters: Python counts \x1c-\x1f as
# whitespace, so `\s+` steps straight over them.
FIELD_SEP = "\n\x00\n"


@dataclass(slots=True)
class RawItem:
    """A single piece of collected information, before enrichment."""

    source: str                       # source id from config/sources.yaml
    source_kind: str                  # rss | edgar_submissions | openfda | ...
    external_id: str                  # stable id within the source
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    body: str = ""
    lang: str = "en"
    # Structured extras a collector knows about: form type, CIK, NCT id, agency…
    meta: dict[str, Any] = field(default_factory=dict)
    # Tickers the *collector* already knows about (e.g. we polled TEVA's CIK).
    # The linker adds indirect links on top of these.
    seed_tickers: list[str] = field(default_factory=list)
    seed_relation: str = "DIRECT"

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=timezone.utc)
        self.published_at = self.published_at.astimezone(timezone.utc)
        self.title = _clean_text(self.title)
        self.summary = _clean_text(self.summary)

    @property
    def uid(self) -> str:
        """Stable primary key. Same source + same external id = same row."""
        return hashlib.sha1(
            f"{self.source}\x00{self.external_id}".encode("utf-8", "replace")
        ).hexdigest()

    @property
    def text(self) -> str:
        """Everything the matchers should look at, with the fields kept apart."""
        return FIELD_SEP.join((self.title, self.summary, self.body))


@dataclass(slots=True)
class ScoredItem:
    """A RawItem after entity-linking, event classification and scoring."""

    raw: RawItem
    links: list[Link]
    events: list[str]                 # event_type ids, highest-base first
    score: float                      # 0-100, for the best-scoring link
    per_ticker_score: dict[str, float]
    tier: str                         # ALERT | HIGH | NORMAL | NOISE
    reasons: list[str]                # human/LLM-readable scoring trace

    @property
    def tickers(self) -> list[str]:
        return sorted({link.ticker for link in self.links})


@dataclass(slots=True)
class PriceSnapshot:
    """Tape context used both for scoring and for the `whats_moving` view."""

    ticker: str
    # When WE fetched it. Not when the price happened - on a Saturday these are
    # two days apart, and reporting the fetch as the age of the print made a
    # Friday close read as "2 minutes old".
    asof: datetime
    # When the exchange last printed: the last trade, or the closing print of
    # the last session. This is the observation time; `asof` is the fetch time.
    # None for providers that do not say.
    market_time: datetime | None = None
    last: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    adv20: float | None = None
    volume_multiple: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    session: str = "unknown"          # premarket | regular | afterhours | closed
    # Which feed this print came from. A trader reconciling our -4.2% against
    # their broker needs to know whether they are looking at a delayed Yahoo
    # intraday quote or a Stooq end-of-day bar; the two disagree by design.
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "asof": self.asof.isoformat(),
            "market_time": self.market_time.isoformat() if self.market_time else None,
            "last": self.last,
            "prev_close": self.prev_close,
            "change_pct": round(self.change_pct, 2) if self.change_pct is not None else None,
            "volume": self.volume,
            "adv20": self.adv20,
            "volume_multiple": (
                round(self.volume_multiple, 2) if self.volume_multiple is not None else None
            ),
            "session": self.session,
            "provider": self.provider,
        }


@dataclass(slots=True)
class CalendarEntry:
    """A known future catalyst - what a trader needs to not be blindsided by."""

    ticker: str
    kind: str                         # earnings | pdufa | trial_completion | conference | auction
    date: str                         # ISO date (may be approximate)
    label: str
    source: str
    confidence: float = 0.8
    url: str = ""
    # How this date reaches this ticker. DIRECT is the company's own calendar;
    # SECTOR_REG is a date in its industry, which is not the same thing and must
    # not be offered as "the next known catalyst" for the company.
    relation: str = "DIRECT"


_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return _WS.sub(" ", _TAGS.sub(" ", value)).strip()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
