"""TASE / MAYA collector - Israeli immediate reports.

This is the biggest structural edge available for this particular basket. 18 of
the 21 resolved names are dual-listed in Tel Aviv, and Israeli issuers file
"immediate reports" (דיווח מיידי) to MAYA during Israeli trading hours - which
is the middle of the night in New York. A MAYA report at 10:00 Israel time is
information you can act on at the 09:30 ET open, hours before it reaches the US
tape.

Honesty about the endpoint
--------------------------
TASE's documented, supported route is the paid/registered API served from
datawise.tase.co.il (set ``TASE_API_KEY``). The keyless fallback is the JSON
backend of the new Maya site itself: ``maya.tase.co.il/api/v1/*``, same-origin
with the site, no key, verified live 2026-08-02. The OLD fallback hosts -
mayaapi.tase.co.il and premayaapi.tase.co.il - sit behind bot protection
(HUMAN Security) that returns 403 to every non-browser client regardless of
headers; do not point the config back at them. The v1 channel is undocumented
and can change without notice, exactly like the channel it replaces, so this
collector:

* prefers the official API when a key is present,
* falls back to the public v1 endpoints otherwise,
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

from ..config import SourceConfig
from ..http import HttpError
from ..models import RawItem
from .base import Collector, CollectorContext, register

# Field-name candidates, in preference order. MAYA has used several spellings.
TITLE_KEYS = ("subject", "Subject", "title", "Title", "headline", "reportName",
              "SubjectHeb", "subjectHeb", "name", "Name")
DATE_KEYS = ("pubDate", "PubDate", "publicationDate", "PublicationDate", "date",
             "Date", "reportDate", "ReportDate", "publishDate")
URL_KEYS = ("url", "Url", "link", "Link", "pdfUrl", "PdfUrl", "reportUrl")
ID_KEYS = ("mayaReportId", "MayaReportId", "reportId", "ReportId", "id", "Id",
           "eventId", "EventId")

# Public channel - the new Maya site's own backend, shared with the site. The
# server validates `limit` to 1..30 and rejects a `toDate` later than its own
# "today" (HTTP 400 either way); `companyId` is the ISSUER number - the same
# value the official API calls IssuerId (TEVA is 629 in both).
PUBLIC_BASE = "https://maya.tase.co.il"
PUBLIC_REPORTS_PATH = "/api/v1/reports/companies"
PUBLIC_EVENTS_PATH = "/api/v1/corporate-actions/events"
PUBLIC_PAGE_LIMIT = 30
# One name posting 300 reports in a 7-day window is not a busy week, it is a
# parser walking in circles - stop and say so rather than hammer the host.
PUBLIC_MAX_PAGES = 10

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
    def __init__(self, source: SourceConfig, ctx: CollectorContext) -> None:
        super().__init__(source, ctx)
        # Names whose response held nothing we could parse. Collected here and
        # reported once at the end of the pass - see collect().
        self.unparseable: list[str] = []
        self.http_failures: list[tuple[str, int]] = []

    def collect(self) -> Iterator[RawItem]:
        official = bool(self.source.api_key)
        targets: list[tuple[str, Any, Any]] = []
        missing_issuer: list[str] = []

        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if tc is None:
                continue
            # BOTH channels key on the issuer number: the official v2 API calls
            # it IssuerId, the public v1 channel calls it companyId, and it is
            # the same registry (TEVA is 629 in both). A name can be
            # addressable by issuer with no security id recorded (PANW, LPSN).
            # Sending tase_id instead would query an unrelated issuer - or
            # nothing - and read exactly like "no news", so skip loudly.
            issuer_id = tc.raw.get("tase_issuer_id")
            if issuer_id:
                targets.append((ticker, tc, issuer_id))
            elif tc.tase_id:
                missing_issuer.append(ticker)

        if not targets and not missing_issuer:
            self.warn(
                "no MAYA-addressable ticker: both the official API and the "
                "public channel need tase_issuer_id"
            )
            return

        for ticker, tc, issuer_id in targets:
            try:
                if official:
                    yield from self._collect_official(ticker, tc, issuer_id)
                else:
                    yield from self._collect_public(ticker, tc, issuer_id)
            except HttpError as exc:
                self.warn(
                    f"{ticker} (TASE {tc.tase_id}): {exc} - if this persists the "
                    f"MAYA endpoint shape has changed; run `harel probe-maya`"
                )
            except Exception as exc:
                self.warn(f"{ticker}: unexpected {type(exc).__name__}: {exc}")

        if missing_issuer:
            self.warn(
                "no tase_issuer_id for " + ", ".join(missing_issuer) + " - both "
                "MAYA channels key on issuer number, not the security id held "
                "in tase_id. These names are NOT being collected from MAYA."
            )

        if self.unparseable:
            # A field rename is the failure this collector is built to survive,
            # and it used to leave no trace anywhere a human looks. save_state
            # was the only signal and it is not one: it writes under the source
            # key, so each name overwrote the previous name's message, and the
            # generator finishes without raising, so the pipeline then stamps a
            # fresh last_ok_at and clears last_error at the end of the same
            # pass. `harel doctor` showed the biggest structural edge in this
            # basket as healthy-and-quiet while it was returning nothing.
            names = ", ".join(self.unparseable)
            self.warn(
                f"MAYA returned no parseable records for {len(self.unparseable)} "
                f"of {len(targets)} names ({names}) - the response shape has "
                f"changed; run `harel probe-maya`"
            )
            self.save_state(last_error=f"0 parseable records for {names}")

        if self.http_failures and len(self.http_failures) == len(targets):
            # Every name refused, which is not a quiet day. The 403s are known
            # and documented - the MAYA 2.0.0 feed is pending approval - but
            # `harel doctor` read the source as healthy through all twenty of
            # them, because warnings live in the run report and doctor reads
            # source_state. A source that failed 100% of its requests must not
            # be indistinguishable from one that was simply asked on a quiet
            # day, whatever the reason, or the panel only reports the failures
            # somebody already thought to look for.
            statuses = sorted({status for _, status in self.http_failures})
            self.save_state(last_error=(
                f"HTTP {'/'.join(str(s) for s in statuses)} for all "
                f"{len(targets)} names"))

    # -- fetching ---------------------------------------------------------- #
    def _collect_public(self, ticker: str, tc: Any,
                        issuer_id: Any) -> Iterator[RawItem]:
        """The new Maya site's own JSON backend, paged.

        Pages ride on limit/offset; a page shorter than the limit is the last
        one. `dated` is counted separately from what is actually emitted so a
        week of only-old reports is not misreported as a field rename.
        """
        tase_id = str(tc.tase_id or issuer_id)
        limit = int(self.source.raw.get("page_limit", PUBLIC_PAGE_LIMIT))
        seen_records = 0
        dated = 0
        for page in range(PUBLIC_MAX_PAGES):
            url, headers, body = self._build_public_request(issuer_id, offset=page * limit)
            resp = self.client.post(url, json=body, headers=headers,
                                    allow_status=(400, 401, 403, 404, 500))
            if resp.status >= 400:
                self.warn(f"{ticker}: MAYA returned HTTP {resp.status}")
                self.http_failures.append((ticker, resp.status))
                return

            try:
                payload = resp.json()
            except Exception:
                self.warn(f"{ticker}: MAYA response was not JSON")
                return

            if payload == [] and page == 0:
                # A clean empty list is a quiet week, not a shape change.
                return

            records = _find_records(payload)
            if not records:
                if page == 0:
                    self.unparseable.append(ticker)
                break

            for rec in records:
                item = self._record_to_item(rec, ticker, tase_id)
                if item is None:
                    continue
                dated += 1
                if item.published_at < self.ctx.since:
                    continue
                yield item

            seen_records += len(records)
            if len(records) < limit:
                break
        else:
            self.warn(
                f"{ticker}: more than {PUBLIC_MAX_PAGES * limit} MAYA reports "
                f"in the window - tail dropped; shorten the collect window"
            )

        if seen_records and dated == 0:
            self.warn(
                f"{ticker}: MAYA returned {seen_records} records but none had a "
                f"parseable date - field names may have changed"
            )

    def _collect_official(self, ticker: str, tc: Any,
                          issuer_id: Any = None) -> Iterator[RawItem]:
        url, headers, params = self._build_official_request(tc, issuer_id)
        resp = self.client.get(url, headers=headers, params=params,
                               allow_status=(400, 401, 403, 404, 500))
        if resp.status >= 400:
            self.warn(f"{ticker}: MAYA returned HTTP {resp.status}")
            self.http_failures.append((ticker, resp.status))
            return

        try:
            payload = resp.json()
        except Exception:
            self.warn(f"{ticker}: MAYA response was not JSON")
            return

        records = _find_records(payload)
        if not records:
            self.unparseable.append(ticker)
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

    def _build_official_request(
        self, tc: Any, issuer_id: Any = None
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        raw = self.source.raw
        frm = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        to = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

        # No browser-spoofing headers here - send a clean request.
        # Accept-Language is REQUIRED by the v2 spec, not optional.
        headers = {
            "apiKey": self.source.api_key,
            "Accept": "application/json",
            "Accept-Language": raw.get("official_language", "he-IL"),
        }
        base = raw.get("official_api_base", OFFICIAL_BASE).rstrip("/")
        path = raw.get("official_endpoint", DISCLOSURES_PATH)
        params = {"FromDate": frm, "ToDate": to, "IssuerId": int(issuer_id)}
        return base + path, headers, params

    def _build_public_request(
        self, issuer_id: Any, offset: int = 0
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        raw = self.source.raw
        base = raw.get("base_url", PUBLIC_BASE).rstrip("/")
        path = (raw.get("endpoints") or {}).get("company_reports", PUBLIC_REPORTS_PATH)
        limit = int(raw.get("page_limit", PUBLIC_PAGE_LIMIT))
        now = datetime.now(timezone.utc)
        # A toDate past the server's own "today" is a 400, and the named day is
        # included in full - so today-UTC is both accepted and complete.
        frm = (now - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000Z")
        to = now.strftime("%Y-%m-%dT00:00:00.000Z")
        headers = {
            "Accept": "application/json",
            # he-IL keeps titles in Hebrew. Without it the server prefers the
            # English translation where one exists and the feed goes bilingual.
            "Accept-Language": raw.get("official_language", "he-IL"),
        }
        body = {
            "pageNumber": offset // limit + 1,
            "fromDate": frm,
            "toDate": to,
            "by": "company",
            "companyId": int(issuer_id),
            "limit": limit,
            "offset": offset,
        }
        return base + path, headers, body

    # -- shaping ----------------------------------------------------------- #
    def _record_to_item(self, rec: dict[str, Any], ticker: str,
                        tase_id: str) -> RawItem | None:
        title = _first(rec, TITLE_KEYS)
        if not title:
            return None
        published = _parse_maya_date(_first(rec, DATE_KEYS))
        if published is None:
            return None

        report_id = (
            rec.get("mayaReportId") or rec.get("MayaReportId")
            or rec.get("reportId") or rec.get("ReportId") or rec.get("id")
        )
        url = _first(rec, URL_KEYS)
        if not url:
            # v1 records carry no URL of their own; the site's canonical page
            # for a report is /he/reports/{id}.
            url = (f"https://maya.tase.co.il/he/reports/{report_id}" if report_id
                   else f"https://maya.tase.co.il/reports/company/{tase_id}")
        if url.startswith("/"):
            url = "https://maya.tase.co.il" + url
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
                "maya_report_id": report_id,
                # v1 has reportType: null but carries the TASE form number
                # (e.g. "ת076") as formId.
                "form_type": (rec.get("reportType") or rec.get("ReportType")
                              or rec.get("formId")),
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
                # v1 attachment paths are relative to the mayafiles CDN, which
                # serves them with no auth - direct document links for the
                # agent reading the item.
                "attachment_urls": [
                    "https://mayafiles.tase.co.il/" + str(a["url"]).lstrip("/")
                    for a in (rec.get("attachments") or [])
                    if isinstance(a, dict) and a.get("url")
                ][:6],
                "raw_keys": sorted(rec)[:25],
            },
        )


@register("maya_schedule")
class MayaScheduleCollector(Collector):
    """Expected financial-report dates, straight from the issuers.

    ``docs/LIMITATIONS.md`` section 4 rates the missing earnings calendar as one
    of the larger gaps versus Bloomberg - "לסוחר יומי, 'לא להיות שורט לתוך דוח'
    זו הדרישה המינימלית" - and names this source as the fix. TASE publishes
    the expected publication dates and conference-call times for every listed
    company, which is strictly better than scraping or a third-party guess.

    Two channels: the official financial-report-schedule product when
    ``TASE_API_KEY`` is active, and the new Maya site's keyless
    corporate-actions feed otherwise - the same upcoming-events data the site
    shows under "אירועים קרובים".

    The emitted items are noise-capped on purpose: the deliverable is the
    ``calendar`` row the pipeline harvests from ``scheduled_report_on``, not a
    feed headline. They are still emitted so the source has a visible item count
    instead of looking dead.
    """

    _lookups: tuple[dict[int, str], dict[int, str]] | None = None

    def collect(self) -> Iterator[RawItem]:
        key = self.source.api_key

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
                if key:
                    yield from self._collect_schedule(ticker, issuer_id, key)
                else:
                    yield from self._collect_events_public(ticker, issuer_id)
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

    # -- keyless fallback --------------------------------------------------- #
    def _collect_events_public(self, ticker: str, issuer_id: Any) -> Iterator[RawItem]:
        """Expected report dates without the subscription.

        The new Maya site publishes every company's upcoming corporate events
        on the same keyless v1 channel as the reports themselves. Two event
        kinds matter here: "פרסום דוחות" (report publication - the row a
        trader must not be short into) and "שיחת ועידה" (conference call).
        The clock usually rides in the call's moreInfo ("תתכנס בשעה - 15:30"),
        so a same-day call annotates the publication row rather than becoming
        a second calendar entry for the same date.
        """
        raw = self.source.raw
        base = str(raw.get("public_base", PUBLIC_BASE)).rstrip("/")
        path = raw.get("public_events_endpoint", PUBLIC_EVENTS_PATH)
        horizon = int(raw.get("horizon_days", 365))
        today = datetime.now(timezone.utc).date()
        headers = {
            "Accept": "application/json",
            "Accept-Language": raw.get("official_language", "he-IL"),
        }

        rows: list[dict[str, Any]] = []
        for page in range(PUBLIC_MAX_PAGES):
            body = {
                "pageNumber": page + 1,
                "fromDate": f"{today.isoformat()}T00:00:00.000Z",
                "toDate": f"{(today + timedelta(days=horizon)).isoformat()}T00:00:00.000Z",
                "by": "company",
                "companyId": int(issuer_id),
                "limit": PUBLIC_PAGE_LIMIT,
                "offset": page * PUBLIC_PAGE_LIMIT,
            }
            resp = self.client.post(base + path, json=body, headers=headers,
                                    allow_status=(400, 401, 403, 404, 500))
            if resp.status >= 400:
                self.warn(f"{ticker}: events returned HTTP {resp.status}")
                return
            try:
                page_rows = resp.json() or []
            except Exception:
                self.warn(f"{ticker}: events response was not JSON")
                return
            if not isinstance(page_rows, list):
                self.warn(f"{ticker}: events response shape changed "
                          f"({type(page_rows).__name__}) - run `harel probe-maya`")
                return
            rows.extend(r for r in page_rows if isinstance(r, dict))
            if len(page_rows) < PUBLIC_PAGE_LIMIT:
                break

        # An empty list is normal: a name that has not yet filed its next
        # report date simply has no rows. No warning for that.
        calls: dict[str, str] = {}
        for row in rows:
            if _is_event(row, 1, "שיחת ועידה"):
                clock = _clock_in(str(row.get("moreInfo") or ""))
                date_str = str(row.get("date") or "")[:10]
                if clock and len(date_str) == 10:
                    calls[date_str] = clock

        for row in rows:
            if not _is_event(row, 2, "פרסום דוחות"):
                continue
            date_str = str(row.get("date") or "")[:10]
            if len(date_str) != 10:
                continue
            info = str(row.get("moreInfo") or "").strip()
            clock = calls.get(date_str) or _clock_in(info)
            label = info or "פרסום דוחות"
            when = " ".join(p for p in (date_str, clock) if p)
            report_id = row.get("reportId")
            yield self.make_item(
                external_id=f"maya-events:{issuer_id}:{date_str}:{report_id or ''}",
                title=f"[MAYA] {ticker} - {label} expected {when}",
                url=(f"https://maya.tase.co.il/he/reports/{report_id}"
                     if report_id else "https://maya.tase.co.il/he"),
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
                    "scheduled_time": clock,
                    "time_zone": "Israel",
                    "issuer_id": issuer_id,
                    "report_year": None,
                },
            )


# --------------------------------------------------------------------------- #
# "תתכנס בשעה - 15:30" / "בשעה 09:30" - the clock inside an event's moreInfo.
_CLOCK_RE = re.compile(r"בשעה\s*-?\s*(\d{1,2}:\d{2})")


def _clock_in(text: str) -> str | None:
    match = _CLOCK_RE.search(text)
    return match.group(1) if match else None


def _is_event(row: dict[str, Any], event_id: int, name: str) -> bool:
    """The numeric event ids are undocumented and the labels are Hebrew
    strings - either alone is a guess, both together survive a rename of one.
    (Observed live: eventId 2 = פרסום דוחות, eventId 1 = שיחת ועידה.)"""
    if row.get("eventId") == event_id:
        return True
    return name in str(row.get("eventName") or "")


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
