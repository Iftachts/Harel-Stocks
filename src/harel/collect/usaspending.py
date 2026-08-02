"""Federal contract awards, from the award database rather than a press feed.

`dod_contracts` is the DoD's daily press *release* - 2 stored items - and it
carries no civilian-agency award by construction. The consequence was measured
before this module existed: **$302.0M of 2026 CBP awards to ELBITAMERICA reached
this system through nothing at all.**

    2026-03-20  $215,851,905  70B02C26C00000008  PERSISTENT SURVEILLANCE AND DETECTION EXTENSION
    2026-06-01  $ 86,139,426  70B02C26F00000256  CONSOLIDATE TOWERS AND SURVEILLANCE EQUIPMENT

USASpending is the system of record behind FPDS: free, keyless, current to the
same day (`GET /api/v2/awards/last_updated/` said 08/02/2026 on 2026-08-02) and
roughly 1-3 days behind signature.

Two failure modes drove the whole design, and both are silent by default:

* **An unknown field returns `null`, it does not error.** `Award Amt` and
  `Total Obligated Amount` both came back 200 with the value null on every row.
  A rename of `Award Amount` upstream would therefore give us null amounts
  forever and nothing would look wrong. Hence `_check_required_fields`.
* **An unknown filter KEY is dropped and the query becomes a firehose.**
  Misspelling `recipient_search_text` returned 100 awards led by HUMANA at
  $51.3bn and three Department of Energy labs - every one of which this
  collector would otherwise have stamped `seed_tickers=["ESLT"]` at DIRECT. The
  response does say so, in `messages`, and that check is not optional. A batch
  carrying it is discarded whole, because a query that was not the query we
  asked cannot be filtered back into one.

Note which mistakes are *loud* already, so the guards stay aimed at the quiet
ones: a bad `date_type` value is 422, `limit: 101` is 422, and a `sort` naming a
field absent from `fields` is 400.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ..http import HttpError
from ..models import RawItem
from .base import Collector, register

# One request per recipient per pass, ~1.3-2.2s each against a 2 req/s default
# rate limit: two names is ~4s. The caps bound what a config change can cost.
MAX_RECIPIENTS_PER_RUN = 8
# 300 awards per recipient. The backfill pass genuinely reaches this cap - Elbit
# returned a full 300 over 365 days - so it truncates, and the direction matters:
# the query sorts by Award Amount DESCENDING, so what is dropped is always the
# smallest. Reversing that sort would silently start discarding the $215.9M row
# and keeping wiring-harness orders.
MAX_PAGES_PER_RECIPIENT = 3
PAGE_LIMIT = 100                  # 101 is a 422; this is the API's own ceiling

# Fields requested by name. `sort` must name one of these or the API 400s, which
# is a useful accident: a rename of the sort key fails loudly rather than null.
FIELDS = [
    "Award ID", "Recipient Name", "Recipient UEI", "Award Amount",
    "Awarding Agency", "Awarding Sub Agency", "Description",
    "Base Obligation Date", "Last Modified Date", "generated_internal_id",
]
# The fields an item cannot be built without. Checked for null-on-most-rows
# because that is what a rename looks like from here - see the module docstring.
REQUIRED_FIELDS = ("generated_internal_id", "Recipient Name", "Award Amount",
                   "Base Obligation Date")
NULL_FIELD_RATIO = 0.5

# The exact sentence the API uses to report a filter it threw away. Matched on
# the stable fragment rather than the whole line, which names the offending key
# and so is never twice the same.
IGNORED_FILTER_MARKER = "were not used"

# Awards are lumpy: a name can go months with nothing, and a zero must not read
# as a broken parser. Before warning about a pass that found nothing anywhere,
# spend ONE request over this window to tell "quiet" from "the query shape has
# stopped matching".
CANARY_DAYS = 730

MAX_SUMMARY_CHARS = 600


@register("usaspending")
class UsaSpendingCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        plan = self._recipient_plan()
        if not plan:
            return
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        self._error: str | None = None
        found = 0
        failed = 0

        for ticker, query, accept, ueis in plan:
            label = f"{ticker} federal awards"
            try:
                for item in self._read_recipient(ticker, query, accept, ueis,
                                                 label, now):
                    found += 1
                    yield item
            except HttpError as exc:
                failed += 1
                self.warn(f"{label}: {exc}")
            except Exception as exc:  # one recipient must not lose the pass
                failed += 1
                self.warn(f"{label}: unexpected {type(exc).__name__}: {exc}")

        if failed:
            self._record_error(f"{failed} of {len(plan)} recipients failed")
        elif found == 0 and not self._error and not self._canary_finds_anything(
                plan[0], now):
            # Only when nothing has already explained the zero. A pass whose
            # filter was discarded finds nothing BECAUSE of that, and running
            # the canary anyway spends a request to rediscover it and then
            # reports "canary window empty" - which sends the next reader
            # looking at the recipient names instead of at the request body.
            # Nothing recent AND nothing in two years. One of those is a quiet
            # quarter; both together is a query that no longer matches anything.
            self.warn(f"no awards for any configured recipient, and the "
                      f"{CANARY_DAYS}-day canary for {plan[0][0]} is empty too - "
                      f"the filter shape or the recipient names have changed")
            self._record_error("canary window empty")

        if self._error:
            # NOT last_ok_at, and NOT last_error=None. A recipient answering
            # normally does not vouch for one that reported null amounts or a
            # discarded filter; clearing the error here would have erased those
            # findings a few lines after making them, and `harel doctor` would
            # show a healthy source that had just said it could not trust its
            # own data. Found by the two guard tests, which is what they are for.
            self.save_state(last_run_at=stamp, last_error=self._error,
                            items_last_run=found,
                            consecutive_failures=int(
                                self.state().get("consecutive_failures") or 0) + 1)
            return
        self.save_state(last_run_at=stamp, last_ok_at=stamp, last_error=None,
                        consecutive_failures=0, items_last_run=found)

    def _record_error(self, message: str) -> None:
        """Record against the source now AND remember it for the end of the
        pass, so the bookkeeping in `collect` cannot overwrite it.

        The FIRST error of a pass is the one kept. Later ones are usually its
        consequences - a discarded filter makes every downstream recipient look
        empty too - and `harel doctor` shows one line, which should be the cause
        rather than the last thing to notice it.
        """
        if self._error is None:
            self._error = message
        self.save_state(last_error=self._error)

    # -- configuration ----------------------------------------------------- #
    def _recipient_plan(self) -> list[tuple[str, str, list[str], list[str]]]:
        """(ticker, query string, accept names, accept UEIs).

        One request per ticker rather than one OR'd query for all of them, so
        attribution is what the config declared and never re-derived from a
        recipient name we would then have to map back to a ticker.
        """
        if self.source.raw.get("recipients_from") != "universe.federal_recipients":
            return []
        plan: list[tuple[str, str, list[str], list[str]]] = []
        for ticker in self.active_tickers:
            tc = self.cfg.ticker(ticker)
            if not tc:
                continue
            block = tc.raw.get("federal_recipients") or {}
            queries = [str(q) for q in (block.get("query") or []) if q]
            accept = [str(a).upper() for a in (block.get("accept") or []) if a]
            ueis = [str(u).upper() for u in (block.get("uei") or []) if u]
            if not queries or not (accept or ueis):
                # An empty accept list would accept everything the fuzzy search
                # returns. Refuse rather than guess.
                if queries:
                    self.warn(f"{ticker}: federal_recipients has a query but no "
                              f"accept/uei list, so every fuzzy match would be "
                              f"stamped {ticker} - skipped")
                continue
            for query in queries:
                plan.append((ticker, query, accept, ueis))
        return plan[:MAX_RECIPIENTS_PER_RUN]

    # -- fetching ---------------------------------------------------------- #
    def _read_recipient(self, ticker: str, query: str, accept: list[str],
                        ueis: list[str], label: str,
                        now: datetime) -> Iterator[RawItem]:
        # `last_modified_date` makes the ordinary pass incremental: awards
        # TOUCHED in the window, which is what catches a months-old award being
        # modified today. The cost is that history is invisible to it - the
        # $215.9M CBP award was last modified in May, so a 14-day window on a
        # fresh database would have missed the very thing this source is for.
        # One wider pass while there is no state, then incremental forever.
        first_run = not self.state().get("last_ok_at")
        lookback = int(self.source.raw.get(
            "backfill_days" if first_run else "lookback_days") or 14)
        start = (now - timedelta(days=lookback)).date().isoformat()
        rows: list[dict[str, Any]] = []

        for page in range(1, MAX_PAGES_PER_RECIPIENT + 1):
            payload = self._search(query, start, now.date().isoformat(), page)
            if payload is None:
                return                     # an ignored filter; already reported
            batch = payload.get("results") or []
            rows.extend(batch)
            if not (payload.get("page_metadata") or {}).get("hasNext"):
                break

        if not rows:
            return
        self._check_required_fields(rows, label)

        accepted = rejected = 0
        for row in rows:
            if not self._is_ours(row, accept, ueis):
                rejected += 1
                continue
            accepted += 1
            try:
                item = self._row_to_item(row, ticker, now)
            except Exception as exc:
                self.warn(f"{label}: bad award row ({type(exc).__name__}: {exc}) "
                          f"- {str(row.get('Award ID'))[:40]!r}")
                continue
            if item is not None:
                yield item

        if accepted == 0 and rejected:
            # The fuzzy search answered, and none of it was us. Either the
            # subsidiary doing the contracting is missing from `accept` - which
            # is how 83% of TAT's awards would have been lost - or the query is
            # now pointed at somebody else entirely.
            self.warn(f"{label}: {rejected} awards returned for {query!r} and "
                      f"none matched accept={accept} / uei={ueis}")
            self._record_error(f"{label}: {rejected} rows, none accepted")

    def _search(self, query: str, start: str, end: str,
                page: int) -> dict[str, Any] | None:
        """One POST. None means the response cannot be trusted at all."""
        body = {
            "filters": {
                "recipient_search_text": [query],
                "award_type_codes": list(self.source.raw.get("award_type_codes")
                                         or ["A", "B", "C", "D"]),
                # date_type is the incremental key: awards *touched* in the
                # window, which is what catches a months-old award being
                # modified today. Omitting it silently falls back to action_date
                # and the window changes meaning without any error.
                "time_period": [{"start_date": start, "end_date": end,
                                 "date_type": "last_modified_date"}],
            },
            "fields": list(FIELDS),
            "limit": PAGE_LIMIT,
            "page": page,
            "sort": "Award Amount",
            "order": "desc",
            # Self-reported by primes, months late, and not an award action.
            "subawards": False,
        }
        resp = self.client.post(self.source.base_url, json=body)
        payload = resp.json() or {}
        if self._filters_were_ignored(payload):
            return None
        return payload

    def _filters_were_ignored(self, payload: dict[str, Any]) -> bool:
        """The non-negotiable guard. See the module docstring for the firehose.

        `messages` is not empty in the healthy case - it always carries a note
        about the 2007-10-01 floor - so this matches the one sentence that means
        a filter was discarded, and nothing else.
        """
        ignored = [str(m) for m in (payload.get("messages") or [])
                   if IGNORED_FILTER_MARKER in str(m)]
        if not ignored:
            return False
        self.warn(f"USASpending discarded a filter and answered a different "
                  f"question - every award it returned is unrelated and none of "
                  f"it can be attributed: {ignored[0][:200]}")
        self._record_error(f"filter ignored by the API: {ignored[0][:150]}")
        return True

    def _check_required_fields(self, rows: list[dict[str, Any]],
                               label: str) -> None:
        """A renamed field is null on every row and 200 on every request."""
        for field in REQUIRED_FIELDS:
            missing = sum(1 for row in rows if row.get(field) is None)
            if missing > len(rows) * NULL_FIELD_RATIO:
                self.warn(f"{label}: {field!r} is null on {missing} of "
                          f"{len(rows)} awards - the API has renamed or dropped "
                          f"it, and amounts read from here are not real")
                self._record_error(
                    f"{label}: {field!r} null on {missing}/{len(rows)} rows")

    def _canary_finds_anything(self, first: tuple[str, str, list[str], list[str]],
                               now: datetime) -> bool:
        """One extra request before crying wolf about an empty pass."""
        _, query, _, _ = first
        start = (now - timedelta(days=CANARY_DAYS)).date().isoformat()
        try:
            payload = self._search(query, start, now.date().isoformat(), 1)
        except Exception as exc:
            self.warn(f"canary request for {query!r} failed: "
                      f"{type(exc).__name__}: {exc}")
            return False
        return bool(payload and payload.get("results"))

    # -- one award --------------------------------------------------------- #
    def _is_ours(self, row: dict[str, Any], accept: list[str],
                 ueis: list[str]) -> bool:
        """Two independent identities, either of which is enough.

        The UEI is exact and survives a legal-name change; the name substring
        survives a subsidiary being issued a new UEI. A row satisfying neither
        is dropped outright - see the note in universe.yaml about why it is
        never merely downgraded.
        """
        if str(row.get("Recipient UEI") or "").upper() in ueis:
            return True
        name = str(row.get("Recipient Name") or "").upper()
        return any(token in name for token in accept)

    def _row_to_item(self, row: dict[str, Any], ticker: str,
                     now: datetime) -> RawItem | None:
        gid = str(row.get("generated_internal_id") or "").strip()
        amount = _as_float(row.get("Award Amount"))
        if not gid or amount is None:
            return None

        cents = int(round(amount * 100))
        # The uid carries the money, which is the entire revision design: a
        # re-fetch is the same id and so is not a second item; a modification
        # that changes the obligation is a new one; an administrative
        # modification that only bumps `Last Modified Date` is not. That last
        # case is real and common - a 2022 F-16 display-unit repair was
        # re-stamped 2026-07-28 with its $50,306.40 untouched.
        external_id = f"{gid}#{cents}"
        state_key = f"{self.source.key}:{gid}"
        prior_cents = _as_int(self.db.get_source_state(state_key).get("cursor"))
        self.db.set_source_state(state_key, cursor=str(cents),
                                 last_run_at=now.isoformat())

        # Whether THIS id was first published as a revision is a property of the
        # id, not of when we happen to re-read it, so it is read back from the
        # item we already stored rather than recomputed. Recomputing would flip
        # published_at between the signature date and the modification stamp on
        # alternate passes.
        stored = self.db.stored_meta(self.source.key, external_id)
        if stored is not None:
            delta = _as_float(stored.get("revision_delta_usd"))
        elif prior_cents is not None and prior_cents != cents:
            delta = (cents - prior_cents) / 100.0
        else:
            delta = None

        minimum = float(self.source.raw.get("min_award_usd") or 0)
        # A revision is judged on the money that MOVED, not on the running
        # total: a $40k adjustment to a $200M contract is bookkeeping.
        if abs(delta if delta is not None else amount) < minimum:
            return None

        recipient = str(row.get("Recipient Name") or "").strip()
        agency = (str(row.get("Awarding Sub Agency") or "").strip()
                  or str(row.get("Awarding Agency") or "").strip())
        description = str(row.get("Description") or "").strip()
        award_id = str(row.get("Award ID") or "").strip()

        if delta is None:
            published = _award_date(row.get("Base Obligation Date"))
            dating: dict[str, Any] = {
                "base_obligation_date": str(row.get("Base Obligation Date") or "")}
        else:
            # A modification's own date is when it was recorded, so the stamp is
            # honest but coarse - flagged as such, exactly as other load-stamped
            # sources are.
            published = (_award_date(row.get("Last Modified Date"))
                         or _award_date(row.get("Base Obligation Date")))
            dating = {"date_is_load_stamp": True,
                      "revision_delta_usd": delta,
                      "prior_amount_usd": (prior_cents / 100.0
                                           if prior_cents is not None else None),
                      "base_obligation_date": str(
                          row.get("Base Obligation Date") or "")}
        if published is None:
            return None
        # Deliberately NOT meta["undated"]. Every award here has a real, exact
        # signature date, and UNDATED_CAP would have made an $86M award
        # unrankable.

        return self.make_item(
            external_id=external_id,
            title=_title(recipient, agency, description, amount, delta)[:300],
            url=f"https://www.usaspending.gov/award/{gid}",
            summary=_summary(row, amount, delta)[:MAX_SUMMARY_CHARS],
            published_at=published,
            seed_tickers=[ticker],
            seed_relation="DIRECT",
            meta={
                "award_id": award_id,
                "generated_internal_id": gid,
                "recipient": recipient,
                "recipient_uei": str(row.get("Recipient UEI") or ""),
                "awarding_agency": str(row.get("Awarding Agency") or ""),
                "awarding_sub_agency": str(row.get("Awarding Sub Agency") or ""),
                "award_amount_usd": amount,
                "seed_why": f"USASpending names {recipient} as the recipient",
                **dating,
            },
        )


# --------------------------------------------------------------------------- #
# Headline shape.
#
# This has to satisfy `major_contract` (scoring.yaml, base 78) or an $86M award
# scores as unclassified text. That rule wants an awarding verb within 40
# characters of contract|order|award|tender, so the amount goes between them and
# the agency and the description follow. Do not reorder these without rerunning
# the test that compiles the live rule against the output.
# --------------------------------------------------------------------------- #
def _title(recipient: str, agency: str, description: str, amount: float,
           delta: float | None) -> str:
    if delta is None:
        head = f"{recipient} awarded {_usd(amount)} contract"
    elif delta > 0:
        head = f"{recipient} awarded {_usd(delta)} contract increase"
    else:
        # A de-obligation is not a contract win and must not be scored as one,
        # so this branch deliberately does not match `major_contract`. It is
        # still material - money leaving a backlog - and reaches the reader on
        # the strength of the amount rather than the event class.
        head = f"{recipient} contract obligation cut by {_usd(abs(delta))}"
    if agency:
        head += f" from {agency}"
    if delta is not None:
        head += f" - now {_usd(amount)}"
    if description:
        head += f" - {description}"
    return f"[USASpending] {head}"


def _summary(row: dict[str, Any], amount: float, delta: float | None) -> str:
    parts = [f"Award {row.get('Award ID')}",
             f"recipient {row.get('Recipient Name')}",
             f"obligated {_usd(amount)}"]
    if delta is not None:
        parts.append(f"changed by {'+' if delta > 0 else '-'}{_usd(abs(delta))} "
                     f"since the last pass")
    if row.get("Awarding Agency"):
        parts.append(f"awarded by {row['Awarding Agency']}")
    if row.get("Base Obligation Date"):
        parts.append(f"base obligation {row['Base Obligation Date']}")
    return "; ".join(parts) + "."


def _usd(amount: float) -> str:
    """The forms `major_contract`'s money pattern recognises."""
    if abs(amount) >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f} billion"
    if abs(amount) >= 1_000_000:
        return f"${amount / 1_000_000:.1f} million"
    return f"${amount:,.0f}"


_DATE_HEAD = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _award_date(value: Any) -> datetime | None:
    """`Base Obligation Date` is a bare date; `Last Modified Date` carries a
    time. Both start with an ISO date, which is all that is wanted here.

    Base Obligation Date and not `Start Date`: the latter is the start of the
    period of performance, not the moment the money was committed, and the two
    differ - the $215.9M CBP award was signed 2026-03-20 against a 2026-03-03
    performance start.
    """
    match = _DATE_HEAD.match(str(value or "").strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
