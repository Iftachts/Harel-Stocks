"""Entity linking - the part that produces "indirect" coverage.

Given one collected item, decide which of our 22 names it touches and *how*.
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

# Relations that carry evidence about the *company*: the document named it, or
# named a product, subsidiary, peer, customer or supplier we track for it.
# Everything below this line is evidence about a group.
CAUSAL_RELATIONS = frozenset({
    "DIRECT", "SUBSIDIARY", "PRODUCT_RIVAL", "CUSTOMER", "PEER", "SUPPLIER",
})


def causal_eligible(relation: str | None) -> bool:
    """May a link of this kind be offered as the *cause* of a price move?

    Relevance and causation are different questions, and one number was
    answering both. A UFLPA entity-list notice is genuinely relevant to a
    foundry - and on 2026-07-31 it was offered as the reason TSEM rose 4.0% and,
    in the same session, as the reason CAMT fell 3.5%, while SOXX barely moved.
    A document that explains a move and its opposite explains neither.

    So a sector-level match stays in the feed - suppressing it would be its own
    failure, the entity list does matter to a foundry - but it cannot be a
    driver on a keyword alone. What lifts it is evidence: the company named, an
    entity we track for it named, or (checked at read time, not here) the whole
    basket moving together in the direction the document would predict.
    """
    return (relation or "").upper() in CAUSAL_RELATIONS

# Symbols that are also ordinary English words. Matching these bare would flood
# the feed, so they need an exchange prefix or a $ sigil.
AMBIGUOUS_TICKERS = {
    "ICL", "ORA", "KEN", "NICE", "ALL", "ONE", "ARE", "IT", "OPK", "TAT",
    # A gilt is a UK government bond, and the bond wires mention it constantly.
    "GILT",
}

# Company NAMES that are also ordinary English words. Same failure as an
# ambiguous symbol, one level up - and worse, because a name match carries
# higher confidence than a symbol match.
AMBIGUOUS_NAMES = {
    "allot",    # a verb: "PH, US allot P42b for anti-TB drive" -> Allot, DIRECT
    "nice",     # an adjective, and a French city
    "nova",     # a star, a region, a hundred product names
    "orbit",
    "one",
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
            # A company name that is also an ordinary word needs corporate
            # context. "PH, US allot P42b for anti-TB, HIV drive" was linked
            # DIRECT to Allot Communications at confidence 0.97, because "allot"
            # is a verb. Same failure mode as the ambiguous symbols below, one
            # level up: the string is right and the entity is not.
            if name.lower() in AMBIGUOUS_NAMES:
                pattern = _word_re_with_context(name)
                confidence = 0.9
            else:
                pattern = _word_re(name)
                confidence = 0.88
            self.rules.append(_Rule(
                pattern=pattern, ticker=t, relation="DIRECT",
                why=f'names "{name}"', base_confidence=confidence,
                title_only_bonus=0.09, probe=_probe(name),
            ))

        # 2. The symbol. An ambiguous symbol needs context - an exchange prefix,
        # a $ sigil, or a corporate verb / financial noun immediately after it.
        # Prefix-or-sigil alone was too strict: "NICE Price Target Cut to
        # $111.00 by Morgan Stanley" is unmistakably about NICE and carried
        # neither.
        if t in AMBIGUOUS_TICKERS:
            pattern = _word_re_with_context(t)
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

        # 5. Named competitors. Same ordinary-word trap as our own names: with a
        # bare match, "Nova Scotia announces an energy plan" is a Camtek peer
        # story and "a nice day in Nice" is a LivePerson one.
        for name in tc.peer_names:
            if len(name) >= 4:
                ambiguous = name.lower() in AMBIGUOUS_NAMES
                self.rules.append(_Rule(
                    pattern=(_word_re_with_context(name) if ambiguous
                             else _word_re(name)),
                    ticker=t, relation="PEER",
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
        #    `peer_events_that_matter` mixes two kinds of entity: genuine demand
        #    drivers (TSMC capex -> CAMT) and competitors whose results de-rate
        #    the group (CrowdStrike guidance -> PANW). Only the first kind is a
        #    CUSTOMER; naming a competitor here must not outrank its PEER link,
        #    because CUSTOMER sits above PEER in the precedence order.
        peer_name_set = {name.strip().lower() for name in tc.peer_names}
        for driver in tc.peer_events_that_matter:
            for head in _entity_candidates(driver):
                if head.strip().lower() in peer_name_set:
                    continue
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
        # A collector that knows *why* it seeded a ticker says so in
        # `meta.seed_why`. "collected from google_news as product_rival" is not
        # something a trader can check; "the story names 'NRG Energy', a rival we
        # track for KEN" is.
        seed_why = str(item.meta.get("seed_why") or "").strip()
        for ticker in item.seed_tickers:
            if ticker not in explicit:
                offer(ticker, item.seed_relation, 0.92,
                      seed_why or
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


_DIRECT_EVIDENCE_CACHE: dict[str, re.Pattern[str]] = {}


def direct_evidence(tc, text: str) -> bool:
    """Does this text actually name the company, by name, alias or symbol?

    Google News answers a query loosely, so the per-ticker search returns
    stories that mention nobody: "PH, US allot P42b for anti-TB, HIV drive" came
    back from the Allot query and was tagged DIRECT at 0.92 on the strength of
    the query alone. The same ambiguity rules as the linker apply here - an
    ordinary-word name needs corporate context - so this asks exactly the
    question the linker would ask, before the seed is trusted.
    """
    pattern = _DIRECT_EVIDENCE_CACHE.get(tc.ticker)
    if pattern is None:
        parts = []
        for name in tc.match_names:
            if name == tc.ticker or len(name) < 4:
                continue
            builder = (_word_re_with_context if name.lower() in AMBIGUOUS_NAMES
                       else _word_re)
            parts.append(builder(name).pattern)
        parts.append(_word_re_with_context(tc.ticker).pattern
                     if tc.ticker in AMBIGUOUS_TICKERS
                     else rf"(?<![\w.]){re.escape(tc.ticker)}(?!\w)")
        pattern = re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)
        _DIRECT_EVIDENCE_CACHE[tc.ticker] = pattern
    return bool(pattern.search(text or ""))


def _word_re(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive, punctuation-tolerant match."""
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


