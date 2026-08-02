"""Collectors. One module per external system; all emit :class:`RawItem`."""

from __future__ import annotations

from .base import Collector, CollectorContext, build_collectors, register

# Importing the modules is what populates the registry.
from . import (  # noqa: F401  (side-effect imports)
    clinicaltrials,
    edgar,
    fda,
    federal_register,
    ir_pages,
    maya,
    prices,
    rss,
)

__all__ = ["Collector", "CollectorContext", "build_collectors", "register"]
