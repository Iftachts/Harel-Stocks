from __future__ import annotations

import json
from pathlib import Path

import pytest

from harel.config import load_config
from harel.db import Database
from harel.http import Response

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def config():
    return load_config(REPO_ROOT / "config")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


UNRESOLVED_BLOCK = """
  ZZTEST:
    name: "UNRESOLVED TICKER"
    enabled: false
    unresolved: true
    resolution_hint: "Fixture-only entry used to test the unresolved-ticker path."
    aliases: []
    cik: null
    tase_id: null
    exchange: null
    sector: unknown
    float_class: unknown
    peers: []
    themes: []
"""


@pytest.fixture
def config_with_unresolved(tmp_path):
    """The real universe has no unresolved tickers, which is the desired state.

    The machinery that handles one still has to work - a symbol can be mistyped
    or delisted at any time - so these tests run against a copy of the real
    config with one synthetic unresolved entry appended.
    """
    import shutil

    cdir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", cdir)
    universe = cdir / "universe.yaml"
    universe.write_text(
        universe.read_text(encoding="utf-8") + UNRESOLVED_BLOCK, encoding="utf-8"
    )
    return load_config(cdir)


class FakeHttpClient:
    """Serves recorded fixtures by URL substring. Anything unmapped 404s, which
    is what a collector must survive."""

    def __init__(self, routes: dict[str, str | bytes | dict]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, *, headers=None, params=None, etag=None,
            last_modified=None, allow_status=()):
        full = url
        if params:
            if isinstance(params, dict):
                pairs = list(params.items())
            else:
                pairs = list(params)
            full += "?" + "&".join(f"{k}={v}" for k, v in pairs)
        self.calls.append(full)

        for needle, payload in self.routes.items():
            if needle in full:
                if isinstance(payload, (dict, list)):
                    text = json.dumps(payload)
                    body = text.encode()
                elif isinstance(payload, bytes):
                    body, text = payload, payload.decode("utf-8", "replace")
                else:
                    text, body = payload, payload.encode()
                return Response(200, text, body, {}, url)

        if 404 in allow_status:
            return Response(404, "", b"", {}, url)
        from harel.http import HttpError

        raise HttpError(404, url, "no fixture")


@pytest.fixture
def fake_client():
    return FakeHttpClient


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))
