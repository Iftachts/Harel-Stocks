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

import re
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

# Official API - product "Market Announcements feed - MAYA 2.0.0".
# Per its OpenAPI spec the server is datawise.tase.co.il and the security scheme
# is a plain `apiKey` header. openapi.tase.co.il is only the developer portal.
OFFICIAL_BASE = "https://datawise.tase.co.il"
DISCLOSURES_PATH = "/api/v2/market-announcements/companies-disclosures/by-issuer"
SCHEDULE_PATH = "/api/v2/market-announcements/financial-report-schedule/by-schedule-date"
REPORT_TYPES_PATH = "/api/v2/market-announcements/financial-report-schedule/event-types"
PERIOD_TYPES_PATH = "/api/v2/market-announcements/financial-report-schedule/period-types"

# Schedule rows are calendar data, not headlines. This form_type carries a hard
# noise cap in scoring.yaml so they stay out of the feed while still being
# collected - the value is the calendar row, not a story.
SCHEDULE_FORM_TYPE = "MAYA-SCHEDULE"

# Trailing "+02:00" / "-0500" - i.e. the stamp already states its zone.
_UTC_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}$")


@register("maya")
class MayaCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        official = bool(self.source.api_key)
        targets: list[tuple[str, Any, Any]] = []
        missing_issuer: list[str] = []

        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if tc is None:
                continue
            issuer_id = tc.raw.get("tase_issuer_id")
            if official:
                # v2 keys on issuer number, and a name can be addressable by
                # issuer with no security id recorded (PANW, LPSN). Sending
                # tase_id here would query an unrelated issuer - or nothing -
                # and read exactly like "no news", so skip loudly instead.
                if issuer_id:
                    targets.append((ticker, tc, issuer_id))
                elif tc.tase_id:
                    missing_issuer.append(ticker)
            elif tc.tase_id:
                targets.append((ticker, tc, None))

        if not targets and not missing_issuer:
            self.warn(
                "no MAYA-addressable ticker: the official API needs "
                "tase_issuer_id, the public fallback needs tase_id"
            )
            return

        for ticker, tc, issuer_id in targets:
            try:
                yield from self._collect_company(ticker, tc, issuer_id)
            except HttpError as exc:
                self.warn(
                    f"{ticker} (TASE {tc.tase_id}): {exc} - if this persists the "
                    f"MAYA endpoint shape has changed; run `harel probe-maya`"
                )
            except Exception as exc:
                self.warn(f"{ticker}: unexpected {type(exc).__name__}: {exc}")

        if missing_issuer:
            self.warn(
                "no tase_issuer_id for " + ", ".join(missing_issuer) + " - the "
                "official v2 API keys on issuer number, not the security id held "
                "in tase_id. These names are NOT being collected from MAYA."
            )

    # -- fetching ---------------------------------------------------------- #
    def _collect_company(self, ticker: str, tc: Any,
                         issuer_id: Any = None) -> Iterator[RawItem]:
        url, headers, params = self._build_request(tc, issuer_id)
        resp = self.client.get(url, headers=headers, params=params,
                               allow_status=(400, 401, 403, 404, 500))
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
            item = self._record_to_item(rec, ticker, str(tc.tase_id))
            if item is None or item.published_at < self.ctx.since:
                continue
            emitted += 1
            yield item

        # v2 returns meta.hasMore plus an opaque `keyset`, but the documented
        # request parameters carry no continuation field. Report truncation
        # rather than silently dropping the tail.
        meta = payload.get("meta") if isinstance(payload, dict) else None
        if isinstance(meta, dict) and meta.get("hasMore"):
            self.warn(
                f"{ticker}: MAYA has more disclosures than one page returned "
                f"(total={meta.get('total')}) - shorten the collect window"
            )

        if emitted == 0 and records:
            self.warn(
                f"{ticker}: MAYA returned {len(records)} records but none had a "
                f"parseable date - field names may have changed"
            )

    def _build_request(
        self, tc: Any, issuer_id: Any = None
    ) -> tuple[str, dict[str, str], dict[str, Any] | None]:
        raw = self.source.raw
        frm = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        to = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        key = self.source.api_key
        if key:
            # The public endpoint's browser-spoofing headers (X-Maya-With,
            # Referer) mean nothing here - send a clean request. Accept-Language
            # is REQUIRED by the v2 spec, not optional.
            headers = {
                "apiKey": key,
                "Accept": "application/json",
                "Accept-Language": raw.get("official_language", "he-IL"),
            }
            base = raw.get("official_api_base", OFFICIAL_BASE).rstrip("/")
            path = raw.get("official_endpoint", DISCLOSURES_PATH)
            params = {"FromDate": frm, "ToDate": to, "IssuerId": int(issuer_id)}
            return base + path, headers, params

        headers = dict(raw.get("headers") or {})
        base = raw.get("base_url", "https://mayaapi.tase.co.il").rstrip("/")
        template = (raw.get("endpoints") or {}).get(
            "company_reports",
            "/api/report/company?companyId={tase_id}&fromDate={from}&toDate={to}",
        )
        path = (
            template.replace("{tase_id}", str(tc.tase_id))
            .replace("{from}", frm)
            .replace("{to}", to)
        )
        return base + path, headers, None

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
                "maya_report_id": (
                    rec.get("mayaReportId") or rec.get("MayaReportId")
                    or rec.get("reportId") or rec.get("ReportId")
                ),
                "form_type": rec.get("reportType") or rec.get("ReportType"),
                # v2 renamed this to isPriorityReport. Priority reports are the
                # high-signal ones, so losing the flag would matter.
                "is_priority": bool(
                    rec.get("isPriority")
                    or rec.get("IsPriority")
                    or rec.get("isPriorityReport")
                ),
                "is_correction": bool(rec.get("isCorrection")),
                # v2 carries TASE's own event taxonomy per report.
                "tase_events": [
                    e.get("eventName")
                    for e in (rec.get("events") or [])
                    if isinstance(e, dict) and e.get("eventName")
                ],
                "raw_keys": sorted(rec)[:25],
            },
        )


