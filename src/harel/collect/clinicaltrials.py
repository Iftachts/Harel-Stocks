"""ClinicalTrials.gov (API v2).

A registry snapshot is not news. The *change* is the news:

  Recruiting -> Active, not recruiting   enrollment complete, readout approaching
  primary completion date pulled in      readout sooner than the street thinks
  primary completion date pushed out     delay, usually a negative
  Any status -> Terminated / Suspended   often precedes the press release
  Withdrawn                              program quietly killed

So this collector keeps a fingerprint per NCT id and only emits when something
actually moved. It watches our own sponsors AND the competitor programs listed
under ``competitor_products`` in universe.yaml - a rival's Phase 3 completing is
frequently a bigger mover for CGEN or ORMP than their own press release.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

BASE = "https://clinicaltrials.gov/api/v2/studies"

FIELDS = ",".join([
    "protocolSection.identificationModule.nctId",
    "protocolSection.identificationModule.briefTitle",
    "protocolSection.statusModule.overallStatus",
    "protocolSection.statusModule.lastUpdatePostDateStruct",
    "protocolSection.statusModule.primaryCompletionDateStruct",
    "protocolSection.statusModule.completionDateStruct",
    "protocolSection.statusModule.startDateStruct",
    "protocolSection.statusModule.whyStopped",
    "protocolSection.sponsorCollaboratorsModule.leadSponsor",
    "protocolSection.designModule.phases",
    "protocolSection.designModule.enrollmentInfo",
    "protocolSection.conditionsModule.conditions",
])

PAGE_SIZE = 100

# Only these phases can move a stock. Phase 1 dose escalation rarely does.
INTERESTING_PHASES = {"PHASE2", "PHASE2_PHASE3", "PHASE3", "PHASE4"}

STATUS_MEANING = {
    "TERMINATED": ("Trial terminated", "negative"),
    "SUSPENDED": ("Trial suspended", "negative"),
    "WITHDRAWN": ("Trial withdrawn before enrollment", "negative"),
    "ACTIVE_NOT_RECRUITING": ("Enrollment complete - readout approaching", "positive"),
    "COMPLETED": ("Trial completed - data expected", "positive"),
    "RECRUITING": ("Now recruiting", "neutral"),
    "NOT_YET_RECRUITING": ("Not yet recruiting", "neutral"),
    "ENROLLING_BY_INVITATION": ("Enrolling by invitation", "neutral"),
}


@register("clinicaltrials")
class ClinicalTrialsCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        prior = self._load_fingerprints()
        current: dict[str, str] = {}

        for query, tickers, relation, label in self._query_plan():
            try:
                for study in self._search(query):
                    item = self._study_to_item(study, tickers, relation, label,
                                               prior, current)
                    if item is not None:
                        yield item
            except HttpError as exc:
                self.warn(f"{label}: {exc}")
            except Exception as exc:
                self.warn(f"{label}: unexpected {type(exc).__name__}: {exc}")

        # Keep the store bounded; a stale NCT that stopped matching can go.
        merged = {**prior, **current}
        if len(merged) > 20_000:
            merged = current
        self.save_state(cursor=json.dumps(merged))

    # -- planning ---------------------------------------------------------- #
    def _query_plan(self) -> list[tuple[dict[str, str], list[str], str, str]]:
        plan: list[tuple[dict[str, str], list[str], str, str]] = []
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            sector = self.cfg.sector(tc.sector)
            if "clinicaltrials" not in sector.regulators:
                continue

            sponsor = tc.name.split(" Ltd")[0].split(" Inc")[0].strip()
            plan.append(({"query.spons": sponsor}, [ticker], "DIRECT",
                         f"{ticker} sponsored trials"))

            # Competitor programs: the class read-across channel.
            for term in tc.competitor_products[:8]:
                plan.append(({"query.intr": term}, [ticker], "PRODUCT_RIVAL",
                             f"{ticker} rival program: {term}"))
        return plan

    def _search(self, query: dict[str, str]) -> Iterator[dict]:
        params = {
            **query,
            "fields": FIELDS,
            "pageSize": str(PAGE_SIZE),
            "countTotal": "false",
        }
        resp = self.client.get(BASE, params=params, allow_status=(400, 404))
        if resp.status >= 400:
            self.warn(f"HTTP {resp.status} for {query}")
            return
        for study in (resp.json() or {}).get("studies") or []:
            yield study

    # -- change detection -------------------------------------------------- #
    def _load_fingerprints(self) -> dict[str, str]:
        cursor = self.state().get("cursor")
        if not cursor:
            return {}
        try:
            return json.loads(cursor)
        except json.JSONDecodeError:
            return {}

    def _study_to_item(self, study: dict, tickers: list[str], relation: str,
                       label: str, prior: dict[str, str],
                       current: dict[str, str]) -> RawItem | None:
        proto = study.get("protocolSection") or {}
        ident = proto.get("identificationModule") or {}
        status_mod = proto.get("statusModule") or {}
        design = proto.get("designModule") or {}
        sponsor = ((proto.get("sponsorCollaboratorsModule") or {}).get("leadSponsor")
                   or {}).get("name", "")

        nct = ident.get("nctId")
        if not nct:
            return None

        phases = [p.upper() for p in (design.get("phases") or [])]
        if phases and not (set(phases) & INTERESTING_PHASES):
            return None

        status = str(status_mod.get("overallStatus") or "").upper()
        primary_completion = (status_mod.get("primaryCompletionDateStruct") or {}).get("date", "")
        last_update = (status_mod.get("lastUpdatePostDateStruct") or {}).get("date", "")
        why_stopped = status_mod.get("whyStopped") or ""

        fingerprint = f"{status}|{primary_completion}|{why_stopped}"
        current[nct] = fingerprint

        previous = prior.get(nct)
        if previous == fingerprint:
            return None                       # nothing moved

        # First sight of a trial is only interesting if it was updated recently.
        published = _ct_date(last_update)
        if previous is None:
            if published < self.ctx.since:
                return None
            change = "first observed"
        else:
            old_status, old_pcd, _ = (previous.split("|") + ["", "", ""])[:3]
            parts = []
            if old_status != status:
                parts.append(f"status {old_status} -> {status}")
            if old_pcd != primary_completion:
                parts.append(_completion_change(old_pcd, primary_completion))
            if why_stopped:
                parts.append(f"why stopped: {why_stopped}")
            change = "; ".join(parts) or "record updated"

        meaning, sign = STATUS_MEANING.get(status, (status.title(), "neutral"))
        enrollment = (design.get("enrollmentInfo") or {}).get("count")

        return self.make_item(
            external_id=f"ct:{nct}:{fingerprint}",
            title=f"[TRIAL {'/'.join(phases) or 'NA'}] {sponsor}: "
                  f"{ident.get('briefTitle', '')[:150]} - {change}",
            url=f"https://clinicaltrials.gov/study/{nct}",
            summary=(
                f"{meaning}. Sponsor: {sponsor}. Phases: {', '.join(phases) or 'n/a'}. "
                f"Enrollment: {enrollment}. Primary completion: {primary_completion}. "
                f"Conditions: {', '.join((proto.get('conditionsModule') or {}).get('conditions', [])[:4])}."
            ),
            published_at=published,
            seed_tickers=list(tickers),
            seed_relation=relation,
            meta={
                "nct_id": nct,
                "status": status,
                "phases": phases,
                "sponsor": sponsor,
                "primary_completion": primary_completion,
                "change": change,
                "sentiment_hint": sign,
                "query_label": label,
            },
        )


def _completion_change(old: str, new: str) -> str:
    """Describe a primary-completion-date change, and only claim a direction
    when the two dates actually establish one.

    The direction used to be decided by comparing the raw strings, and
    ClinicalTrials.gov returns this field at either month or day precision. So
    "2027-01" -> "2027-01-15" - a sponsor doing nothing but refining a month to
    a day - sorted as "pushed out" and went out as a DELAY, which the module
    docstring calls usually a negative. And clearing the field sorted as
    "pulled in": fabricated good news off a removed value. Compare at the
    coarser of the two precisions, and say "unknown" when there is nothing to
    compare.
    """
    old_dt, old_month = _ct_precision(old)
    new_dt, new_month = _ct_precision(new)
    label = f"primary completion {old or 'unspecified'} -> {new or 'unspecified'}"

    if old_dt is None or new_dt is None:
        return f"{label} (direction unknown)"
    if old_month or new_month:
        # One side only names a month, so the day the other side names carries
        # no information: compare the months they agree on stating.
        old_dt = old_dt.replace(day=1)
        new_dt = new_dt.replace(day=1)
    if new_dt == old_dt:
        return f"{label} (same date, restated more precisely)"
    return f"{label} ({'pushed out' if new_dt > old_dt else 'pulled in'})"


def _ct_precision(value: str) -> tuple[datetime | None, bool]:
    """(parsed date, is it month-precision). None when there is no date at all -
    an absent value is unknown, not an early one."""
    value = (value or "").strip()
    for fmt, month_only in (("%Y-%m-%d", False), ("%Y-%m", True)):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc), month_only
        except ValueError:
            continue
    return None, False


def _ct_date(value: str) -> datetime:
    parsed, _ = _ct_precision(value)
    return parsed if parsed is not None else datetime.now(timezone.utc)
