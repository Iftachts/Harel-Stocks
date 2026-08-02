from __future__ import annotations

import json
import json as _json
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
        self.headers_seen: list[dict[str, str]] = []
        # POST bodies, in order. A POST puts the whole query in the body, so
        # without recording it a test cannot tell which recipient was asked
        # about - or notice someone "tidying" date_type out of the filters.
        self.posts: list[tuple[str, dict]] = []

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
        self.headers_seen.append(dict(headers or {}))

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

    def post(self, url, *, json=None, headers=None, allow_status=()):
        """Routes on the URL *and* the request body.

        Every USASpending query goes to one URL and differs only in the body, so
        a URL-only route could not serve one fixture per recipient. A needle is
        matched against both, which lets a test key on "TAT TECHNOLOGIES".
        """
        body = json if isinstance(json, dict) else {}
        self.posts.append((url, body))
        self.calls.append(url)
        self.headers_seen.append(dict(headers or {}))
        haystack = url + " " + _json.dumps(body, sort_keys=True)

        for needle, payload in self.routes.items():
            if needle in haystack:
                if callable(payload):
                    payload = payload(body)
                if isinstance(payload, (dict, list)):
                    text = _json.dumps(payload)
                    return Response(200, text, text.encode(), {}, url)
                text = payload if isinstance(payload, str) else payload.decode()
                return Response(200, text, text.encode(), {}, url)

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