@register("maya_schedule")
class MayaScheduleCollector(Collector):
    """Expected financial-report dates, straight from the issuers.

    ``docs/LIMITATIONS.md`` section 4 rates the missing earnings calendar as one
    of the larger gaps versus Bloomberg - "לסוחר יומי, 'לא להיות שורט לתוך דוח'
    זו הדרישה המינימלית" - and names this endpoint as the fix. TASE publishes
    the expected publication dates and conference-call times for every listed
    company, which is strictly better than scraping or a third-party guess.

    The emitted items are noise-capped on purpose: the deliverable is the
    ``calendar`` row the pipeline harvests from ``scheduled_report_on``, not a
    feed headline. They are still emitted so the source has a visible item count
    instead of looking dead.
    """

    _lookups: tuple[dict[int, str], dict[int, str]] | None = None

    def collect(self) -> Iterator[RawItem]:
        key = self.source.api_key
        if not key:
            self.warn(
                "TASE_API_KEY not set - the financial report schedule has no "
                "public fallback, so the earnings calendar stays empty"
            )
            return

        targets: list[tuple[str, Any]] = []
        missing_issuer: list[str] = []
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if tc is None:
                continue
            issuer_id = tc.raw.get("tase_issuer_id")
            if issuer_id:
                # Only the issuer number is needed here - a name with no
                # security id recorded is still on the TASE report schedule.
                targets.append((ticker, issuer_id))
            elif tc.tase_id:
                missing_issuer.append(ticker)

        for ticker, issuer_id in targets:
            try:
                yield from self._collect_schedule(ticker, issuer_id, key)
            except HttpError as exc:
                self.warn(f"{ticker}: {exc}")
            except Exception as exc:
                self.warn(f"{ticker}: unexpected {type(exc).__name__}: {exc}")

        if missing_issuer:
            self.warn(
                "no tase_issuer_id for " + ", ".join(missing_issuer) + " - no "
                "report dates collected for these names"
            )

    def _headers(self, key: str) -> dict[str, str]:
        return {
            "apiKey": key,
            "Accept": "application/json",
            "Accept-Language": self.source.raw.get("official_language", "he-IL"),
        }

    def _base(self) -> str:
        return self.source.raw.get("official_api_base", OFFICIAL_BASE).rstrip("/")

    def _lookup_tables(self, key: str) -> tuple[dict[int, str], dict[int, str]]:
        """Resolve the two enum id -> label tables so a calendar row reads
        "דוח רבעוני Q2" rather than "type 1 period 2". Best effort: on failure
        the labels fall back to the raw ids."""
        if self._lookups is not None:
            return self._lookups

        def fetch(path: str, id_key: str, name_key: str) -> dict[int, str]:
            try:
                resp = self.client.get(self._base() + path, headers=self._headers(key),
                                       allow_status=(400, 401, 403, 404, 500))
                if resp.status >= 400:
                    return {}
                rows = (resp.json() or {}).get("data") or []
                return {
                    int(r[id_key]): str(r.get(name_key) or "").strip()
                    for r in rows
                    if isinstance(r, dict) and r.get(id_key) is not None
                }
            except Exception as exc:
                self.warn(f"could not read {path.rsplit('/', 1)[-1]}: {exc}")
                return {}

        self._lookups = (
            fetch(REPORT_TYPES_PATH, "financialReportTypeId", "financialReportType"),
            fetch(PERIOD_TYPES_PATH, "periodTypeId", "periodType"),
        )
        return self._lookups

    def _collect_schedule(self, ticker: str, issuer_id: Any, key: str) -> Iterator[RawItem]:
        report_types, period_types = self._lookup_tables(key)
        raw = self.source.raw
        today = datetime.now(timezone.utc).date()
        horizon = int(raw.get("horizon_days", 365))

        resp = self.client.get(
            self._base() + raw.get("schedule_endpoint", SCHEDULE_PATH),
            headers=self._headers(key),
            params={
                "FromScheduledDate": today.isoformat(),
                "ToScheduledDate": (today + timedelta(days=horizon)).isoformat(),
                "IssuerId": int(issuer_id),
            },
            allow_status=(400, 401, 403, 404, 500),
        )
        if resp.status >= 400:
            self.warn(f"{ticker}: schedule returned HTTP {resp.status}")
            return

        try:
            payload = resp.json() or {}
        except Exception:
            self.warn(f"{ticker}: schedule response was not JSON")
            return

        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            item = self._row_to_item(row, ticker, issuer_id, report_types, period_types)
            if item is not None:
                yield item

        meta = payload.get("meta")
        if isinstance(meta, dict) and meta.get("hasMore"):
            self.warn(
                f"{ticker}: more scheduled reports than one page returned "
                f"(total={meta.get('total')}) - shorten horizon_days"
            )

    def _row_to_item(self, row: dict[str, Any], ticker: str, issuer_id: Any,
                     report_types: dict[int, str],
                     period_types: dict[int, str]) -> RawItem | None:
        date_str = str(row.get("scheduledDate") or "")[:10]
        if len(date_str) != 10:
            return None

        year = row.get("year")
        type_id = row.get("financialReportTypeId")
        period_id = row.get("periodTypeId")
        type_label = report_types.get(type_id, f"type {type_id}")
        period_label = period_types.get(period_id, f"period {period_id}")

        clock = str(row.get("scheduledTime") or "")[:5]
        zone = row.get("timeZone") or ""
        when = " ".join(p for p in (date_str, clock, zone) if p)
        label = " ".join(p for p in (type_label, period_label, str(year or "")) if p).strip()

        return self.make_item(
            external_id=f"maya-schedule:{issuer_id}:{year}:{period_id}:{type_id}:{date_str}",
            title=f"[MAYA] {ticker} - {label} expected {when}",
            url=row.get("url") or f"https://maya.tase.co.il/company/{issuer_id}",
            summary=f"TASE-published expected publication date for {ticker}: {label}.",
            published_at=datetime.now(timezone.utc),
            lang="he",
            seed_tickers=[ticker],
            seed_relation="DIRECT",
            meta={
                "venue": "TASE",
                "form_type": SCHEDULE_FORM_TYPE,
                "scheduled_report_on": date_str,
                "schedule_label": f"{ticker} {label}".strip(),
                "scheduled_time": clock or None,
                "time_zone": zone or None,
                "issuer_id": issuer_id,
                "report_year": year,
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
    """Parse a MAYA timestamp to UTC.

    The two channels disagree about zones. The public endpoint returns a naive
    Israel-local stamp; the official v2 API documents ``publicationDate`` as
    ``yyyy-MM-ddTHH:mm:sss'Z'`` - already UTC. Subtracting the Israel offset
    from a stamp that is *already* UTC would move every report 2-3 hours
    earlier, which in a system whose whole edge is "this landed hours before
    the US open" corrupts both the recency decay and the lookback cutoff.
    """
    if not value:
        return None
    value = value.strip()
    explicit_zone = value.endswith("Z") or bool(_UTC_OFFSET_RE.search(value))
    normalised = value.replace("Z", "+00:00")

    if explicit_zone:
        try:
            return datetime.fromisoformat(normalised).astimezone(timezone.utc)
        except ValueError:
            pass

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
            dt = datetime.strptime(normalised.split("+")[0], fmt)
        except ValueError:
            continue
        if explicit_zone:
            return dt.replace(tzinfo=timezone.utc)
        return (dt - timedelta(hours=_israel_offset(dt))).replace(tzinfo=timezone.utc)

    try:
        return datetime.fromisoformat(normalised).astimezone(timezone.utc)
    except ValueError:
        return None


def _israel_offset(dt: datetime) -> int:
    """Israel DST runs from the Friday before the last Sunday of March to the
    last Sunday of October. Approximated by month; the one-hour error at the
    boundary is immaterial for news ordering."""
    return 3 if 4 <= dt.month <= 10 else 2
