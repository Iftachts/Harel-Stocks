"""Bank of Israel policy rate: the next decision date and each decision.

Every Israeli bank, insurer and landlord in the basket trades on this one
number, and the Bank of Israel publishes no feed for it: its press-release and
Supervisor-of-Banks pages sit behind a bot challenge from this host, and the
ministries' gov.il pages 403 outright (checked 2026-10-07). What does answer is
the site's own JSON endpoint behind the rate widget:

    GET https://www.boi.org.il/PublicApi/GetInterest
    {"currentInterest": 3.25, "nextInterestDate": "2026-10-21T00:00:00Z",
     "lastPublishedDate": "2026-09-01T16:00:04.647Z"}

Two things come out of it. The next decision date becomes a calendar row - the
day not to be caught the wrong way into a bank - and each newly published
decision becomes an item, phrased as a cut, a hike or a hold against the rate
this collector saw last.

The tickers are not guessed from text. A sector that lists this source under
`regulators:` in sectors.yaml is a sector this decision is regulatory news for,
and every name in it is seeded SECTOR_REG.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from ..models import RawItem
from .base import Collector, register

INTEREST_URL = "https://www.boi.org.il/PublicApi/GetInterest"
# Calendar data, not a headline - capped like MAYA's schedule rows.
SCHEDULE_FORM_TYPE = "BOI-SCHEDULE"


@register("boi_interest")
class BoiInterestCollector(Collector):
    def collect(self) -> Iterator[RawItem]:
        tickers = self._regulated_tickers()
        if not tickers:
            self.warn("no sector lists this source under `regulators:` - "
                      "nothing to attach a rate decision to")
            return
        url = self.source.base_url or INTEREST_URL
        resp = self.client.get(url, allow_status=(403, 404, 429, 500))
        if resp.status >= 400:
            self.warn(f"GetInterest returned HTTP {resp.status}")
            return
        try:
            payload = resp.json() or {}
        except Exception:
            self.warn("GetInterest did not return JSON - the endpoint has "
                      "probably been put behind the site's bot challenge")
            return
        rate = payload.get("currentInterest")
        if not isinstance(rate, (int, float)):
            self.warn(f"GetInterest shape changed: {sorted(payload)}")
            return

        cursor = self._cursor()
        published = _parse(payload.get("lastPublishedDate"))
        if published and published.isoformat() != cursor.get("published"):
            item = self._decision_item(float(rate), cursor.get("rate"),
                                       published, tickers)
            # The first pass has no previous rate to compare with, and a
            # decision from weeks ago is not news: it is recorded, not emitted.
            if published >= self.ctx.since:
                yield item

        upcoming = _parse(payload.get("nextInterestDate"))
        if upcoming:
            yield self._schedule_item(upcoming, float(rate), tickers)

        self.save_state(cursor=json.dumps({
            "rate": float(rate),
            "published": published.isoformat() if published else cursor.get("published"),
        }), last_error=None)

    # ------------------------------------------------------------------ #
    def _regulated_tickers(self) -> list[str]:
        sectors = {key for key, sector in self.cfg.sectors.items()
                   if self.source.key in sector.regulators}
        return [t for t in self.active_tickers
                if self.cfg.ticker(t) and self.cfg.ticker(t).sector in sectors]

    def _cursor(self) -> dict[str, Any]:
        try:
            return json.loads(self.state().get("cursor") or "{}")
        except json.JSONDecodeError:
            return {}

    def _decision_item(self, rate: float, previous: Any, published: datetime,
                       tickers: list[str]) -> RawItem:
        if isinstance(previous, (int, float)) and previous != rate:
            verb = "cuts" if rate < previous else "raises"
            title = (f"[BOI] Bank of Israel {verb} its interest rate to {rate:.2f}% "
                     f"(from {previous:.2f}%)")
        elif isinstance(previous, (int, float)):
            title = f"[BOI] Bank of Israel holds its interest rate at {rate:.2f}%"
        else:
            title = f"[BOI] Bank of Israel interest rate decision: rate at {rate:.2f}%"
        return self.make_item(
            external_id=f"boi:decision:{published.isoformat()}",
            title=title,
            url="https://www.boi.org.il/bank-of-israel/the-monetary-committee/",
            summary=f"Bank of Israel policy rate {rate:.2f}%, published "
                    f"{published:%Y-%m-%d %H:%M} UTC.",
            published_at=published,
            lang="en",
            seed_tickers=list(tickers),
            seed_relation="SECTOR_REG",
            meta={"lock_seed_relation": True, "policy_rate": rate,
                  "previous_rate": previous},
        )

    def _schedule_item(self, when: datetime, rate: float,
                       tickers: list[str]) -> RawItem:
        date_str = when.date().isoformat()
        return self.make_item(
            external_id=f"boi:next-decision:{date_str}",
            title=f"[BOI] Bank of Israel interest rate decision expected {date_str}",
            url="https://www.boi.org.il/bank-of-israel/the-monetary-committee/",
            summary=f"Next Bank of Israel rate decision {date_str}; "
                    f"current rate {rate:.2f}%.",
            published_at=datetime.now(timezone.utc),
            lang="en",
            seed_tickers=list(tickers),
            seed_relation="SECTOR_REG",
            meta={
                "lock_seed_relation": True,
                "form_type": SCHEDULE_FORM_TYPE,
                "scheduled_report_on": date_str,
                "scheduled_kind": "rate_decision",
                "schedule_label": "Bank of Israel rate decision",
                "time_zone": "Israel",
            },
        )


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(
            timezone.utc)
    except ValueError:
        return None
