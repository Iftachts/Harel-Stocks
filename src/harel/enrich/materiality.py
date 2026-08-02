"""Materiality scoring for a short-term trader.

The question this module answers is narrow and specific:

    "Can this move the print in the next few hours?"

Not "is this interesting", not "is this important for the thesis". A patent
grant is important; it is not tradeable today. A 424B5 pricing a dilutive
offering in a $120m float microcap is not interesting; it is the whole day.

Everything is explainable: every score carries a `reasons` trace so the LLM
agent downstream can say *why* an item is at the top of the feed, and so the
trader can tune config/scoring.yaml when it gets something wrong.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..config import Config, SectorConfig
from ..models import Link, RawItem, ScoredItem
from .events import classify_events

# The exchange's own clock. "Pre-market or not" is the difference between a gap
# and a nothing, so it is resolved against the real tzdb - see _timing.
MARKET_TZ = ZoneInfo("America/New_York")

# config/sectors.yaml tunes how strongly other people's news reads across to a
# name, per sector. That is the same question `relations` answers globally, on
# the same 0-1 scale (peer couplings run 0.40-0.90 around a global PEER of
# 0.65), so the sector's number *replaces* the global default for the relations
# it describes, exactly as a per-ticker `relation_overrides` entry does.
# Multiplying the two instead would have made semicap's 0.85 - written because
# "WFE names trade almost 1:1 with peers' guidance" - lower the weight of a
# peer's guidance, to 0.55.
SECTOR_COUPLING = {
    "SECTOR_REG": "read_across",
    "SECTOR_THEME": "read_across",
    "PEER": "peer_read_across",
}

# Base score when nothing in the taxonomy matched.
DEFAULT_BASE_DIRECT = 28.0
DEFAULT_BASE_INDIRECT = 16.0

# 8-K item severity can raise a base that the regex taxonomy missed.
SEVERITY_FLOOR = {"critical": 88.0, "high": 72.0, "medium": 42.0, "low": 14.0}


@dataclass(slots=True)
class PriceContext:
    change_pct: float | None = None
    volume_multiple: float | None = None
    session: str = "unknown"


class MaterialityScorer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.scoring = config.scoring
        self._override_patterns = self._compile_overrides()

    def _compile_overrides(self) -> dict[str, list[tuple[re.Pattern[str], float]]]:
        out: dict[str, list[tuple[re.Pattern[str], float]]] = {}
        for ticker, spec in (self.scoring.overrides or {}).items():
            boosts = []
            for entry in (spec or {}).get("keyword_boosts") or []:
                try:
                    boosts.append(
                        (re.compile(entry["pattern"], re.IGNORECASE | re.UNICODE),
                         float(entry.get("boost", 0)))
                    )
                except (re.error, KeyError):
                    continue
            if boosts:
                # load_config uppercases these keys. Doing it here as well -
                # and only here - is what let a `cgen:` block apply its keyword
                # boosts under CGEN while the relation override it sits with,
                # looked up under the raw key, was silently dropped.
                out[ticker] = boosts
        return out

    # ---------------------------------------------------------------- score --
    def score(
        self,
        item: RawItem,
        links: list[Link],
        price_by_ticker: dict[str, PriceContext] | None = None,
        cluster_max_trust: float | None = None,
        now: datetime | None = None,
    ) -> ScoredItem:
        now = now or datetime.now(timezone.utc)
        price_by_ticker = price_by_ticker or {}
        reasons: list[str] = []

        hits = classify_events(item, self.config)
        event_keys = [rule.key for rule, _ in hits]

        base, base_reason = self._base_score(item, hits, links)
        reasons.append(base_reason)

        cap = self._noise_cap(item)
        if cap is not None:
            reasons.append(f"noise cap {cap:.0f} applied")

        source = self.config.sources.get(item.source)
        trust = source.trust if source else 0.6
        reasons.append(f"source trust x{trust:.2f} ({item.source})")

        decay, decay_reason = self._recency(item, now)
        reasons.append(decay_reason)

        timing_boost, timing_reason = self._timing(item, now)
        if timing_boost:
            reasons.append(timing_reason)

        per_ticker: dict[str, float] = {}
        for link in links:
            score, link_reasons = self._score_link(
                item, link, base, trust, decay, timing_boost,
                price_by_ticker.get(link.ticker), hits,
            )
            if cap is not None:
                score = min(score, cap)

            # Aggregator-only stories cannot reach ALERT on their own.
            if (trust < self.scoring.require_corroboration_below_trust
                    and (cluster_max_trust or trust) < self.scoring.require_corroboration_below_trust):
                if score > self.scoring.corroboration_cap:
                    score = self.scoring.corroboration_cap
                    link_reasons.append(
                        f"capped at {self.scoring.corroboration_cap:.0f}: "
                        f"low-trust source with no corroboration"
                    )

            forced = item.meta.get("forced_score")
            if forced is not None:
                score = max(score, float(forced))
                link_reasons.append(f"forced floor {float(forced):.0f} (synthetic tape alert)")

            per_ticker[link.ticker] = round(_clamp(score), 1)
            reasons.extend(f"[{link.ticker}] {r}" for r in link_reasons)

        best = max(per_ticker.values()) if per_ticker else 0.0
        return ScoredItem(
            raw=item,
            links=links,
            events=event_keys,
            score=best,
            per_ticker_score=per_ticker,
            tier=self.scoring.tier_for(best),
            reasons=reasons,
        )

    # -------------------------------------------------------------- pieces --
    def _base_score(self, item: RawItem, hits, links: list[Link]) -> tuple[float, str]:
        if hits:
            rule, evidence = hits[0]
            base = rule.base
            reason = f"event={rule.key} base={base:.0f} ({evidence})"
        else:
            has_direct = any(link.relation in ("DIRECT", "SUBSIDIARY") for link in links)
            base = DEFAULT_BASE_DIRECT if has_direct else DEFAULT_BASE_INDIRECT
            reason = f"no taxonomy match, default base={base:.0f}"

        severity = str(item.meta.get("item_severity") or "").lower()
        anchor = SEVERITY_FLOOR.get(severity)
        if anchor:
            labels = ", ".join(item.meta.get("item_labels") or [])
            if anchor > base:
                base = anchor
                reason += f" -> raised to {anchor:.0f} by 8-K severity '{severity}' ({labels})"
            elif severity == "low" and not hits:
                # An 8-K whose only Items are administrative (5.03 bylaws, 5.07
                # vote results, 9.01 exhibits) and whose text matched nothing in
                # the taxonomy is paperwork. Cap it, do not merely leave it.
                base = anchor
                reason += f" -> capped at {anchor:.0f}: only low-severity 8-K items ({labels})"

        # A regulator document that arrives a day early is worth more.
        if item.meta.get("public_inspection"):
            base += 6
            reason += " +6 (public inspection: ~1 business day of lead time)"

        # A MAYA report landing during Israeli hours is actionable at the US open.
        if item.meta.get("israeli_hours"):
            base += 5
            reason += " +5 (TASE disclosure ahead of the US session)"

        return base, reason

    # An entry whose feed gave no date is stamped "now" so it is not dropped.
    # That invented timestamp makes an evergreen marketing page look like it
    # broke a minute ago, at full issuer trust, so it must not be rankable.
    UNDATED_CAP = 10.0

    def _noise_cap(self, item: RawItem) -> float | None:
        caps: list[float] = []

        if item.meta.get("undated"):
            caps.append(self.UNDATED_CAP)

        form = str(item.meta.get("form_type") or "").upper()
        if form and form in self.scoring.noise_form_types:
            caps.append(self.scoring.noise_form_types[form])

        text = f"{item.title} {item.summary}"
        for noise in self.scoring.noise_title_patterns:
            if noise.pattern.search(text):
                caps.append(noise.cap)

        # Post-move commentary: written because the price already moved, so it
        # cannot be evidence of why. Matched on the TITLE only - a real filing
        # whose body happens to quote the day's move must not be demoted.
        for noise in self.scoring.reactive_patterns:
            if noise.pattern.search(item.title):
                caps.append(noise.cap)
                item.meta["reactive_recap"] = True
                break

        return min(caps) if caps else None

    def _recency(self, item: RawItem, now: datetime) -> tuple[float, str]:
        half_life = float(self.scoring.recency.get("half_life_hours", 8) or 8)
        floor = float(self.scoring.recency.get("floor", 0.25))
        age_h = max(0.0, (now - item.published_at).total_seconds() / 3600.0)
        decay = max(floor, math.pow(0.5, age_h / half_life))
        return decay, f"age {age_h:.1f}h -> recency x{decay:.2f}"

    def _timing(self, item: RawItem, now: datetime) -> tuple[float, str]:
        """News that lands pre-open creates the gap; news mid-session moves the
        tape immediately. Both beat a story that broke after the close.

        On the exchange's clock rather than `4 if 3 <= month <= 11 else 5`: DST
        ends the first Sunday of November and starts the second Sunday of
        March, so that guess was an hour out for most of November and the first
        week of March - long enough to hand the pre-market boost to a story
        published at the open, and to deny it to one published at 08:45.
        """
        et = item.published_at.astimezone(MARKET_TZ)
        if et.weekday() >= 5:
            return 0.0, ""
        minutes = et.hour * 60 + et.minute
        if 4 * 60 <= minutes < 9 * 60 + 30:
            boost = float(self.scoring.recency.get("premarket_boost", 6))
            return boost, f"+{boost:.0f} published pre-market ({et:%H:%M} ET)"
        if 9 * 60 + 30 <= minutes < 16 * 60:
            boost = float(self.scoring.recency.get("intraday_boost", 4))
            return boost, f"+{boost:.0f} published intraday ({et:%H:%M} ET)"
        return 0.0, ""

    def _score_link(self, item: RawItem, link: Link, base: float, trust: float,
                    decay: float, timing_boost: float,
                    price: PriceContext | None, hits) -> tuple[float, list[str]]:
        reasons: list[str] = []
        ticker_cfg = self.config.ticker(link.ticker)

        # How much news about something *else* moves this name. Most specific
        # wins: the ticker's own relation_override, else its sector's tuned
        # read-across, else the global default in scoring.yaml.
        relation_mult = self.scoring.relations.get(link.relation, 0.4)
        coupling = _sector_coupling(
            self.config.sector(ticker_cfg.sector) if ticker_cfg else None,
            link.relation,
        )
        if coupling is not None:
            relation_mult = coupling
        override = (self.scoring.overrides.get(link.ticker) or {}).get("relation_overrides") or {}
        if link.relation in override:
            relation_mult = float(override[link.relation])
            reasons.append(f"relation {link.relation} override x{relation_mult:.2f}")
        else:
            reasons.append(f"relation {link.relation} x{relation_mult:.2f}")

        float_class = ticker_cfg.float_class if ticker_cfg else "unknown"
        float_mult = self.scoring.float_sensitivity.get(float_class, 1.0)
        # Dilution hits small floats disproportionately.
        if any(rule.float_sensitive for rule, _ in hits) and float_class in ("micro", "small"):
            float_mult *= 1.15
            reasons.append(f"float {float_class} x{float_mult:.2f} (dilution-sensitive)")
        else:
            reasons.append(f"float {float_class} x{float_mult:.2f}")

        score = base * trust * relation_mult * float_mult * decay * link.confidence
        reasons.append(f"link confidence x{link.confidence:.2f}")

        score += timing_boost

        for pattern, boost in self._override_patterns.get(link.ticker, []):
            if pattern.search(item.text):
                score += boost
                reasons.append(f"+{boost:.0f} ticker keyword boost")

        # Tape confirmation only means something for an item that is actually
        # news. "News the tape is already confirming outranks news nothing
        # reacted to" - but a chart-generated article that matched no event at
        # all is not news, and +8 for coinciding with the move it was generated
        # from is how it climbed above a guidance raise.
        if hits:
            score += self._price_boost(price, reasons)
        return score, reasons

    def _price_boost(self, price: PriceContext | None, reasons: list[str]) -> float:
        conf = self.scoring.price_confirmation
        if not price or not conf.get("enabled", True):
            return 0.0
        total = 0.0
        if (price.change_pct is not None
                and abs(price.change_pct) >= float(conf.get("abs_move_pct_threshold", 3.0))):
            boost = float(conf.get("boost_move", 8))
            total += boost
            reasons.append(f"+{boost:.0f} tape confirms ({price.change_pct:+.1f}%)")
        if (price.volume_multiple is not None
                and price.volume_multiple >= float(conf.get("volume_multiple_threshold", 2.0))):
            boost = float(conf.get("boost_volume", 6))
            total += boost
            reasons.append(f"+{boost:.0f} volume {price.volume_multiple:.1f}x ADV")
        return total


def _sector_coupling(sector: SectorConfig | None, relation: str) -> float | None:
    """The sector's own read-across weight for this relation, if it tuned one."""
    field = SECTOR_COUPLING.get(relation)
    if sector is None or field is None:
        return None
    # 0.0 means "this sector never filled it in" (the `unknown` sector, and any
    # sector added without the field), not "a peer's news is worth nothing".
    return float(getattr(sector, field, 0.0) or 0.0) or None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))
