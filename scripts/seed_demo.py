#!/usr/bin/env python3
"""Seed a demo database from the test fixtures - no network required.

    python scripts/seed_demo.py            # writes data/demo.db
    harel --db data/demo.db morning
    harel --db data/demo.db export demo.html

Use this to see the ranking, the relation labels and the terminal layout before
you configure any credentials. The content is a handful of realistic-but-fake
records; it is not market data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeHttpClient  # noqa: E402
from harel.config import load_config  # noqa: E402
from harel.db import Database  # noqa: E402
from harel.pipeline import Pipeline  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

ROUTES = {
    "submissions/CIK0000818686": json.loads((FIXTURES / "edgar_teva_submissions.json").read_text()),
    "efts.sec.gov": json.loads((FIXTURES / "edgar_fts.json").read_text()),
    "federalregister.gov/api/v1/documents.json": json.loads(
        (FIXTURES / "federal_register.json").read_text()
    ),
    "clinicaltrials.gov/api/v2/studies": json.loads(
        (FIXTURES / "clinicaltrials.json").read_text()
    ),
    "api.fda.gov/drug/enforcement.json": json.loads(
        (FIXTURES / "openfda_enforcement.json").read_text()
    ),
    "maya.tase.co.il/api/v1/reports": json.loads(
        (FIXTURES / "maya_v1_reports.json").read_text(encoding="utf-8")
    ),
    # Only TEVA gets a price series, so the demo does not imply that every
    # name gapped 10% on the same day.
    "s=teva.us": (FIXTURES / "stooq_teva.csv").read_text(),
    "ir.cgen.com": (FIXTURES / "ir_feed.xml").read_text(),
}

SOURCES = [
    "sec_edgar_submissions", "sec_edgar_full_text", "federal_register",
    "clinicaltrials", "fda_enforcement", "maya_tase", "prices_stooq",
    "company_ir_rss",
]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "demo.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    config = load_config(ROOT / "config")
    db = Database(out)
    report = Pipeline(
        config=config, db=db, lookback_hours=24 * 365, client=FakeHttpClient(ROUTES)
    ).run(only=SOURCES)

    print(f"seeded {out}")
    print(f"  collected {report.collected}, stored {report.stored}, "
          f"dropped {report.deduped}")
    for source, count in sorted(report.by_source.items(), key=lambda kv: -kv[1]):
        if count:
            print(f"  {source:<28} {count}")
    print(f"\nNext:\n  harel --db {out} morning\n  harel --db {out} export demo.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
