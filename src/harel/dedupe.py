"""Deduplication and cross-source clustering.

The same event arrives many times: the issuer's own PR, GlobeNewswire, the 8-K,
Google News, Globes in Hebrew. For a trader that is not five stories - it is one
story with five confirmations, and the confirmation count is itself signal.

Strategy:
  1. `dedupe_key` - a normalized-title + day fingerprint. Exact collisions are
     the same story with near-certainty.
  2. SimHash over token shingles - catches rewritten headlines and translations
     of the same wire copy within a time window.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from .models import RawItem

_PUNCT = re.compile(r"[^\w\s֐-׿]+", re.UNICODE)
_WS = re.compile(r"\s+")

# Google News appends " - <publisher>" to every headline, and the same wire copy
# is syndicated to regional mirrors, so one story arrived as
# "…Here's What to Expect - Yahoo Finance" and again as
# "…Here's What to Expect - Yahoo Finance Singapore" - two fingerprints, two
# rows, and a corroboration count of two for a single article.
_PUBLISHER_SUFFIX = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,45}$")

# Wire boilerplate that shifts the fingerprint without changing the story.
_BOILERPLATE = re.compile(
    r"\b(globe ?newswire|business ?wire|pr ?newswire|globe|newswire|accesswire|"
    r"reuters|bloomberg|nasdaq|nyse|tase|inc\.?|ltd\.?|corp\.?|plc|s\.a\.|n\.v\.|"
    r"announces?|announced|reports?|reported|said|says|update[sd]?)\b",
    re.IGNORECASE,
)

_STOP = {
    "the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "at", "by",
    "its", "it", "as", "from", "that", "this", "is", "are", "was", "were",
    "של", "את", "עם", "על", "אל", "כי", "הוא", "היא",
}


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = _PUBLISHER_SUFFIX.sub("", text)
    text = _BOILERPLATE.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _WS.split(text) if t and t not in _STOP and len(t) > 1]
    return " ".join(tokens)


def dedupe_key(item: RawItem) -> str:
    """Exact-match fingerprint: normalized title + publication day.

    Unless the source hands us a stable identifier for the underlying event, in
    which case that identifier is the fingerprint and nothing else needs to
    agree. The Federal Register publishes the same document twice - once on
    public inspection, days early, and once on publication - under one document
    number, with a different title prefix and a different date. Title-and-day
    made those two stories. They are one document with one lifecycle, and the
    number says so.
    """
    stable = str((item.meta or {}).get("dedupe_id") or "").strip()
    if stable:
        return hashlib.sha1(f"id|{stable}".encode("utf-8")).hexdigest()
    norm = normalize_title(item.title)
    day = item.published_at.date().isoformat()
    return hashlib.sha1(f"{norm}|{day}".encode("utf-8")).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    """64-bit SimHash over word bigrams."""
    tokens = normalize_title(text).split()
    if not tokens:
        return 0
    shingles = (
        [" ".join(tokens[i : i + 2]) for i in range(len(tokens) - 1)]
        if len(tokens) > 1
        else tokens
    )
    vector = [0] * bits
    for shingle in shingles:
        h = int.from_bytes(hashlib.md5(shingle.encode("utf-8")).digest()[:8], "big")
        for bit in range(bits):
            vector[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(bits):
        if vector[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class Clusterer:
    """Clusters one story across sources, and across runs.

    Two items cluster together when they share a dedupe_key, or when their
    SimHashes are within `threshold` bits AND they were published within
    `window_hours` of each other AND they name a company in common.

    That last condition is not decoration. Loosening the near-duplicate match to
    span runs widens the window in which two unrelated documents can collide on
    a generic headline, and on the live corpus exactly two pairs did: an Evogene
    exhibit mentioning Kamada against the same exhibit mentioning Compugen, and
    two different FERC dockets both titled "Combined Notice of Filings #1". Both
    would have merged on the title alone. All twenty-five genuine merges in the
    same corpus - one Teleflex approval carried by three publishers, one OPKO
    result carried twice - share a ticker, so the guard costs nothing real.
    """

    def __init__(self, threshold: int = 6, window_hours: float = 36.0) -> None:
        self.threshold = threshold
        self.window = timedelta(hours=window_hours)
        self._by_key: dict[str, str] = {}
        # (simhash, cluster_id, published_at, tickers)
        self._hashes: list[tuple[int, str, Any, frozenset[str]]] = []

    def seed(self, rows: Iterable[dict[str, Any]]) -> int:
        """Prime from what is already stored, so a story survives a run boundary.

        The near-duplicate half of this clusterer used to live and die inside one
        pass: `_by_key` was rebuilt empty every time, so a wire copy that arrived
        in the NEXT poll opened its own cluster instead of joining the story. The
        exact-title half always persisted by accident, because `cluster_id` is
        derived from the key - which is why this went unnoticed. Corroboration
        counts feed the score, so a second source landing an hour later was being
        scored as though it had never arrived.

        SimHash is recomputed from the stored title rather than read from a
        column. It is a pure function of the title, and recomputing means a
        change to `normalize_title` or `simhash` takes effect everywhere at once
        instead of silently mixing two algorithms in one comparison.
        """
        seeded = 0
        for row in rows:
            key, cluster_id = row.get("dedupe_key"), row.get("cluster_id")
            if not key or not cluster_id:
                continue
            self._by_key.setdefault(key, cluster_id)
            sh = simhash(row.get("title") or "")
            published = row.get("published_at")
            if sh and published is not None:
                self._hashes.append(
                    (sh, cluster_id, published, frozenset(row.get("tickers") or ())))
            seeded += 1
        return seeded

    def assign(self, item: RawItem,
               tickers: Iterable[str] = ()) -> tuple[str, str]:
        """Return (dedupe_key, cluster_id) for an item."""
        key = dedupe_key(item)
        if key in self._by_key:
            return key, self._by_key[key]

        mine = frozenset(tickers)
        sh = simhash(item.title)
        if sh:
            for other_hash, cluster_id, published, theirs in self._hashes:
                if abs(item.published_at - published) > self.window:
                    continue
                if hamming(sh, other_hash) > self.threshold:
                    continue
                # A shared name is what makes two similar headlines the same
                # event rather than the same template.
                if not (mine & theirs):
                    continue
                self._by_key[key] = cluster_id
                self._hashes.append((sh, cluster_id, item.published_at, mine))
                return key, cluster_id

        cluster_id = key[:16]
        self._by_key[key] = cluster_id
        if sh:
            self._hashes.append((sh, cluster_id, item.published_at, mine))
        return key, cluster_id
