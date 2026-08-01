"""Configuration loading and validation.

All domain knowledge lives in YAML under ``config/`` so the trader can tune the
system without touching Python. This module turns it into typed accessors and
fails loudly on structural mistakes (but never on a missing optional field).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("HAREL_CONFIG_DIR", REPO_ROOT / "config"))
DATA_DIR = Path(os.environ.get("HAREL_DATA_DIR", REPO_ROOT / "data"))


@dataclass(slots=True)
class TickerConfig:
    ticker: str
    name: str
    sector: str
    enabled: bool = True
    unresolved: bool = False
    aliases: list[str] = field(default_factory=list)
    cik: str | None = None
    tase_id: str | None = None
    exchange: str | None = None
    float_class: str = "unknown"
    ir_feeds: list[str] = field(default_factory=list)
    peers: list[str] = field(default_factory=list)
    peer_names: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    products: dict[str, list[str]] = field(default_factory=dict)
    competitor_products: list[str] = field(default_factory=list)
    peer_events_that_matter: list[str] = field(default_factory=list)
    single_points_of_failure: list[str] = field(default_factory=list)
    geographies: list[str] = field(default_factory=list)
    commodity_watch: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)
    resolution_hint: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def cik10(self) -> str | None:
        """CIK zero-padded to 10 digits, the form data.sec.gov expects."""
        if not self.cik:
            return None
        return str(self.cik).strip().lstrip("CIK").zfill(10)

    @property
    def match_names(self) -> list[str]:
        """Every string that means "this company" in a headline."""
        out = [self.ticker, self.name, *self.aliases]
        return [s for s in dict.fromkeys(out) if s and len(s) >= 2]

    @property
    def product_terms(self) -> list[str]:
        terms: list[str] = []
        for key, values in self.products.items():
            terms.append(key.replace("_", " "))
            terms.extend(values)
        return [t for t in dict.fromkeys(terms) if t]


@dataclass(slots=True)
class SectorConfig:
    key: str
    label: str
    regulators: list[str] = field(default_factory=list)
    fr_agencies: list[str] = field(default_factory=list)
    fr_terms: list[str] = field(default_factory=list)
    read_across: float = 0.0
    peer_read_across: float = 0.0
    high_impact_events: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SourceConfig:
    key: str
    kind: str
    label: str
    trust: float = 0.7
    latency: str = "unknown"
    poll_sec: int = 900
    enabled: bool = True
    fragile: bool = False
    requires: str | None = None
    key_optional: bool = False
    base_url: str = ""
    feeds: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        if not self.requires:
            return None
        return os.environ.get(self.requires) or None

    @property
    def available(self) -> bool:
        """A source needing an absent key is skipped, not fatal. Sources marked
        ``key_optional`` still run - the key only upgrades them (e.g. MAYA falls
        back from the official API to the public endpoints)."""
        if not self.enabled:
            return False
        if self.requires and not self.api_key and not self.key_optional:
            return False
        return True

    @property
    def degraded(self) -> bool:
        """Running, but without the credential that would make it authoritative."""
        return bool(self.requires and not self.api_key and self.key_optional)


@dataclass(slots=True)
class EventRule:
    key: str
    label: str
    base: float
    patterns: list[re.Pattern[str]]
    form_types: list[str] = field(default_factory=list)
    float_sensitive: bool = False


@dataclass(slots=True)
class NoisePattern:
    pattern: re.Pattern[str]
    cap: float


@dataclass(slots=True)
class ScoringConfig:
    tiers: dict[str, float]
    events: list[EventRule]
    noise_form_types: dict[str, float]
    noise_title_patterns: list[NoisePattern]
    # Articles written because the price already moved. Capped like noise, and
    # separately used by `whats_moving` to keep an effect from being presented
    # as a cause. One list, both uses.
    reactive_patterns: list[NoisePattern]
    noise_hard_cap_default: float
    require_corroboration_below_trust: float
    corroboration_cap: float
    relations: dict[str, float]
    float_sensitivity: dict[str, float]
    recency: dict[str, Any]
    price_confirmation: dict[str, Any]
    overrides: dict[str, Any]

    def tier_for(self, score: float) -> str:
        if score >= self.tiers.get("alert", 75):
            return "ALERT"
        if score >= self.tiers.get("high", 55):
            return "HIGH"
        if score >= self.tiers.get("normal", 35):
            return "NORMAL"
        return "NOISE"


@dataclass(slots=True)
class Config:
    universe: dict[str, TickerConfig]
    sectors: dict[str, SectorConfig]
    sources: dict[str, SourceConfig]
    scoring: ScoringConfig
    defaults: dict[str, Any]
    benchmarks: dict[str, Any] = field(default_factory=dict)

    def benchmark_for(self, sector_key: str) -> str | None:
        """Index proxy a name should be judged against.

        A move only means something relative to its group: +12% on a day the
        semis index rose 8% is a 4pp stock-specific move, not a 12% one.
        """
        by_sector = (self.benchmarks or {}).get("by_sector") or {}
        return by_sector.get(sector_key) or (self.benchmarks or {}).get("default")

    @property
    def benchmark_symbols(self) -> list[str]:
        out = []
        for t in self.active_tickers:
            tc = self.ticker(t)
            sym = self.benchmark_for(tc.sector) if tc else None
            if sym and sym not in out:
                out.append(sym)
        return out

    # -- convenience ------------------------------------------------------- #
    @property
    def active_tickers(self) -> list[str]:
        return [t for t, c in self.universe.items() if c.enabled and not c.unresolved]

    @property
    def unresolved_tickers(self) -> list[str]:
        return [t for t, c in self.universe.items() if c.unresolved]

    def ticker(self, symbol: str) -> TickerConfig | None:
        return self.universe.get(symbol.upper())

    def sector(self, key: str) -> SectorConfig:
        return self.sectors.get(key) or SectorConfig(key=key, label=key)

    def sources_for_kind(self, kind: str) -> list[SourceConfig]:
        return [s for s in self.sources.values() if s.kind == kind]

    def user_agent(self) -> str:
        template = self.defaults.get("user_agent", "HarelTerminal/1.0")
        contact = os.environ.get("SEC_CONTACT_EMAIL", "set-SEC_CONTACT_EMAIL@example.com")
        return template.replace("{SEC_CONTACT_EMAIL}", contact)

    def missing_keys(self) -> list[tuple[str, str]]:
        """Sources fully disabled because an API key is absent."""
        return [
            (s.key, s.requires)
            for s in self.sources.values()
            if s.enabled and s.requires and not s.api_key and not s.key_optional
        ]

    def degraded_sources(self) -> list[tuple[str, str]]:
        """Sources running on an unofficial fallback because a key is absent."""
        return [(s.key, s.requires) for s in self.sources.values() if s.degraded]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    compiled = []
    for pat in patterns or []:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE | re.UNICODE))
        except re.error as exc:  # a typo in YAML should name itself, not crash later
            raise ValueError(f"invalid regex in scoring.yaml: {pat!r} ({exc})") from exc
    return compiled


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(config_dir: Path | str | None = None) -> Config:
    cdir = Path(config_dir) if config_dir else CONFIG_DIR

    uni_raw = _load_yaml(cdir / "universe.yaml")
    sec_raw = _load_yaml(cdir / "sectors.yaml")
    src_raw = _load_yaml(cdir / "sources.yaml")
    sco_raw = _load_yaml(cdir / "scoring.yaml")

    universe: dict[str, TickerConfig] = {}
    for ticker, spec in (uni_raw.get("tickers") or {}).items():
        spec = spec or {}
        universe[ticker.upper()] = TickerConfig(
            ticker=ticker.upper(),
            name=spec.get("name", ticker),
            sector=spec.get("sector", "unknown"),
            enabled=bool(spec.get("enabled", True)),
            unresolved=bool(spec.get("unresolved", False)),
            aliases=list(spec.get("aliases") or []),
            cik=spec.get("cik"),
            tase_id=spec.get("tase_id"),
            exchange=spec.get("exchange"),
            float_class=spec.get("float_class", "unknown"),
            ir_feeds=list(spec.get("ir_feeds") or []),
            peers=list(spec.get("peers") or []),
            peer_names=list(spec.get("peer_names") or []),
            themes=list(spec.get("themes") or []),
            products=dict(spec.get("products") or {}),
            competitor_products=list(spec.get("competitor_products") or []),
            peer_events_that_matter=list(spec.get("peer_events_that_matter") or []),
            single_points_of_failure=list(spec.get("single_points_of_failure") or []),
            geographies=list(spec.get("geographies") or []),
            commodity_watch=list(spec.get("commodity_watch") or []),
            risk_flags=list(spec.get("risk_flags") or []),
            catalysts=list(spec.get("catalysts") or []),
            resolution_hint=spec.get("resolution_hint", ""),
            raw=spec,
        )

    sectors: dict[str, SectorConfig] = {}
    for key, spec in (sec_raw.get("sectors") or {}).items():
        spec = spec or {}
        sectors[key] = SectorConfig(
            key=key,
            label=spec.get("label", key),
            regulators=list(spec.get("regulators") or []),
            fr_agencies=list(spec.get("fr_agencies") or []),
            fr_terms=list(spec.get("fr_terms") or []),
            read_across=float(spec.get("read_across", 0.0)),
            peer_read_across=float(spec.get("peer_read_across", 0.0)),
            high_impact_events=list(spec.get("high_impact_events") or []),
        )

    sources: dict[str, SourceConfig] = {}
    for key, spec in (src_raw.get("sources") or {}).items():
        spec = spec or {}
        sources[key] = SourceConfig(
            key=key,
            kind=spec.get("kind", "rss"),
            label=spec.get("label", key),
            trust=float(spec.get("trust", 0.7)),
            latency=spec.get("latency", "unknown"),
            poll_sec=int(spec.get("poll_sec", 900)),
            enabled=bool(spec.get("enabled", True)),
            fragile=bool(spec.get("fragile", False)),
            requires=spec.get("requires"),
            key_optional=bool(spec.get("key_optional", False)),
            base_url=spec.get("base_url", ""),
            feeds=list(spec.get("feeds") or []),
            raw=spec,
        )

    events = []
    for key, spec in (sco_raw.get("events") or {}).items():
        spec = spec or {}
        events.append(
            EventRule(
                key=key,
                label=spec.get("label", key),
                base=float(spec.get("base", 0)),
                patterns=_compile(spec.get("patterns") or []),
                form_types=[f.upper() for f in (spec.get("form_types") or [])],
                float_sensitive=bool(spec.get("float_sensitive", False)),
            )
        )
    events.sort(key=lambda e: e.base, reverse=True)

    noise = sco_raw.get("noise") or {}
    hard_cap_default = float(noise.get("hard_cap_default", 12))

    def _noise_patterns(key: str) -> list[NoisePattern]:
        # `hard_cap_default` is what a noise pattern gets when it does not name
        # its own cap. It was declared in scoring.yaml and then duplicated here
        # as a literal 12, so editing the config moved nothing.
        return [
            NoisePattern(pattern=re.compile(np["pattern"], re.IGNORECASE | re.UNICODE),
                         cap=float(np.get("cap", hard_cap_default)))
            for np in (noise.get(key) or [])
        ]

    noise_titles = _noise_patterns("title_patterns")
    reactive = _noise_patterns("reactive_patterns")

    scoring = ScoringConfig(
        tiers={k: float(v) for k, v in (sco_raw.get("tiers") or {}).items()},
        events=events,
        noise_form_types={k.upper(): float(v) for k, v in (noise.get("form_types") or {}).items()},
        noise_title_patterns=noise_titles,
        reactive_patterns=reactive,
        noise_hard_cap_default=hard_cap_default,
        require_corroboration_below_trust=float(
            noise.get("require_corroboration_below_trust", 0.7)
        ),
        corroboration_cap=float(noise.get("corroboration_cap", 60)),
        relations={k: float(v) for k, v in (sco_raw.get("relations") or {}).items()},
        float_sensitivity={
            k: float(v) for k, v in (sco_raw.get("float_sensitivity") or {}).items()
        },
        recency=dict(sco_raw.get("recency") or {}),
        price_confirmation=dict(sco_raw.get("price_confirmation") or {}),
        # Keyed by ticker, and every ticker in this system is uppercase. A
        # lowercase YAML key half-applied its own block: `_compile_overrides`
        # normalised the key it built its keyword patterns under, the relation
        # lookup in `_score_link` did not, so `cgen:` gave CGEN its +20 keyword
        # boost while silently dropping the 0.95 PRODUCT_RIVAL it sits with.
        # Normalise once, here, so there is one spelling downstream.
        overrides={str(k).upper(): v for k, v in (sco_raw.get("overrides") or {}).items()},
    )

    return Config(
        universe=universe,
        sectors=sectors,
        sources=sources,
        scoring=scoring,
        defaults=dict(src_raw.get("defaults") or {}),
        benchmarks=dict(sec_raw.get("benchmarks") or {}),
    )


@lru_cache(maxsize=4)
def get_config(config_dir: str | None = None) -> Config:
    return load_config(config_dir)
