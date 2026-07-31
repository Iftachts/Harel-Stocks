"""SEC EDGAR collectors.

Two distinct channels:

``EdgarSubmissionsCollector``
    Polls ``data.sec.gov/submissions/CIK##########.json`` for each of our
    issuers. This is the legally-mandated material-events channel (8-K / 6-K)
    and it is the closest free equivalent to a Bloomberg company wire.

``EdgarFullTextCollector``
    Searches EDGAR full text for our companies' names inside *other* issuers'
    filings. That is how you find out that a competitor's 10-K names Teva as
    the litigant, or that a customer disclosed a Camtek order. This is the
    "indirect news" channel and it has no free equivalent anywhere else.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

# EDGAR reports acceptance times on the Eastern clock. See _parse_edgar_dt.
EDGAR_TZ = ZoneInfo("America/New_York")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"

# 8-K item codes -> (label, whether a day trader should care)
EIGHT_K_ITEMS: dict[str, tuple[str, str]] = {
    "1.01": ("Material definitive agreement", "high"),
    "1.02": ("Termination of material agreement", "high"),
    "1.03": ("Bankruptcy or receivership", "critical"),
    "1.05": ("Material cybersecurity incident", "critical"),
    "2.01": ("Completion of acquisition/disposition", "high"),
    "2.02": ("Results of operations (earnings)", "high"),
    "2.03": ("Direct financial obligation created", "medium"),
    "2.04": ("Acceleration of obligation", "high"),
    "2.05": ("Exit or disposal costs", "medium"),
    "2.06": ("Material impairment", "high"),
    "3.01": ("Delisting notice / listing rule failure", "high"),
    "3.02": ("Unregistered sale of equity (dilution)", "high"),
    "3.03": ("Modification of security holder rights", "medium"),
    "4.01": ("Change of auditor", "high"),
    "4.02": ("Non-reliance on prior financials", "critical"),
    "5.01": ("Change in control", "critical"),
    "5.02": ("Officer/director departure or appointment", "medium"),
    "5.03": ("Amendment to articles/bylaws", "low"),
    "5.07": ("Shareholder vote results", "low"),
    "7.01": ("Reg FD disclosure", "medium"),
    "8.01": ("Other events", "medium"),
    "9.01": ("Financial statements and exhibits", "low"),
}

# Forms whose mere arrival is news, regardless of content.
LOUD_FORMS = {
    "8-K", "6-K", "SC 13D", "SC 13D/A", "SC TO-T", "SC TO-I", "425",
    "NT 10-K", "NT 10-Q", "NT 20-F", "25-NSE", "424B5", "424B4", "F-3", "S-3",
}


@register("edgar_submissions")
class EdgarSubmissionsCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        cik_map = self._ensure_ciks()
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            cik = tc.cik10 or cik_map.get(ticker)
            if not cik:
                self.warn(f"{ticker}: no CIK - not a US filer, or resolution failed")
                continue
            try:
                yield from self._collect_one(ticker, cik)
            except HttpError as exc:
                self.warn(f"{ticker} (CIK {cik}): {exc}")
            except Exception as exc:
                self.warn(f"{ticker}: unexpected {type(exc).__name__}: {exc}")

    # -- CIK resolution ---------------------------------------------------- #
    def _ensure_ciks(self) -> dict[str, str]:
        """Resolve any ticker whose CIK is missing from universe.yaml."""
        missing = [
            t for t in self.active_tickers
            if not (self.cfg.ticker(t) and self.cfg.ticker(t).cik10)
        ]
        cached = self.state().get("cursor")
        if not missing and cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass
        if not missing:
            return {}

        try:
            resp = self.client.get(TICKER_MAP_URL)
            data = resp.json() or {}
        except Exception as exc:
            self.warn(f"could not fetch SEC ticker map: {exc}")
            return {}

        by_ticker = {
            str(row.get("ticker", "")).upper(): str(row.get("cik_str", "")).zfill(10)
            for row in data.values()
            if isinstance(row, dict)
        }
        resolved = {t: by_ticker[t] for t in missing if t in by_ticker}
        for t in missing:
            if t not in resolved:
                self.warn(
                    f"{t}: not found in SEC ticker map - it is either not a US "
                    f"filer or the symbol is wrong (see docs/LIMITATIONS.md)"
                )
        self.save_state(cursor=json.dumps(resolved))
        return resolved

    # -- per-issuer -------------------------------------------------------- #
    def _collect_one(self, ticker: str, cik: str) -> Iterator[RawItem]:
        state_key = f"{self.source.key}:{ticker}"
        prev = self.db.get_source_state(state_key)
        resp = self.client.get(
            SUBMISSIONS_URL.format(cik=cik),
            etag=prev.get("etag"),
            last_modified=prev.get("last_modified"),
            allow_status=(404,),
        )
        now = datetime.now(timezone.utc).isoformat()
        if resp.not_modified:
            self.db.set_source_state(state_key, last_run_at=now, last_ok_at=now,
                                     consecutive_failures=0, items_last_run=0)
            return
        if resp.status == 404:
            self.warn(f"{ticker}: CIK {cik} returned 404")
            return

        data = resp.json() or {}
        recent = (data.get("filings") or {}).get("recent") or {}
        company = data.get("name") or ticker
        cik_int = str(int(cik))

        n = len(recent.get("accessionNumber", []))
        count = 0
        for i in range(n):
            try:
                item = self._filing_to_item(ticker, company, cik_int, recent, i)
            except Exception as exc:
                self.warn(f"{ticker}: bad filing row {i} ({exc})")
                continue
            if item is None:
                continue
            if item.published_at < self.ctx.since:
                # `recent` is newest-first, so the first stale row ends the scan.
                break
            count += 1
            yield item

        self.db.set_source_state(
            state_key, etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
            last_run_at=now, last_ok_at=now, last_error=None,
            consecutive_failures=0, items_last_run=count,
        )

    def _filing_to_item(self, ticker: str, company: str, cik_int: str,
                        recent: dict, i: int) -> RawItem | None:
        def get(field: str) -> str:
            values = recent.get(field) or []
            return str(values[i]) if i < len(values) else ""

        form = get("form").upper()
        accession = get("accessionNumber")
        if not accession:
            return None
        accession_nodash = accession.replace("-", "")

        published = _parse_edgar_dt(get("acceptanceDateTime")) or _parse_edgar_dt(
            get("filingDate")
        )
        if published is None:
            return None

        doc = get("primaryDocument")
        url = (
            ARCHIVE_URL.format(cik=cik_int, accession=accession_nodash, doc=doc)
            if doc
            else FILING_INDEX_URL.format(cik=cik_int, accession=accession_nodash)
        )

        item_codes = [c.strip() for c in get("items").split(",") if c.strip()]
        item_labels = [EIGHT_K_ITEMS.get(c, (c, "medium"))[0] for c in item_codes]
        severities = [EIGHT_K_ITEMS.get(c, (c, "medium"))[1] for c in item_codes]

        description = get("primaryDocDescription")
        title_bits = [f"[{form}]", company]
        if item_labels:
            title_bits.append("- " + "; ".join(item_labels))
        elif description:
            title_bits.append(f"- {description}")

        return self.make_item(
            external_id=f"{accession}:{doc or 'index'}",
            title=" ".join(title_bits),
            url=url,
            summary=(
                f"SEC form {form} filed by {company}. "
                + (f"Items: {', '.join(item_codes)}. " if item_codes else "")
                + (f"Document: {description}. " if description else "")
                + f"Report date: {get('reportDate') or get('filingDate')}."
            ),
            published_at=published,
            seed_tickers=[ticker],
            seed_relation="DIRECT",
            meta={
                "form_type": form,
                "accession": accession,
                "cik": cik_int,
                "items": item_codes,
                "item_labels": item_labels,
                "item_severity": max(severities, key=_severity_rank) if severities else None,
                "is_loud_form": form in LOUD_FORMS,
                "file_number": get("fileNumber"),
                "report_date": get("reportDate"),
            },
        )


@register("edgar_full_text")
class EdgarFullTextCollector(Collector):
    """Find our companies named inside *other* issuers' filings."""

    # Only forms where a mention carries real information.
    FORMS = "8-K,10-K,10-Q,20-F,6-K,S-1,424B5,DEF 14A"

    def collect(self) -> Iterator[RawItem]:
        start = self.ctx.since.date().isoformat()
        end = datetime.now(timezone.utc).date().isoformat()

        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            # Search on the distinctive name, not the ticker: "ICL" or "ORA"
            # inside a filing is almost always something else.
            query = f'"{tc.name.split(" Ltd")[0].split(" Inc")[0].strip()}"'
            try:
                yield from self._search(query, ticker, tc.cik10, start, end)
            except HttpError as exc:
                self.warn(f"full-text {ticker}: {exc}")
            except Exception as exc:
                self.warn(f"full-text {ticker}: unexpected {type(exc).__name__}: {exc}")

    def _search(self, query: str, ticker: str, own_cik: str | None,
                start: str, end: str) -> Iterator[RawItem]:
        params = {
            "q": query,
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": self.FORMS,
        }
        resp = self.client.get(FTS_URL, params=params, allow_status=(400, 404))
        if resp.status >= 400:
            self.warn(f"full-text search returned HTTP {resp.status} for {query}")
            return

        data = resp.json() or {}
        hits = ((data.get("hits") or {}).get("hits")) or []
        for hit in hits:
            try:
                item = self._hit_to_item(hit, ticker, own_cik, query)
            except Exception as exc:
                self.warn(f"full-text bad hit ({exc})")
                continue
            if item is not None:
                yield item

    def _hit_to_item(self, hit: dict, ticker: str, own_cik: str | None,
                     query: str) -> RawItem | None:
        src = hit.get("_source") or {}
        hit_id = hit.get("_id") or ""
        if ":" not in hit_id:
            return None
        accession, doc = hit_id.split(":", 1)

        ciks = [str(c).zfill(10) for c in (src.get("ciks") or [])]
        # A filing by the company itself is already covered by the submissions
        # collector; here we only want *other* issuers mentioning it.
        if own_cik and own_cik in ciks:
            return None

        filer_names = src.get("display_names") or []
        filer = filer_names[0] if filer_names else "unknown filer"
        form = str(src.get("root_form") or src.get("file_type") or "").upper()
        file_date = src.get("file_date") or ""
        published = _parse_edgar_dt(file_date)
        if published is None:
            return None

        cik_int = str(int(ciks[0])) if ciks else ""
        url = ARCHIVE_URL.format(
            cik=cik_int, accession=accession.replace("-", ""), doc=doc
        )

        return self.make_item(
            external_id=f"fts:{ticker}:{hit_id}",
            title=f"[{form}] {filer} mentions {self.cfg.ticker(ticker).name}",
            url=url,
            summary=(
                f"{filer} filed a {form} on {file_date} whose text contains "
                f"{query}. Indirect signal: read the surrounding paragraph for "
                f"litigation, supply, competitive or customer context."
            ),
            published_at=published,
            seed_tickers=[ticker],
            seed_relation="PEER",
            meta={
                "form_type": form,
                "accession": accession,
                "filer": filer,
                "filer_ciks": ciks,
                "indirect": True,
                "matched_query": query,
                # The title we synthesise here contains our own company name.
                # Without this lock the linker would read that name back out and
                # upgrade the link to DIRECT, turning "Viatris mentions Teva"
                # into "Teva news". It is not.
                "lock_seed_relation": True,
            },
        )


def _parse_edgar_dt(value: str) -> datetime | None:
    """Parse an EDGAR timestamp to UTC.

    ``acceptanceDateTime`` ends in "Z" but the clock is **Eastern**, not UTC -
    the filing window is 06:00-22:00 ET and the raw values sit squarely inside
    it. Reading them as UTC moved every filing 4-5 hours earlier, which pushed
    after-close filings back into the trading session: a Form 4 accepted at
    16:13 ET was stored as 12:13 ET and could then be read as the cause of that
    day's move. It also made every filing look hours fresher than it was, which
    inflates the recency decay and the intraday timing boosts.

    A date-only ``filingDate`` is anchored to Eastern midnight so the calendar
    date stays the one EDGAR means.
    """
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=EDGAR_TZ).astimezone(timezone.utc)
    return None


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _severity_rank(sev: str) -> int:
    return _SEVERITY_ORDER.get(sev, 1)
