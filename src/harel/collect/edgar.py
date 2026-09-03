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

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

# EDGAR mostly reports acceptance times on the Eastern clock, but some records
# are true UTC despite the identical formatting. See _parse_edgar_dt.
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
# EDGAR spells the Schedule 13 forms BOTH ways in the same submissions history:
# older rows say "SC 13D", newer ones "SCHEDULE 13D". config/scoring.yaml already
# learned this for the 13G noise caps; this set had not, so a modern activist
# stake was not a loud form. Both spellings, everywhere.
LOUD_FORMS = {
    "8-K", "6-K", "SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A",
    "SC TO-T", "SC TO-I", "425",
    "NT 10-K", "NT 10-Q", "NT 20-F", "25-NSE", "424B5", "424B4", "F-3", "S-3",
}



# --------------------------------------------------------------------------- #
# 6-K enrichment: reading the exhibit, because the form itself says nothing.
#
# 17 of the 22 names here are Foreign Private Issuers, whose material-event form
# is the 6-K - and a 6-K carries NO item-code taxonomy. The submissions feed
# returns items='' and primaryDocDescription='6-K', so the title this collector
# could build was literally "[6-K] ELBIT SYSTEMS LTD - 6-K". For Elbit that is
# 845 filings rendered contentless, and it is the reason a $270 million contract
# reached the terminal at NOISE while the tape alert called the move unexplained.
#
# The content is one directory listing and one document away, on the same host,
# with our own User-Agent. The press release lives in an EXHIBIT; the primary
# document is the cover page ("Report of Foreign Private Issuer Pursuant to Rule
# 13a-16..."), which is why there is deliberately no fallback to it: an
# unhelpful title is better than a confidently wrong one made of boilerplate.
# --------------------------------------------------------------------------- #

# Two extra requests per 6-K, so it is budgeted per pass like rss.py's body
# lookups. The submissions feed re-emits the same filings every pass, so results
# are cached by accession in source_state and a filing is read exactly once.
MAX_EXHIBIT_LOOKUPS_PER_RUN = 8
MAX_EXHIBIT_BYTES = 400_000
EXHIBIT_HEADLINE_CHARS = 240

# EDGAR renders an exhibit with its own header line: "EX-99.1 2 exhibit_1.htm".
_EX_WRAPPER = re.compile(r"^\s*(?:EX|EXHIBIT)[\w.\-]*\s+\d+\s+\S+?\.(?:htm|html)\b\s*",
                         re.IGNORECASE)
_FILENAME_TOKEN = re.compile(r"^\s*\S+\.(?:htm|html)\b\s*", re.IGNORECASE)
# "TOWER SEMICONDUCTOR LTD - 928876 - 2026"
_EDGAR_CAPTION = re.compile(r"^\s*[A-Z][A-Z\s.,&'\-]{3,}\s+-\s+\d{4,}\s+-\s+\d{4}\s*",
                            re.IGNORECASE)
# The dateline that ends a headline: "Haifa, Israel, September 1, 2026 - ..."
_DATELINE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+\d{1,2},\s*\d{4}\s*[\u2013\u2014-]", re.IGNORECASE)
_TICKER_PAREN = re.compile(r"\((?:NASDAQ|NYSE|TASE|OTC)\s*:", re.IGNORECASE)
# Searched, not matched-at-start: the cover page wraps across lines, so
# "of the Securities Exchange Act of 1934" is its own line and an anchored
# pattern walks straight past it into the output.
_COVER_PAGE = re.compile(
    r"(?i)(^exhibit\s*[\d.\-]*$|^press release$|^for immediate release$|^logo$"
    r"|report of foreign private issuer|pursuant to rule 13a|pursuant to rule 15d"
    r"|securities exchange act|commission file number|translation of registrant"
    r"|^form\s*6-?k$|^united states|securities and exchange commission"
    r"|^washington|under the securities exchange)")


