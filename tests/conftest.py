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
