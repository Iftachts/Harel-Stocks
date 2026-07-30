"""TASE / MAYA collector - Israeli immediate reports.

This is the biggest structural edge available for this particular basket. 18 of
the 21 resolved names are dual-listed in Tel Aviv, and Israeli issuers file
"immediate reports" (דיווח מיידי) to MAYA during Israeli trading hours - which
is the middle of the night in New York. A MAYA report at 10:00 Israel time is
information you can act on at the 09:30 ET open, hours before it reaches the US
tape.

Honesty about the endpoint
--------------------------
TASE's documented, supported route is the paid/registered API at
openapi.tase.co.il (set ``TASE_API_KEY``). The unauthenticated mayaapi.tase.co.il
endpoints that the site's own front-end uses are undocumented and can change
without notice. This collector therefore:

* prefers the official API when a key is present,
* falls back to the public endpoints from sources.yaml,
* parses responses **structurally rather than by fixed schema**, so a field
  rename degrades to "records found, fields unmapped" instead of a crash,
* records what it saw in ``source_state`` so ``harel doctor`` can show it.

Run ``harel probe-maya`` after the first deployment to confirm the shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

# Field-name candidates, in preference order. MAYA has used several spellings.
TITLE_KEYS = ("subject", "Subject", "title", "Title", "headline", "reportName",
              "SubjectHeb", "subjectHeb", "name", "Name")
DATE_KEYS = ("pubDate", "PubDate", "publicationDate", "PublicationDate", "date",
             "Date", "reportDate", "ReportDate", "publishDate")
URL_KEYS = ("url", "Url", "link", "Link", "pdfUrl", "PdfUrl", "reportUrl")
ID_KEYS = ("mayaReportId", "MayaReportId", "reportId", "ReportId", "id", "Id",
           "eventId", "EventId")


@register("maya")
class MayaCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        dual_listed = [
            t for t in self.active_tickers
            if self.cfg.ticker(t) and self.cfg.ticker(t).tase_id
        ]
        if not dual_listed:
            self.warn("no tase_id set for any ticker - MAYA collector idle")
            return

        for ticker in dual_listed:
            tc = self.cfg.ticker(ticker)
            try:
                yield from self._collect_company(ticker, str(tc.tase_id))
            except HttpError as exc:
                self.warn(
                    f"{ticker} (TASE {tc.tase_id}): {exc} - if this persists the "
                    f"MAYA endpoint shape has changed; run `harel probe-maya`"
                )
            except Exception as exc:
                self.warn(f"{ticker}: unexpected {type(exc).__name__}: {exc}")

    # -- fetching ---------------------------------------------------------- #
    def _collect_company(self, ticker: str, tase_id: str) -> Iterator[RawItem]:
        url, headers = self._build_request(tase_id)
        resp = self.client.get(url, headers=headers, allow_status=(400, 401, 403, 404, 500))
        if resp.status >= 400:
            self.warn(f"{ticker}: MAYA returned HTTP {resp.status}")
            return

        try:
            payload = resp.json()
        except Exception:
            self.warn(f"{ticker}: MAYA response was not JSON")
            return

        records = _find_records(payload)
        if not records:
            self.save_state(last_error=f"{ticker}: 0 parseable records")
            return

        emitted = 0
        for rec in records:
            item = self._record_to_item(rec, ticker, tase_id)
            if item is None or item.published_at < self.ctx.since:
                continue
            emitted += 1
            yield item

        if emitted == 0 and records:
            self.warn(
                f"{ticker}: MAYA returned {len(records)} records but none had a "
                f"parseable date - field names may have changed"
            )

    def _build_request(self, tase_id: str) -> tuple[str, dict[str, str]]:
        raw = self.source.raw
        headers = dict(raw.get("headers") or {})
        key = self.source.api_key

        frm = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        to = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        if key:
            base = raw.get("official_api_base", "https://openapi.tase.co.il/tase/prod")
            headers["apikey"] = key
            headers.setdefault("Accept", "application/json")
            path = raw.get("official_endpoint",
                           "/api/v1/maya-reports-online/company-disclosures")
            url = f"{base.rstrip('/')}{path}?companyId={tase_id}&fromDate={frm}&toDate={to}"
            return url, headers

        base = raw.get("base_url", "https://mayaapi.tase.co.il").rstrip("/")
        template = (raw.get("endpoints") or {}).get(
            "company_reports",
            "/api/report/company?companyId={tase_id}&fromDate={from}&toDate={to}",
        )
        path = (
            template.replace("{tase_id}", tase_id)
            .replace("{from}", frm)
            .replace("{to}", to)
        )
        return base + path, headers

    # -- shaping ----------------------------------------------------------- #
    def _record_to_item(self, rec: dict[str, Any], ticker: str,
                        tase_id: str) -> RawItem | None:
        title = _first(rec, TITLE_KEYS)
        if not title:
            return None
        published = _parse_maya_date(_first(rec, DATE_KEYS))
        if published is None:
            return None

        url = _first(rec, URL_KEYS) or f"https://maya.tase.co.il/reports/company/{tase_id}"
        if url and url.startswith("/"):
            url = "https://mayaapi.tase.co.il" + url
        ident = _first(rec, ID_KEYS) or f"{tase_id}:{published.isoformat()}:{title[:40]}"

        return self.make_item(
            external_id=f"maya:{ident}",
            title=f"[MAYA] {title}",
            url=url,
            summary=_summarize(rec),
            published_at=published,
            lang="he",
            seed_tickers=[ticker],
            seed_relation="DIRECT",
            meta={
                "venue": "TASE",
                "tase_id": tase_id,
                "israeli_hours": True,
                "form_type": rec.get("reportType") or rec.get("ReportType"),
                "is_priority": bool(rec.get("isPriority") or rec.get("IsPriority")),
                "raw_keys": sorted(rec)[:25],
            },
        )


# --------------------------------------------------------------------------- #
def _find_records(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Locate the list of report records anywhere in an unknown JSON shape."""
    if depth > 4:
        return []
    if isinstance(payload, list):
        dicts = [p for p in payload if isinstance(p, dict)]
        if dicts and any(_first(d, TITLE_KEYS) for d in dicts[:5]):
            return dicts
        best: list[dict[str, Any]] = []
        for entry in payload[:20]:
            found = _find_records(entry, depth + 1)
            if len(found) > len(best):
                best = found
        return best
    if isinstance(payload, dict):
        best = []
        for value in payload.values():
            found = _find_records(value, depth + 1)
            if len(found) > len(best):
                best = found
        return best
    return []


def _first(rec: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _summarize(rec: dict[str, Any]) -> str:
    bits = []
    for key in ("subjectEng", "SubjectEng", "description", "Description",
                "reportType", "ReportType", "formType", "FormType"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            bits.append(f"{key}: {value.strip()}")
    return " | ".join(bits)[:1500]


def _parse_maya_date(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            dt = datetime.strptime(value.split("+")[0], fmt)
            # MAYA timestamps are Israel local time (UTC+2/+3).
            return (dt - timedelta(hours=_israel_offset(dt))).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def _israel_offset(dt: datetime) -> int:
    """Israel DST runs from the Friday before the last Sunday of March to the
    last Sunday of October. Approximated by month; the one-hour error at the
    boundary is immaterial for news ordering."""
    return 3 if 4 <= dt.month <= 10 else 2