def _exhibit_headline(html_text: str) -> str:
    """The press-release headline out of a rendered EDGAR exhibit.

    Structural, not schematic: the headline is whatever readable text precedes
    the dateline, with EDGAR's own rendering furniture stripped. Filer agents
    each lay these out differently (Z-K Global writes exhibit_1.htm, EdgarAgents
    writes ea030232701ex99-1.htm) so nothing keys on a filename.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text,
                  flags=re.S | re.IGNORECASE)
    text = unescape(re.sub(r"<[^>]+>", "\n", text))
    text = re.sub(r"[ \t\r\xa0]+", " ", text)

    collected: list[str] = []
    for line in text.split("\n"):
        line = _EX_WRAPPER.sub("", line.strip())
        line = _EDGAR_CAPTION.sub("", line)
        line = _FILENAME_TOKEN.sub("", line).strip()
        # A bare page number or footnote marker at the top of the exhibit.
        line = re.sub(r"^\s*\d{1,3}\s+(?=[A-Z])", "", line)
        if len(line) < 3:
            continue
        if _DATELINE.search(line) or _TICKER_PAREN.search(line):
            break
        if _COVER_PAGE.search(line):
            continue
        if not re.search(r"[A-Za-z]{3}", line):
            continue
        collected.append(line)
        if len(" ".join(collected)) > EXHIBIT_HEADLINE_CHARS:
            break
    return re.sub(r"\s+", " ", " ".join(collected)).strip()[:EXHIBIT_HEADLINE_CHARS]


def _exhibit_size(entry: dict[str, Any]) -> int:
    try:
        return int(str(entry.get("size") or "0").strip() or 0)
    except (TypeError, ValueError):
        return 0


@register("edgar_submissions")
class EdgarSubmissionsCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        self._exhibit_lookups = 0
        cik_map = self._ensure_ciks()
        rejected = self._mismatched_ciks()
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            if ticker in rejected:
                # Deliberately collect NOTHING rather than the wrong company.
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

    def _mismatched_ciks(self) -> set[str]:
        """Tickers whose configured CIK belongs to somebody else.

        `_ensure_ciks` only ever FILLED a missing CIK; a CIK already written in
        universe.yaml was taken on faith and never checked against the SEC's own
        map. Four of the twenty-two were wrong, and the failure was silent and
        confident: CIK 0001086222 is Akamai, so AudioCodes' lane carried
        Akamai's 8-Ks; 0001505497 is BioRestorative Therapies, so BrainsWay's
        did too - both at trust 1.0, the highest the system can assign, tagged
        DIRECT. Gilat's pointed at a 2003 municipal bond trust, which files
        nothing, so that name simply went quiet on the best source it has.

        A wrong issuer is worse than a missing one - a missing one is visible in
        `harel doctor`, while a wrong one reads as news about your position - so
        a mismatch excludes the ticker rather than degrading it.

        Verification re-runs whenever the configured set changes, not every
        pass: the map is ~800KB and the answer only moves when someone edits
        universe.yaml.
        """
        configured = {
            t: self.cfg.ticker(t).cik10
            for t in self.active_tickers
            if self.cfg.ticker(t) and self.cfg.ticker(t).cik10
        }
        if not configured:
            return set()
        fingerprint = hashlib.sha1(
            json.dumps(configured, sort_keys=True).encode()
        ).hexdigest()

        # A sub-keyed state row, as the RSS collector does per feed:
        # `set_source_state` writes a fixed column list and silently discards
        # any other field, so a bespoke column here would never have persisted
        # and the 800KB map would have been refetched every single pass.
        state_key = f"{self.source.key}:cik-verification"
        cached = self.db.get_source_state(state_key).get("cursor")
        if cached:
            try:
                prev = json.loads(cached)
                if prev.get("fingerprint") == fingerprint:
                    return set(prev.get("bad") or [])
            except (json.JSONDecodeError, AttributeError):
                pass

        try:
            data = self.client.get(TICKER_MAP_URL).json() or {}
        except Exception as exc:
            self.warn(f"could not verify CIKs against the SEC ticker map: {exc}")
            return set()

        by_ticker = {
            str(row.get("ticker", "")).upper(): str(row.get("cik_str", "")).zfill(10)
            for row in data.values()
            if isinstance(row, dict)
        }
        bad: set[str] = set()
        for ticker, cik in configured.items():
            official = by_ticker.get(ticker.upper())
            # Absent from the map is not a mismatch: plenty of foreign issuers
            # file without an entry there. Only a positive disagreement counts.
            if official and official != cik:
                bad.add(ticker)
                self.warn(
                    f"{ticker}: configured CIK {cik} belongs to another filer - "
                    f"the SEC ticker map says {official}. Refusing to collect "
                    f"for {ticker} until config/universe.yaml is corrected."
                )
        self.db.set_source_state(
            state_key,
            cursor=json.dumps({"fingerprint": fingerprint, "bad": sorted(bad)}),
        )
        return bad

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

    def _form4_detail(self, form: str, url: str) -> dict[str, Any] | None:
        """Read the transaction codes out of a Form 4 so a grant stops looking
        like a purchase.

        Nothing parsed the codes, so every Form 4 scored identically off its
        form type alone. A routine RSU award to an SVP and an executive buying
        on the open market are opposite signals, and the awards - which are far
        more numerous - were taking the top of the feed above real earnings.

        Only fetched for filings we have not stored yet: the collector re-emits
        the same filings every pass, and the codes never change.
        """
        # The renderer directory carries a version and it is not always one
        # digit. TEVA files xslF345X06 today, so this works today - but an
        # xslF345X10 renderer would leave the URL untouched, return None below,
        # and silently downgrade EVERY Form 4 back to plain "4", scored like an
        # open-market purchase.
        raw_url = re.sub(r"/xslF345X\d+/", "/", url)
        if raw_url == url or not raw_url.endswith(".xml"):
            return None
        try:
            resp = self.client.get(raw_url, allow_status=(403, 404))
            if resp.status >= 400:
                # Never lose the filing over a missing detail - but say so.
                # Failing quietly here downgrades every Form 4 back to "looks
                # like a purchase" with nothing on screen to explain why.
                self.warn(f"Form 4 detail unavailable (HTTP {resp.status}): {raw_url}")
                return None
            body = resp.text
        except Exception as exc:
            self.warn(f"Form 4 detail failed ({type(exc).__name__}): {raw_url}")
            return None

        codes, qty = _form4_transactions(body)
        if not codes:
            return None
        titles = re.findall(_TAG.format("officerTitle"), body)
        owners = re.findall(_TAG.format("rptOwnerName"), body)

        # The filing is XML, so the officer title arrives escaped: a title of
        # "Policy & General Counsel" reaches us as "Policy &amp; General
        # Counsel" and would be shown that way in the feed.
        titles = [unescape(t) for t in titles]
        owners = [unescape(o) for o in owners]

        labels = [FORM4_CODES.get(c, (c, False))[0] for c in codes]
        signal = any(FORM4_CODES.get(c, (c, False))[1] for c in codes)

        who = (titles or owners or [""])[0]
        label = " / ".join(dict.fromkeys(labels))
        if qty:
            label += f" {qty:,.0f} sh"
        if who:
            label += f" - {who}"

        return {
            "form_type": form if signal else ROUTINE_FORM4,
            "codes": sorted(set(codes)),
            "open_market": signal,
            "shares": qty or None,
            "who": who or None,
            "label": label,
        }

    def _six_k_headline(self, cik_int: str, accession: str,
                        accession_nodash: str, primary_doc: str) -> str:
        """The press-release headline inside a 6-K, or "" if there isn't one.

        Cached per accession and budgeted per pass. The submissions feed
        re-emits the same filings on every poll, so without the cache this
        would multiply the request cost by the number of 6-Ks still inside the
        lookback window, forever, for an answer that cannot change.

        Returns "" rather than guessing when the filing has no exhibit: the
        primary document of a 6-K is the cover page, and "Report of Foreign
        Private Issuer Pursuant to Rule 13a-16" as a headline would be worse
        than the form name it replaced.
        """
        state_key = f"{self.source.key}:6k:{accession}"
        cached = self.db.get_source_state(state_key).get("cursor")
        if cached is not None:
            return str(cached)
        if self._exhibit_lookups >= MAX_EXHIBIT_LOOKUPS_PER_RUN:
            return ""
        self._exhibit_lookups += 1

        headline = ""
        try:
            index = self.client.get(
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                f"{accession_nodash}/index.json"
            ).json() or {}
            entries = ((index.get("directory") or {}).get("item")) or []
            exhibits = [
                e for e in entries
                if isinstance(e, dict)
                and str(e.get("name") or "").lower().endswith((".htm", ".html"))
                and e.get("name") != primary_doc
                and _exhibit_size(e) > 0
            ]
            if exhibits:
                # Largest, not first: filer agents order the directory
                # differently and the press release is the substantial document.
                best = max(exhibits, key=_exhibit_size)
                resp = self.client.get(
                    f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                    f"{accession_nodash}/{best['name']}"
                )
                headline = _exhibit_headline((resp.text or "")[:MAX_EXHIBIT_BYTES])
        except HttpError as exc:
            self.warn(f"6-K exhibit {accession}: {exc}")
            return ""
        except Exception as exc:
            self.warn(f"6-K exhibit {accession}: "
                      f"unexpected {type(exc).__name__}: {exc}")
            return ""

        # Cached even when empty - "this filing has no exhibit" is an answer
        # worth remembering, and re-deriving it every pass costs two requests.
        self.db.set_source_state(state_key, cursor=headline)
        return headline

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
        headline = ""
        if item_labels:
            title_bits.append("- " + "; ".join(item_labels))
        elif form in ("6-K", "6-K/A"):
            # A 6-K has no item codes, so without this the title is the form
            # name twice over. The words are in the exhibit.
            headline = self._six_k_headline(cik_int, accession, accession_nodash, doc)
            title_bits.append(f"- {headline or description or form}")
        elif description:
            title_bits.append(f"- {description}")

        external_id = f"{accession}:{doc or 'index'}"
        insider = None
        if form in ("4", "4/A"):
            # The submissions feed re-emits the same filings every pass. Reuse
            # what we already parsed rather than refetching - and reuse it
            # rather than dropping it, or the rebuilt item would overwrite the
            # enriched row and the classification would decay away.
            prior = self.db.stored_meta(self.source.key, external_id) or {}
            insider = prior.get("insider") or self._form4_detail(form, url)
        if insider:
            title_bits = [f"[{form}]", company, "-", insider["label"]]

        return self.make_item(
            external_id=external_id,
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
                # Named provenance for the drill-down. "SEC EDGAR" alone does not
                # tell a reader which company's filing list this came off, and
                # the CIK is what they need to look it up themselves.
                "feed_label": f"SEC submissions feed for {ticker} (CIK {cik_int})",
                # A grant-only Form 4 is routine paperwork. Left as plain "4" it
                # scored like an open-market purchase and took the top of the
                # feed; ROUTINE_FORM4 carries a hard cap in scoring.yaml.
                "form_type": (insider["form_type"] if insider else form),
                "accession": accession,
                "cik": cik_int,
                "items": item_codes,
                **({"insider": insider} if insider else {}),
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
            query = f'"{_fulltext_name(tc.name)}"'
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
                "feed_label": f"SEC full-text search for {query}",
                "seed_why": (f"{filer}'s own {form} filing contains {query}; this "
                             f"is somebody else's document, not {ticker}'s"),
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


ROUTINE_FORM4 = "4-ROUTINE"

# Form 4 Table I transaction codes. Only open-market buying and selling carries
# a signal; the rest is compensation plumbing.
FORM4_CODES = {
    "P": ("open-market BUY", True),
    "S": ("open-market SELL", True),
    "A": ("grant/award", False),
    "M": ("option exercise", False),
    "F": ("tax withholding", False),
    "G": ("gift", False),
    "D": ("disposition to issuer", False),
    "C": ("conversion", False),
    "X": ("option exercise", False),
}
_TAG = r"<{0}>\s*(?:<value>)?\s*([^<\s][^<]*?)\s*(?:</value>)?\s*</{0}>"

# Table I (nonDerivative) and Table II (derivative) rows. The tag name is
# captured so each row's shares are attributed to the table they came from.
_TXN_RE = re.compile(
    r"<(?P<table>nonDerivative|derivative)Transaction\b[^>]*>"
    r"(?P<row>.*?)</(?P=table)Transaction>",
    re.DOTALL | re.IGNORECASE,
)


def _form4_transactions(body: str) -> tuple[list[str], float]:
    """Transaction codes, and a share count that is not counted twice.

    ``<transactionShares>`` appears in BOTH tables of a Form 4, and an option
    exercise is reported in both: once as the derivative exercised (Table II)
    and once as the underlying stock acquired (Table I). Summing a flat regex
    over the whole document therefore added the same shares twice - TEVA's four
    most recent Form 4s printed "option exercise 43,478 sh" against a true
    21,739, and 28,984 against a true 14,492. Doubling an insider's size is the
    one number in that headline a reader would act on.

    Table I is the stock that actually moved, so it wins whenever it exists; a
    filing carrying only Table II (an option grant) has no other figure to give.
    """
    codes: list[str] = []
    totals = {"nonderivative": 0.0, "derivative": 0.0}
    tables: set[str] = set()

    for match in _TXN_RE.finditer(body):
        table = match.group("table").lower()
        row = match.group("row")
        tables.add(table)
        code = re.search(_TAG.format("transactionCode"), row)
        if code:
            codes.append(code.group(1))
        for value in re.findall(_TAG.format("transactionShares"), row):
            try:
                totals[table] += float(value)
            except ValueError:
                continue

    if not tables:
        # No transaction rows recognised - the document is shaped differently
        # than we expect. Keep the flat code scan, because separating a grant
        # from a purchase is the whole point of this fetch, but report no share
        # count rather than guess which table a loose number belongs to.
        return re.findall(_TAG.format("transactionCode"), body), 0.0

    return codes, totals["nonderivative" if "nonderivative" in tables else "derivative"]


def _fulltext_name(name: str) -> str:
    """The phrase to search for inside *other* issuers' filings.

    Dropping the legal suffix keeps the search on the distinctive name rather
    than the ticker - "ICL" or "ORA" inside a filing is almost always something
    else. But when the suffix is all that separates the name from an ordinary
    English word, dropping it creates exactly the problem it was avoiding:
    "NICE Ltd" became "NICE" and "Allot Ltd" became "Allot", so any filing
    using the word "nice" or "allotment" was collected as a peer mention. That
    was 110 of ALLT's links and 19 of NICE's, including sovereign bond
    prospectuses and a mortgage trust.

    A single bare token is the risky shape, so those keep their suffix.
    """
    stripped = name
    for suffix in (" Ltd", " Ltd.", " Inc", " Inc.", " Corp", " Corp.", " plc", " N.V."):
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
            break
    stripped = stripped.strip().rstrip(",")
    if len(stripped.split()) < 2:
        return name.strip()
    return stripped


def _parse_edgar_dt(value: str) -> datetime | None:
    """Parse an EDGAR timestamp to UTC.

    ``acceptanceDateTime`` ends in "Z" but the clock convention is **mixed**:
    some values are genuinely Eastern despite the Z, others are true UTC.
    Probing the filing index against the API made both kinds concrete - a TEVA
    8-K's ``07:00:20Z`` matched the index's "Accepted 07:00:20 ET" (Eastern
    mislabeled as UTC; 03:00 ET would be outside EDGAR's 06:00-22:00 acceptance
    window), while an ORA 13G's ``15:20:56Z`` matched "Accepted 11:20:56 ET"
    (true UTC). Reading a mislabeled stamp as UTC moves the filing 4-5 hours
    earlier - a Form 4 accepted at 16:13 ET lands at 12:13 ET and reads as the
    cause of that day's move. Reading a true-UTC stamp as Eastern moves it 4-5
    hours *later*, into the future: it holds the maximum recency-decay score
    until the clock catches up and earns intraday timing boosts for the wrong
    window (one stored filing entered the DB 86 minutes before its own
    collection time).

    Eastern stays the default, disambiguated by a one-directional invariant:
    the Eastern reading of a genuine Eastern stamp is the true acceptance time
    and can never be in the future, so an Eastern reading ahead of the wall
    clock proves the stamp was true UTC. A raw clock strictly inside
    (22:00, 02:00) is likewise impossible as an Eastern acceptance regardless
    of how late we parse it, so it is UTC too. The residual - a true-UTC stamp
    first parsed hours after acceptance with a clock outside that window - is
    undetectable and stays 4-5 hours late; recency decay has flattened those.

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
        dt_et = naive.replace(tzinfo=EDGAR_TZ).astimezone(timezone.utc)
        if fmt == "%Y-%m-%d":
            return dt_et
        # 22:00:00 exactly is a valid last-second Eastern acceptance, and a
        # true-UTC 02:00:00 is 22:00 ET, so both endpoints stay out of the
        # forced-UTC window.
        in_dead_window = (
            naive.hour in (23, 0, 1)
            or (naive.hour == 22 and (naive.minute, naive.second, naive.microsecond) != (0, 0, 0))
        )
        if in_dead_window:
            return naive.replace(tzinfo=timezone.utc)
        if dt_et > datetime.now(timezone.utc) + timedelta(minutes=10):
            return naive.replace(tzinfo=timezone.utc)
        return dt_et
    return None


_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _severity_rank(sev: str) -> int:
    return _SEVERITY_ORDER.get(sev, 1)