# What an ordinary word has to be doing to read as a company.
#
# Requiring a corporate suffix was too strict and withdrew real stories:
# "Allot to Release Second Quarter 2026 Results", "NICE Price Target Cut to
# $111.00 by Morgan Stanley", "Nova slides as semiconductor selloff outweighs
# momentum". Headlines drop the "Ltd". What actually separates those from
# "Supreme Court directs the state to allot adjacent land" is the word that
# FOLLOWS - a corporate verb or a financial noun - and the word before, since
# "to allot" is an infinitive and never a company.
_CORP_SUFFIX = (r"Ltd|Ltd\.|Limited|Inc|Inc\.|Corp|Corp\.|Communications|"
                r"Technologies|Systems|Networks|Pharmaceutical[s]?|Industries|"
                r"Holdings|Group|Energy|Semiconductor[s]?|plc|N\.V\.|S\.A\.")
_CORP_VERB = (r"to\s+(?:release|report|announce|host|acquire|launch|present|hold|"
              r"buy|sell|merge|invest|expand)|announce[sd]?|report[sd]?|posts?|"
              r"posted|beats?|misses?|raises?|lowers?|cuts?|slides?|slid|jumps?|"
              r"gains?|falls?|fell|rises?|rose|soars?|plunges?|drops?|climbs?|"
              r"wins?|won|secures?|signs?|signed|expands?|names?|named|appoints?|"
              r"completes?|completed|launches?|launched|acquires?|acquired|"
              r"receives?|received|is\s+up|is\s+down|was\s+up|was\s+down")
_FIN_NOUN = (r"stock|shares?|share\s+price|price\s+target|earnings|revenue[s]?|"
             r"guidance|results|outlook|dividend|buyback|CEO|CFO|board|"
             r"Q[1-4]|first|second|third|fourth|FY\d{2,4}|investors?|analysts?")
# "to allot", "will allot", "should allot" - an infinitive is never a company.
_NOT_A_COMPANY_BEFORE = r"(?<!\bto\s)(?<!\bwill\s)(?<!\bshall\s)(?<!\bmust\s)(?<!\bmay\s)"


def _word_re_with_context(term: str) -> re.Pattern[str]:
    """Match an ordinary-word company name only where it reads as a company."""
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.compile(
        # NASDAQ: ALLT / $ALLT - unambiguous on its own.
        rf"(?:(?:{_TICKER_CONTEXT}|\$)\s*{escaped}(?!\w))"
        # Allot Ltd / Allot Communications
        rf"|(?:(?<!\w){escaped}\s+(?:{_CORP_SUFFIX})(?!\w))"
        # Allot to Release… / Nova slides… / Allot's results
        rf"|(?:{_NOT_A_COMPANY_BEFORE}(?<!\w){escaped}(?:'s|’s)?\s+"
        rf"(?:{_CORP_VERB}|{_FIN_NOUN})(?!\w))",
        re.IGNORECASE,
    )


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
