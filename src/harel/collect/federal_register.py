"""Federal Register collectors.

The Federal Register is the highest-value *free* regulatory source in the whole
system: one free, keyless, well-documented API that covers BIS export controls
(TSEM/NVMI/CAMT), FDA rules (TEVA/KMDA/CGEN/OPK/BWAY), CMS reimbursement (OPK,
BWAY), IRS clean-energy credit guidance (ORA), ITC duty rulings (ICL), FAA
airworthiness directives (TATT) and FCC spectrum actions (GILT).

Two collectors:
  ``federal_register``     the published record (06:00 ET each business day)
  ``federal_register_pi``  public inspection - the same documents ~1 day EARLY,
                           which for a short-term trader is the whole point.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

DOCS_URL = "https://www.federalregister.gov/api/v1/documents.json"
PI_URL = "https://www.federalregister.gov/api/v1/public-inspection-documents/current.json"

FIELDS = [
    "document_number", "title", "abstract", "html_url", "publication_date",
    "type", "agencies", "agency_names", "action", "docket_ids", "topics",
    "significant", "effective_on", "comments_close_on", "citation",
]

# Public inspection documents carry a *smaller* schema than published ones, and
# the API rejects the whole request rather than ignoring a field it does not
# know: asking for FIELDS here returns `400 field 'abstract' not valid`, so the
# collector yielded nothing, every pass, since it was written. The one source in
# this system that buys lead time was silently off. Eight of those fifteen names
# are invalid here (abstract, action, docket_ids, topics, significant,
# effective_on, comments_close_on, citation) - verified field by field against
# the live API on 2026-08-01.
PI_FIELDS = [
    "document_number", "title", "html_url", "publication_date", "type",
    "agencies", "agency_names", "filed_at", "filing_type", "pdf_url",
    "excerpts", "toc_subject", "toc_doc", "num_pages", "docket_numbers",
]

MAX_PER_QUERY = 40


def _as_phrase(term: str) -> str:
    """Quote multi-word terms so the API matches the phrase, not the loose words.

    ``conditions[term]`` ANDs the words and searches the *full document text*,
    not the phrase and not just the title. Unquoted, "entity list" matches any
    rule whose body happens to contain both "entity" and "list" - which for
    long regulatory documents is close to all of them. Combined with umbrella
    agency slugs (commerce-department covers NOAA) that pulled fisheries and
    marine-mammal rules in against the semiconductor names. Quoting restricts
    the match to the exact phrase, which is what a term of art like "entity
    list" or "deemed export" actually means.
    """
    term = term.strip()
    if " " not in term or term.startswith('"'):
        return term
    return f'"{term}"'


@register("federal_register")
class FederalRegisterCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        for agencies, terms, tickers, label in self._query_plan():
            for term in terms:
                try:
                    yield from self._query(agencies, term, tickers, label)
                except HttpError as exc:
                    self.warn(f"{label} / {term}: {exc}")
                except Exception as exc:
                    self.warn(f"{label} / {term}: unexpected {type(exc).__name__}: {exc}")

    def _query_plan(self) -> list[tuple[list[str], list[str], list[str], str]]:
        """One query set per sector that is actually represented in the universe."""
        by_sector: dict[str, list[str]] = {}
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if tc:
                by_sector.setdefault(tc.sector, []).append(ticker)

        # `agency_filter` lets a source narrow itself (e.g. faa_ads).
        forced = self.source.raw.get("agency_filter")

        plan = []
        for sector_key, tickers in by_sector.items():
            sector = self.cfg.sector(sector_key)
            agencies = forced or sector.fr_agencies
            if not agencies or not sector.fr_terms:
                continue
            # A source that forces its own agency (faa_ads -> FAA) must only run
            # for sectors that actually claim it as a regulator. Without this it
            # pairs the FAA with every sector's search terms, which linked
            # airworthiness directives to a medical-device maker and a
            # geothermal operator at SECTOR_REG confidence.
            if forced and self.source.key not in sector.regulators:
                continue
            plan.append((list(agencies), list(sector.fr_terms), tickers, sector_key))
        return plan

    def _query(self, agencies: list[str], term: str, tickers: list[str],
               sector_key: str) -> Iterator[RawItem]:
        phrase = _as_phrase(term)
        params: list[tuple[str, str]] = [
            ("conditions[term]", phrase),
            ("conditions[publication_date][gte]", self.ctx.since.date().isoformat()),
            ("per_page", str(MAX_PER_QUERY)),
            ("order", "newest"),
        ]
        # The agency list existed to constrain loose word-matching. Now that a
        # multi-word term is searched as an exact phrase, the phrase IS the
        # precision, and the agency filter only drops true positives published
        # by an agency the sector did not happen to list. Measured over 2026:
        # "export controls" 6 -> 24 hits, "critical infrastructure protection"
        # 1 -> 10, "zero trust architecture" 0 -> 3.
        #
        # A source that forces its own agency (faa_ads) is different: there the
        # agency is the subject of the source, not a filter, so it always binds.
        forced = bool(self.source.raw.get("agency_filter"))
        if forced or not phrase.startswith('"'):
            params += [("conditions[agencies][]", a) for a in agencies]
        params += [("fields[]", f) for f in FIELDS]

        resp = self.client.get(DOCS_URL, params=params, allow_status=(400, 404))
        if resp.status >= 400:
            self.warn(f"HTTP {resp.status} for term={term!r}")
            return

        for doc in (resp.json() or {}).get("results") or []:
            item = _doc_to_item(self, doc, tickers, sector_key, term, public_inspection=False)
            if item is not None:
                yield item

    # Which sector a query belongs to is not evidence about a document. See
    # `_doc_to_item`; the tickers travel as context and the linker decides.


@register("federal_register_pi")
class FederalRegisterPublicInspectionCollector(Collector):
    """Documents on public inspection - typically one business day ahead of
    publication. This is genuine lead time, so these score higher."""

    def collect(self) -> Iterator[RawItem]:
        agencies_of_interest: dict[str, list[str]] = {}
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            for agency in self.cfg.sector(tc.sector).fr_agencies:
                agencies_of_interest.setdefault(agency, []).append(ticker)

        if not agencies_of_interest:
            return

        params = [("fields[]", f) for f in PI_FIELDS] + [("per_page", "200")]
        try:
            resp = self.client.get(PI_URL, params=params, allow_status=(400, 404))
        except HttpError as exc:
            self.warn(str(exc))
            return
        if resp.status >= 400:
            self.warn(f"public inspection returned HTTP {resp.status}")
            return

        for doc in (resp.json() or {}).get("results") or []:
            slugs = {
                (a.get("slug") or "") for a in (doc.get("agencies") or [])
                if isinstance(a, dict)
            }
            tickers = sorted({
                t for slug in slugs for t in agencies_of_interest.get(slug, [])
            })
            if not tickers:
                continue
            item = _doc_to_item(self, doc, tickers, "public_inspection", "",
                                public_inspection=True)
            if item is not None:
                yield item


def _doc_to_item(collector: Collector, doc: dict, tickers: list[str],
                 sector_key: str, term: str, public_inspection: bool) -> RawItem | None:
    number = doc.get("document_number")
    title = doc.get("title") or ""
    if not number or not title:
        return None

    now = datetime.now(timezone.utc)
    forthcoming = False
    scheduled = doc.get("publication_date") if public_inspection else None

    if public_inspection:
        # `publication_date` on a public-inspection document is the date it is
        # *scheduled* to appear - routinely two or three days out. Reading that
        # as the publication time hands the item a future timestamp, a negative
        # age and a recency bonus it has not earned. `filed_at` is the moment it
        # became public, which is where the lead time actually starts.
        published = _parse_iso(doc.get("filed_at")) or now
        forthcoming = bool(scheduled and scheduled > now.date().isoformat())
        # No abstract in this schema; the table-of-contents pair is the closest
        # thing to one, and excerpts carries the real text when present.
        summary = doc.get("excerpts") or " - ".join(
            part for part in (doc.get("toc_subject"), doc.get("toc_doc")) if part
        )
    else:
        pub_date = doc.get("publication_date") or doc.get("filing_date") or ""
        try:
            published = datetime.strptime(pub_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            published = datetime.now(timezone.utc)
        # documents.json lists documents days BEFORE they publish, so this date
        # is regularly in the future. Stored as published_at it gave the item a
        # negative age: the recency curve read it as maximally fresh, and the
        # relative-time label - which only tested `minutes < 1` - printed a
        # Monday rule as "הרגע" on Saturday night. What is actually true is
        # that we learned of it when we collected it, and that it is scheduled.
        if published > now:
            forthcoming = True
            scheduled = pub_date
            published = now
        summary = doc.get("abstract") or ""

    agency_names = doc.get("agency_names") or [
        a.get("name") for a in (doc.get("agencies") or []) if isinstance(a, dict)
    ]

    prefix = "[FR-EARLY]" if public_inspection else "[FR]"
    return collector.make_item(
        external_id=("pi:" if public_inspection else "fr:") + str(number),
        title=f"{prefix} {title}",
        url=doc.get("html_url") or "",
        summary=(summary or "")[:4000],
        published_at=published,
        # Deliberately empty. A seed is a claim by the collector that it knows
        # who a document is about, and the linker honours it at 0.92 - but all
        # this collector knew was that the API returned the document for one of
        # a sector's search terms, and `conditions[term]` searches the FULL
        # document text. That turned a passing mention into a high-confidence
        # link to every ticker in the sector: a Family Violence Prevention rule
        # became LPSN and NICE news, a hospice wage index became BrainsWay news,
        # and both outranked real stories at 45. The tickers below travel as
        # context; `EntityLinker._link_sector_regulatory` decides, and it looks
        # only at the title and abstract - the parts that say what a document is
        # actually about.
        seed_tickers=[],
        seed_relation="SECTOR_REG",
        meta={
            "document_number": number,
            # One document, one lifecycle. The public-inspection copy and the
            # published copy carry different titles and different dates; the
            # document number is the only thing that identifies the event, so it
            # is what the deduper fingerprints on.
            "dedupe_id": f"federal_register:{number}",
            # Who the query was run for, kept for the drill-down so "why did we
            # even fetch this" stays answerable after the link is gone.
            "queried_for": list(tickers),
            "doc_type": doc.get("type"),
            "action": doc.get("action"),
            "agencies": agency_names,
            # PI calls the same thing `docket_numbers`.
            "docket_ids": doc.get("docket_ids") or doc.get("docket_numbers"),
            "topics": doc.get("topics"),
            "significant": doc.get("significant"),
            "effective_on": doc.get("effective_on"),
            "comments_close_on": doc.get("comments_close_on"),
            "matched_term": term,
            "sector": sector_key,
            "public_inspection": public_inspection,
            "lead_time": "1_business_day" if public_inspection else None,
            # `filing_type: special` means the agency put it on public
            # inspection ahead of the normal schedule - the strongest lead-time
            # signal this source has. `scheduled_publication_date` is the day it
            # will hit the Federal Register, kept out of published_at above.
            "filing_type": doc.get("filing_type") if public_inspection else None,
            "scheduled_publication_date": scheduled,
            # True while the document exists but has not published yet. The
            # surfaces must say "publishes Monday", never date it as news.
            "forthcoming": forthcoming,
            "pdf_url": doc.get("pdf_url") if public_inspection else None,
        },
    )


def _parse_iso(value: str | None) -> datetime | None:
    """PI timestamps arrive as `2026-07-30T16:15:00.000-04:00`."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
        tzinfo=timezone.utc)
