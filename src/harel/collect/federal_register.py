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

MAX_PER_QUERY = 40


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
            plan.append((list(agencies), list(sector.fr_terms), tickers, sector_key))
        return plan

    def _query(self, agencies: list[str], term: str, tickers: list[str],
               sector_key: str) -> Iterator[RawItem]:
        params: list[tuple[str, str]] = [
            ("conditions[term]", term),
            ("conditions[publication_date][gte]", self.ctx.since.date().isoformat()),
            ("per_page", str(MAX_PER_QUERY)),
            ("order", "newest"),
        ]
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

        params = [("fields[]", f) for f in FIELDS] + [("per_page", "200")]
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

    pub_date = doc.get("publication_date") or doc.get("filing_date") or ""
    try:
        published = datetime.strptime(pub_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        published = datetime.now(timezone.utc)

    agency_names = doc.get("agency_names") or [
        a.get("name") for a in (doc.get("agencies") or []) if isinstance(a, dict)
    ]

    prefix = "[FR-EARLY]" if public_inspection else "[FR]"
    return collector.make_item(
        external_id=("pi:" if public_inspection else "fr:") + str(number),
        title=f"{prefix} {title}",
        url=doc.get("html_url") or "",
        summary=(doc.get("abstract") or "")[:4000],
        published_at=published,
        seed_tickers=list(tickers),
        seed_relation="SECTOR_REG",
        meta={
            "document_number": number,
            "doc_type": doc.get("type"),
            "action": doc.get("action"),
            "agencies": agency_names,
            "docket_ids": doc.get("docket_ids"),
            "topics": doc.get("topics"),
            "significant": doc.get("significant"),
            "effective_on": doc.get("effective_on"),
            "comments_close_on": doc.get("comments_close_on"),
            "matched_term": term,
            "sector": sector_key,
            "public_inspection": public_inspection,
            "lead_time": "1_business_day" if public_inspection else None,
        },
    )
