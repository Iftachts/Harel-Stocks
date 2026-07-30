"""Entity linking - the part that produces "indirect" coverage.

Given one collected item, decide which of our 21 names it touches and *how*.
The `how` is the whole point: a trader reads a TIGIT readout from Roche very
differently from a Compugen press release, and the LLM agent downstream needs
that distinction to write a useful note.

Precedence (strongest wins per ticker):
    DIRECT > SUBSIDIARY > PRODUCT_RIVAL > CUSTOMER > PEER > SUPPLIER
    > SECTOR_REG > SECTOR_THEME > MACRO
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Config, TickerConfig
from ..models import Link, RawItem

RELATION_RANK = {
    "DIRECT": 90,
    "SUBSIDIARY": 80,
    "PRODUCT_RIVAL": 70,
    "CUSTOMER": 60,
    "PEER": 50,
    "SUPPLIER": 40,
    "SECTOR_REG": 30,
    "SECTOR_THEME": 20,
    "MACRO": 10,
}

# Symbols that are also ordinary English words. Matching these bare would flood
# the feed, so they need an exchange prefix or a $ sigil.
AMBIGUOUS_TICKERS = {
    "ICL", "ORA", "KEN", "NICE", "ALL", "ONE", "ARE", "IT", "OPK", "TAT",
}

_TICKER_CONTEXT = r"(?:NASDAQ|NYSE|NYSE American|TASE|TLV|Nasdaq|Nyse)\s*[:\-]?\s*"


@dataclass(slots=True)
class _Rule:
    pattern: re.Pattern[str]
    ticker: str
    relation: str
    why: str
    base_confidence: float
    title_only_bonus: float = 0.0
    # Cheap literal prefilter: the longest word of the term, lowercased. A plain
    # `in` test against the lowercased item text is a C-level substring search
    # and skips ~95% of regex executions. There are ~1,000 rules and the
    # pipeline runs every couple of minutes, so this matters.
    probe: str = ""


class EntityLinker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.rules: list[_Rule] = []
        self._build()

    # -- rule construction -------------------------------------------------- #
    def _build(self) -> None:
        for ticker in self.config.active_tickers:
            tc = self.config.ticker(ticker)
            if tc:
                self._build_for_ticker(tc)

    def _build_for_ticker(self, tc: TickerConfig) -> None:
        t = tc.ticker

        # 1. The company itself, by name and alias.
        for name in tc.match_names:
            if name == t:
                continue
            if len(name) < 4:
                continue
            self.rules.append(_Rule(
                pattern=_word_re(name), ticker=t, relation="DIRECT",
                why=f'names "{name}"', base_confidence=0.88, title_only_bonus=0.09,
                probe=_probe(name),
            ))

        # 2. The symbol. Ambiguous symbols need an exchange prefix or a $ sigil.
        if t in AMBIGUOUS_TICKERS:
            pattern = re.compile(rf"(?:{_TICKER_CONTEXT}|\$){re.escape(t)}(?!\w)")
            confidence = 0.9
        else:
            pattern = re.compile(rf"(?<![\w.]){re.escape(t)}(?!\w)")
            confidence = 0.8
        self.rules.append(_Rule(
            pattern=pattern, ticker=t, relation="DIRECT",
            why=f"symbol {t}", base_confidence=confidence, title_only_bonus=0.08,
            probe=t.lower(),
        ))

        # 3. Our own products - a story about AUSTEDO is a story about Teva even
        #    if Teva is never named.
        for term in tc.product_terms:
            if len(term) >= 5:
                self.rules.append(_Rule(
                    pattern=_word_re(term), ticker=t, relation="DIRECT",
                    why=f'our product "{term}"', base_confidence=0.82,
                    probe=_probe(term),
                ))

        # 4. Competing products / same-mechanism programs.
        for term in tc.competitor_products:
            if len(term) >= 5:
                self.rules.append(_Rule(
                    pattern=_word_re(term), ticker=t, relation="PRODUCT_RIVAL",
                    why=f'rival program "{term}"', base_confidence=0.78,
                    probe=_probe(term),
                ))

        # 5. Named competitors.
        for name in tc.peer_names:
            if len(name) >= 4:
                self.rules.append(_Rule(
                    pattern=_word_re(name), ticker=t, relation="PEER",
                    why=f'competitor "{name}"', base_confidence=0.7,
                    probe=_probe(name),
                ))

        # 6. Peer tickers (only unambiguous ones).
        for peer in tc.peers:
            symbol = peer.split(".")[0]
            if len(symbol) >= 3 and symbol.upper() not in AMBIGUOUS_TICKERS:
                self.rules.append(_Rule(
                    pattern=re.compile(rf"(?<![\w.]){re.escape(symbol)}(?!\w)"),
                    ticker=t, relation="PEER", why=f"peer symbol {symbol}",
                    base_confidence=0.55, probe=symbol.lower(),
                ))

        # 7. Customers / demand drivers whose capex is our revenue.
        for driver in tc.peer_events_that_matter:
            for head in _entity_candidates(driver):
                self.rules.append(_Rule(
                    pattern=_word_re(head), ticker=t, relation="CUSTOMER",
                    why=f'demand driver "{head}" ({driver})', base_confidence=0.6,
                    probe=_probe(head),
                ))

        # 8. Sector / company themes.
        for theme in tc.themes:
            if len(theme) >= 6:
                self.rules.append(_Rule(
                    pattern=_word_re(theme), ticker=t, relation="SECTOR_THEME",
                    why=f'theme "{theme}"', base_confidence=0.5,
                    probe=_probe(theme),
                ))

        # 9. Named single points of failure (Microsoft for PERI/AUDC, etc).
        for spof in tc.single_points_of_failure:
            head = spof.split()[0]
            if len(head) >= 4:
                self.rules.append(_Rule(
                    pattern=_word_re(head), ticker=t, relation="SECTOR_THEME",
                    why=f'dependency "{spof}"', base_confidence=0.55,
                    probe=_probe(head),
                ))

    # -- linking ------------------------------------------------------------ #
    def link(self, item: RawItem) -> list[Link]:
        best: dict[str, Link] = {}
        # Some collectors synthesise a headline containing our company name
        # (e.g. "Viatris mentions Teva"). For those the collector's relation is
        # authoritative and text matching must not be allowed to upgrade it.
        locked: set[str] = (
            {t.upper() for t in item.seed_tickers}
            if item.meta.get("lock_seed_relation") else set()
        )

        def offer(ticker: str, relation: str, confidence: float, why: str) -> None:
            ticker = ticker.upper()
            if ticker not in self.config.universe:
                return
            tc = self.config.universe[ticker]
            if not tc.enabled or tc.unresolved:
                return
            current = best.get(ticker)
            if current is not None and ticker in locked:
                return
            if current is None:
                best[ticker] = Link(ticker, relation, min(confidence, 0.99), why)
                return
            if RELATION_RANK.get(relation, 0) > RELATION_RANK.get(current.relation, 0):
                best[ticker] = Link(ticker, relation, min(confidence, 0.99), why)
            elif relation == current.relation and confidence > current.confidence:
                best[ticker] = Link(ticker, relation, min(confidence, 0.99),
                                    f"{current.why}; {why}")

        # (a) The collector already knows something - trust it most.
        explicit = item.meta.get("relations") or {}
        for ticker, relation in explicit.items():
            offer(ticker, relation, 0.95, f"{item.source} matched entity directly")
        for ticker in item.seed_tickers:
            if ticker not in explicit:
                offer(ticker, item.seed_relation, 0.92,
                      f"collected from {item.source} as {item.seed_relation.lower()}")

        # Synthetic items (tape alerts) are *about* exactly one ticker. Their
        # headline contains that symbol, which would otherwise link them to
        # every peer that lists it - "NVMI up 10%" is not news about Camtek.
        if item.meta.get("synthetic"):
            return sorted(
                best.values(),
                key=lambda link: (-RELATION_RANK.get(link.relation, 0), -link.confidence),
            )

        # (b) Text matching over the whole item.
        title = item.title or ""
        body = item.text
        title_lower = title.lower()
        body_lower = body.lower()
        for rule in self.rules:
            if rule.probe and rule.probe not in body_lower:
                continue        # cheap literal prefilter, see _Rule.probe
            in_title = (
                (not rule.probe or rule.probe in title_lower)
                and bool(rule.pattern.search(title))
            )
            if not in_title and not rule.pattern.search(body):
                continue
            confidence = rule.base_confidence + (rule.title_only_bonus if in_title else 0.0)
            where = "headline" if in_title else "body"
            offer(rule.ticker, rule.relation, confidence, f"{rule.why} in {where}")

        # (c) Sector-wide regulatory read-across.
        self._link_sector_regulatory(item, offer)

        return sorted(
            best.values(),
            key=lambda link: (-RELATION_RANK.get(link.relation, 0), -link.confidence),
        )

    def _link_sector_regulatory(self, item: RawItem, offer) -> None:
        """A regulator document that names a sector term touches every ticker in
        that sector, even when no company is named."""
        source = self.config.sources.get(item.source)
        if source is None or source.kind not in (
            "federal_register", "federal_register_pi", "openfda", "html_table"
        ):
            return

        text = item.text.lower()
        for sector_key, sector in self.config.sectors.items():
            tickers = [
                t for t in self.config.active_tickers
                if self.config.ticker(t) and self.config.ticker(t).sector == sector_key
            ]
            if not tickers:
                continue
            matched = [term for term in sector.fr_terms if term.lower() in text]
            if not matched:
                continue
            for ticker in tickers:
                offer(ticker, "SECTOR_REG", 0.62,
                      f'{sector.label}: regulator document mentions "{matched[0]}"')


def _probe(term: str) -> str:
    """Longest alphanumeric word of a term, lowercased - the literal that must be
    present for the full regex to have any chance of matching."""
    words = re.findall(r"[\w\u0590-\u05ff]+", term)
    return max(words, key=len).lower() if words else ""


def _word_re(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive, punctuation-tolerant match."""
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


# Capitalised words that are not company names and would over-match.
_GENERIC_HEADS = {
    "Applied", "Advanced", "China", "Chinese", "Europe", "European", "New",
    "Large", "Major", "Record", "Global", "National", "Israeli", "US", "The",
    "Data", "AI", "Memory", "Foundry", "Defense",
}


def _entity_candidates(phrase: str) -> list[str]:
    """Pull the company name(s) out of a demand-driver phrase.

    "TSMC capex guidance"           -> ["TSMC"]
    "SK Hynix HBM capacity"         -> ["SK Hynix"]
    "Applied Materials guidance"    -> ["Applied Materials"]
    "TSMC CoWoS capacity expansion" -> ["TSMC", "TSMC CoWoS"]
    """
    words = phrase.strip().split()
    head: list[str] = []
    for word in words:
        if word[:1].isupper():
            head.append(word)
        else:
            break
    if not head:
        return []

    candidates: list[str] = []
    first = head[0]
    if len(first) >= 4 and first not in _GENERIC_HEADS:
        candidates.append(first)
    if len(head) >= 2:
        pair = " ".join(head[:2])
        if len(pair) >= 6:
            candidates.append(pair)
    return candidates
