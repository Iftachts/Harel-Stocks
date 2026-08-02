"""FDA collectors (openFDA + a guarded HTML fallback).

Why this matters for *indirect* news: a competitor's ANDA approval on a molecule
Teva sells is a Teva event; a warning letter at a rival plasma plant is a Kamada
event. openFDA lets us pull the whole day's activity and match it against both
our issuers and their named competitors, which is exactly the read-across
Bloomberg does with its entity graph.

Caveat, stated plainly: openFDA is a *batch* source, typically a day behind the
FDA website. It is the reliable structured backfill, not the fast lane. The fast
lane is fda_press (RSS) plus the issuer's own PR.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from ..enrich.linker import AMBIGUOUS_NAMES, _word_re_with_context
from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

MAX_LIMIT = 1000


@register("openfda")
class OpenFdaCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        endpoints = {"primary": self.source.base_url}
        endpoints.update(self.source.raw.get("extra_endpoints") or {})

        for label, url in endpoints.items():
            if not url:
                continue
            try:
                yield from self._collect_endpoint(url, label)
            except HttpError as exc:
                self.warn(f"{label}: {exc}")
            except Exception as exc:
                self.warn(f"{label}: unexpected {type(exc).__name__}: {exc}")

    def _collect_endpoint(self, url: str, label: str) -> Iterator[RawItem]:
        dataset = _dataset_of(url)
        date_field = {
            "drugsfda": "submissions.submission_status_date",
            "enforcement": "report_date",
            "510k": "decision_date",
        }.get(dataset, "receivedate")

        # openFDA is a batch source and every dataset lands on its own delay, so
        # the window has to be padded by more than that delay or the query asks
        # for days the dataset does not have yet. Measured 2026-08-01 against a
        # live API: drugsfda 3 days behind, both enforcement sets 10, 510k 15.
        # The old flat 4-day pad meant enforcement and 510k could never match
        # anything - they were returning zero on their own terms, not only
        # because of the HTTP 500 below. Padding is cheap: the deduper drops
        # records already stored, so a wide window costs one request, not rows.
        lag_days = {"drugsfda": 10, "enforcement": 21, "510k": 30}.get(dataset, 14)
        start = (self.ctx.since - timedelta(days=lag_days)).strftime("%Y%m%d")
        end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")

        params = {
            # openFDA's documented range syntax is `field:[start+TO+end]`, but
            # that `+` is URL encoding for a space - it belongs to the wire, not
            # to the value. Passing it through a params dict escapes it to %2B,
            # so the server received a literal plus and Lucene answered HTTP 500
            # (`Encountered "]" ... was expecting "TO"`) for every FDA endpoint.
            # Write the space; requests does the encoding.
            "search": f"{date_field}:[{start} TO {end}]",
            "limit": str(min(MAX_LIMIT, 500)),
        }
        key = self.source.api_key
        if key:
            params["api_key"] = key

        resp = self.client.get(url, params=params, allow_status=(404, 400))
        if resp.status >= 400:
            # openFDA answers 404 for "no matches", which is not an error.
            if resp.status == 404:
                return
            self.warn(f"{label}: HTTP {resp.status}")
            return

        results = (resp.json() or {}).get("results") or []
        matcher = _EntityMatcher(self)

        for record in results:
            try:
                item = self._record_to_item(record, dataset, matcher, url)
            except Exception as exc:
                self.warn(f"{label}: bad record ({exc})")
                continue
            if item is not None:
                yield item

    # -- record shaping ---------------------------------------------------- #
    def _record_to_item(self, rec: dict, dataset: str, matcher: "_EntityMatcher",
                        url: str) -> RawItem | None:
        if dataset == "enforcement":
            firm = rec.get("recalling_firm") or ""
            product = rec.get("product_description") or ""
            reason = rec.get("reason_for_recall") or ""
            classification = rec.get("classification") or ""
            haystack = f"{firm} {product}"
            hits = matcher.match(haystack)
            if not hits:
                return None
            ident = rec.get("recall_number") or rec.get("event_id") or f"{firm}:{product[:40]}"
            published = _fda_date(rec.get("report_date") or rec.get("recall_initiation_date"))
            return self.make_item(
                external_id=f"enforcement:{ident}",
                title=f"[FDA RECALL {classification}] {firm}: {product[:140]}",
                url="https://www.accessdata.fda.gov/scripts/ires/index.cfm",
                summary=f"Reason: {reason} | Status: {rec.get('status')} | "
                        f"Distribution: {rec.get('distribution_pattern', '')[:200]}",
                published_at=published,
                seed_tickers=[t for t, _, _ in hits],
                seed_relation="SECTOR_REG",
                meta={
                    "dataset": "enforcement",
                    "classification": classification,
                    "firm": firm,
                    "match_reasons": [why for _, _, why in hits],
                    "relations": {t: rel for t, rel, _ in hits},
                },
            )

        if dataset == "510k":
            applicant = rec.get("applicant") or ""
            device = rec.get("device_name") or ""
            hits = matcher.match(f"{applicant} {device}")
            if not hits:
                return None
            k_number = rec.get("k_number") or ""
            return self.make_item(
                external_id=f"510k:{k_number}",
                title=f"[FDA 510(k)] {applicant}: {device}",
                url=f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID={k_number}",
                summary=f"Decision: {rec.get('decision_description')} on "
                        f"{rec.get('decision_date')}. Product code "
                        f"{rec.get('product_code')}, class {rec.get('device_class')}.",
                published_at=_fda_date(rec.get("decision_date")),
                seed_tickers=[t for t, _, _ in hits],
                seed_relation="SECTOR_REG",
                meta={
                    "dataset": "510k",
                    "k_number": k_number,
                    "applicant": applicant,
                    "match_reasons": [why for _, _, why in hits],
                    "relations": {t: rel for t, rel, _ in hits},
                },
            )

        # drugsfda
        sponsor = rec.get("sponsor_name") or ""
        openfda = rec.get("openfda") or {}
        brands = openfda.get("brand_name") or []
        generics = openfda.get("generic_name") or []
        substances = openfda.get("substance_name") or []
        haystack = " ".join([sponsor, *brands, *generics, *substances])
        hits = matcher.match(haystack)
        if not hits:
            return None

        approvals = [
            s for s in (rec.get("submissions") or [])
            if str(s.get("submission_status", "")).upper() == "AP"
        ]
        if not approvals:
            return None
        latest = max(approvals, key=lambda s: str(s.get("submission_status_date") or ""))

        app_no = rec.get("application_number") or ""
        label = brands[0] if brands else (generics[0] if generics else app_no)
        sub_type = latest.get("submission_type", "")
        return self.make_item(
            external_id=f"drugsfda:{app_no}:{latest.get('submission_number')}",
            title=f"[FDA APPROVAL] {sponsor}: {label} ({app_no}, {sub_type})",
            url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event="
                f"overview.process&ApplNo={re.sub(r'^[A-Za-z]+', '', app_no)}",
            summary=f"Sponsor {sponsor}. Substances: {', '.join(substances[:5])}. "
                    f"Submission {sub_type} #{latest.get('submission_number')} approved "
                    f"{latest.get('submission_status_date')}.",
            published_at=_fda_date(latest.get("submission_status_date")),
            seed_tickers=[t for t, _, _ in hits],
            seed_relation="SECTOR_REG",
            meta={
                "dataset": "drugsfda",
                "application_number": app_no,
                "sponsor": sponsor,
                "brand_names": brands,
                "substances": substances,
                "submission_type": sub_type,
                "match_reasons": [why for _, _, why in hits],
                "relations": {t: rel for t, rel, _ in hits},
            },
        )


@register("html_table")
class HtmlListingCollector(Collector):
    """Best-effort scraper for regulator pages that publish no feed or API.

    Deliberately dumb: pull every anchor, keep the ones whose text names an
    entity we care about. It cannot break the run - worst case it yields
    nothing and records a warning. Marked ``fragile: true`` in sources.yaml.
    """

    LINK_RE = re.compile(
        r'<a\s[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<text>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})|(\d{2}/\d{2}/20\d{2})")

    # How far either side of a link to look for its row's date. A warning-letter
    # row is a few hundred characters of table markup; more than that and we
    # start reading the neighbouring row's date.
    DATE_WINDOW = 400

    def collect(self) -> Iterator[RawItem]:
        url = self.source.base_url
        if not url:
            return
        try:
            resp = self.client.get(url, allow_status=(403, 404, 500))
        except HttpError as exc:
            self.warn(f"{url}: {exc}")
            return
        if resp.status >= 400:
            self.warn(f"{url}: HTTP {resp.status} - page layout or access changed")
            return

        matcher = _EntityMatcher(self)
        now = datetime.now(timezone.utc)
        seen: set[str] = set()

        for match in self.LINK_RE.finditer(resp.text):
            text = re.sub(r"<[^>]+>", " ", match.group("text"))
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 6:
                continue
            hits = matcher.match(text)
            if not hits:
                continue
            href = match.group("href")
            if href in seen:
                continue
            seen.add(href)
            if href.startswith("/"):
                href = "https://www.fda.gov" + href
            external_id = f"html:{self.source.key}:{href}"

            published = self._row_date(resp.text, match.start(), match.end(), now)
            dated = published is not None
            if not dated:
                # Stamping "now" is not a neutral default here. external_id is
                # the href, so the uid is stable, and upsert refreshes
                # published_at on every pass: a warning letter issued 2026-03-15
                # and first collected 2026-08-01 was re-stamped every pass
                # forever, so its age stayed at zero, its recency decay stayed
                # at 1.0, and it could never fall out of a recency-ordered feed
                # or a since_hours window. Freeze the first stamp we invented
                # and mark it undated - the convention the feed already renders
                # as "date unknown, seen <when>" and the scorer already caps.
                prior = self.db.stored_meta(self.source.key, external_id) or {}
                published = _stored_dt(prior.get("first_seen")) or now
            yield self.make_item(
                external_id=external_id,
                title=f"[{self.source.label}] {text[:180]}",
                url=href,
                summary=f"Matched on: {'; '.join(why for _, _, why in hits)}",
                published_at=published,
                seed_tickers=[t for t, _, _ in hits],
                seed_relation="SECTOR_REG",
                meta={
                    "scraped": True,
                    "fragile_source": True,
                    **({"listing_date": published.date().isoformat()} if dated
                       else {"undated": True, "first_seen": published.isoformat()}),
                    "relations": {t: rel for t, rel, _ in hits},
                },
            )

    def _row_date(self, page: str, start: int, end: int,
                  now: datetime) -> datetime | None:
        """The date this listing row states, or None if it states none.

        DATE_RE was compiled for exactly this and then referenced nowhere, so
        every scraped letter was dated by the collection time instead. These
        pages put the date in sibling cells of the same table row, so the
        nearest date *before* the anchor is the row's own; a page that puts it
        after the link is read the other way round. A future date is refused -
        that is a response deadline, not a publication date.
        """
        before = page[max(0, start - self.DATE_WINDOW):start]
        after = page[end:end + self.DATE_WINDOW]
        candidates = list(self.DATE_RE.finditer(before))[::-1]
        candidates += list(self.DATE_RE.finditer(after))

        for found in candidates:
            parsed = _listing_dt(found.group(0))
            if parsed is not None and parsed <= now:
                return parsed
        return None


# --------------------------------------------------------------------------- #
class _EntityMatcher:
    """Matches free text against our issuers, their peers and their products."""

    def __init__(self, collector: Collector) -> None:
        self.rules: list[tuple[re.Pattern[str], str, str, str]] = []
        cfg = collector.cfg
        for ticker in cfg.active_tickers:
            tc = cfg.ticker(ticker)
            if not tc:
                continue
            for name in tc.match_names:
                if len(name) < 4 or name == ticker:
                    continue
                # A name that is also an ordinary word needs corporate context,
                # exactly as `EntityLinker` requires it - this matcher is a
                # second, simpler implementation of the same question and had
                # never been told. openFDA's `recalling_firm` is a full legal
                # name, so the trap is other companies rather than English:
                # "Nova Biomedical Corporation" (24 device recalls) and "Nova
                # Products, Inc." (8 drug recalls) both filed as Nova Ltd's own
                # news at DIRECT. The guard reads the word after the name, which
                # is what separates "Nova Ltd" from "Nova Biomedical".
                pattern = (_word_re_with_context(name)
                           if name.lower() in AMBIGUOUS_NAMES else _word_re(name))
                self.rules.append(
                    (pattern, ticker, "DIRECT", f"names {name}")
                )
            for name in tc.peer_names:
                # The only matcher loop here that had no length guard, against
                # the 26 peer names in universe.yaml shorter than five
                # characters. "Nova" means Nova Ltd to us; to openFDA it is also
                # Nova Biomedical (24 device recalls) and Nova Products (8 drug
                # recalls), so a blood-glucose-meter recall was emitted as
                # competitor news for Camtek, a semiconductor inspection
                # company. Its three siblings below and rss._cross_read_terms
                # already draw the line at five over the same lists.
                if len(name) < 5:
                    continue
                peer_pattern = (_word_re_with_context(name)
                                if name.lower() in AMBIGUOUS_NAMES
                                else _word_re(name))
                self.rules.append(
                    (peer_pattern, ticker, "PEER", f"competitor {name}")
                )
            for term in tc.product_terms:
                if len(term) >= 5:
                    self.rules.append(
                        (_word_re(term), ticker, "DIRECT", f"our product {term}")
                    )
            for term in tc.competitor_products:
                if len(term) >= 5:
                    self.rules.append(
                        (_word_re(term), ticker, "PRODUCT_RIVAL",
                         f"competing product {term}")
                    )

    def match(self, text: str) -> list[tuple[str, str, str]]:
        """Return [(ticker, relation, why)], strongest relation per ticker."""
        best: dict[str, tuple[str, str]] = {}
        rank = {"DIRECT": 3, "PRODUCT_RIVAL": 2, "PEER": 1}
        for pattern, ticker, relation, why in self.rules:
            if pattern.search(text):
                current = best.get(ticker)
                if current is None or rank.get(relation, 0) > rank.get(current[0], 0):
                    best[ticker] = (relation, why)
        return [(t, rel, why) for t, (rel, why) in best.items()]


def _word_re(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def _dataset_of(url: str) -> str:
    if "drugsfda" in url:
        return "drugsfda"
    if "enforcement" in url:
        return "enforcement"
    if "510k" in url:
        return "510k"
    return "unknown"


def _listing_dt(value: str) -> datetime | None:
    """A date lifted off a scraped listing row. These are US regulator pages,
    so a slashed date is month-first; day-first is only tried for the ones that
    cannot be read that way (13/03/2026 and later in the month)."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _stored_dt(value: str | None) -> datetime | None:
    """The stamp we invented for this item on an earlier pass, if we still hold
    it. Re-inventing it every pass is what kept undated items permanently new."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fda_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    value = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)
