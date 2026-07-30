"""Collector base class and registry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..config import Config, SourceConfig
from ..db import Database
from ..http import HttpClient
from ..models import RawItem

log = logging.getLogger("harel.collect")

_REGISTRY: dict[str, type["Collector"]] = {}


def register(kind: str) -> Callable[[type["Collector"]], type["Collector"]]:
    def deco(cls: type["Collector"]) -> type["Collector"]:
        _REGISTRY[kind] = cls
        cls.kind = kind
        return cls

    return deco


@dataclass(slots=True)
class CollectorContext:
    config: Config
    client: HttpClient
    db: Database
    lookback_hours: float = 72.0
    dry_run: bool = False

    @property
    def since(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)


class Collector(ABC):
    """One external system. Must never raise for a single bad record."""

    kind: str = "unknown"

    def __init__(self, source: SourceConfig, ctx: CollectorContext) -> None:
        self.source = source
        self.ctx = ctx
        self.cfg = ctx.config
        self.client = ctx.client
        self.db = ctx.db
        self.warnings: list[str] = []

    @abstractmethod
    def collect(self) -> Iterator[RawItem]:
        """Yield RawItems. Implementations should be generous with try/except:
        one malformed record must not lose the rest of the batch."""

    # -- helpers shared by subclasses -------------------------------------- #
    def state(self) -> dict[str, Any]:
        return self.db.get_source_state(self.source.key)

    def save_state(self, **fields: Any) -> None:
        self.db.set_source_state(self.source.key, **fields)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        log.warning("[%s] %s", self.source.key, message)

    def make_item(self, **kwargs: Any) -> RawItem:
        kwargs.setdefault("source", self.source.key)
        kwargs.setdefault("source_kind", self.kind)
        return RawItem(**kwargs)

    @property
    def active_tickers(self) -> list[str]:
        return self.cfg.active_tickers


def build_collectors(ctx: CollectorContext, only: list[str] | None = None) -> list[Collector]:
    """Instantiate one collector per available source in config/sources.yaml."""
    out: list[Collector] = []
    for key, source in ctx.config.sources.items():
        if only and key not in only:
            continue
        if not source.available:
            if source.enabled and source.requires:
                log.info("skipping %s - %s not set", key, source.requires)
            continue
        cls = _REGISTRY.get(source.kind)
        if cls is None:
            log.debug("no collector registered for kind=%s (source=%s)", source.kind, key)
            continue
        out.append(cls(source, ctx))
    return out


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)
