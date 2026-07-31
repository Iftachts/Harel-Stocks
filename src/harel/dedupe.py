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
from datetime import timedelta

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
    """Exact-match fingerprint: normalized title + publication day."""
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
    """In-memory clusterer for one pipeline run, seeded from the DB.

    Two items cluster together when they share a dedupe_key, or when their
    SimHashes are within `threshold` bits AND they were published within
    `window_hours` of each other.
    """

    def __init__(self, threshold: int = 6, window_hours: float = 36.0) -> None:
        self.threshold = threshold
        self.window = timedelta(hours=window_hours)
        self._by_key: dict[str, str] = {}
        self._hashes: list[tuple[int, str, object]] = []   # (simhash, cluster_id, published_at)

    def seed(self, dedupe_keys: dict[str, str]) -> None:
        """Prime with existing (dedupe_key -> cluster_id) pairs from the DB."""
        self._by_key.update(dedupe_keys)

    def assign(self, item: RawItem) -> tuple[str, str]:
        """Return (dedupe_key, cluster_id) for an item."""
        key = dedupe_key(item)
        if key in self._by_key:
            return key, self._by_key[key]

        sh = simhash(item.title)
        if sh:
            for other_hash, cluster_id, published in self._hashes:
                if abs(item.published_at - published) > self.window:
                    continue
                if hamming(sh, other_hash) <= self.threshold:
                    self._by_key[key] = cluster_id
                    self._hashes.append((sh, cluster_id, item.published_at))
                    return key, cluster_id

        cluster_id = key[:16]
        self._by_key[key] = cluster_id
        if sh:
            self._hashes.append((sh, cluster_id, item.published_at))
        return key, cluster_id
