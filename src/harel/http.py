"""A polite, rate-limited HTTP client.

Several of our sources (SEC above all) will ban a client that misbehaves, so
politeness is a correctness requirement here, not a nicety:

* a descriptive User-Agent with a contact address (SEC mandates this),
* a per-host token bucket (SEC allows 10 req/s; we default to 5),
* exponential backoff with jitter on 429/5xx,
* conditional GET via ETag / Last-Modified so repeated polls are cheap.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger("harel.http")

# Conservative per-host rate limits (requests per second).
HOST_RATE_LIMITS = {
    "www.sec.gov": 5.0,
    "data.sec.gov": 5.0,
    "efts.sec.gov": 5.0,
    "api.fda.gov": 3.0,
    "clinicaltrials.gov": 3.0,
    "www.federalregister.gov": 5.0,
    "mayaapi.tase.co.il": 2.0,
    "news.google.com": 2.0,
    "stooq.com": 2.0,
    "query1.finance.yahoo.com": 2.0,
}
DEFAULT_RATE = 2.0


class _RateLimiter:
    """Simple per-host spacing. Threadsafe; good enough for a single-user box."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_ok: dict[str, float] = {}

    def wait(self, host: str) -> None:
        rate = HOST_RATE_LIMITS.get(host, DEFAULT_RATE)
        min_gap = 1.0 / rate
        with self._lock:
            now = time.monotonic()
            ready_at = self._next_ok.get(host, 0.0)
            sleep_for = max(0.0, ready_at - now)
            self._next_ok[host] = max(now, ready_at) + min_gap
        if sleep_for > 0:
            time.sleep(sleep_for)


_limiter = _RateLimiter()


@dataclass(slots=True)
class Response:
    status: int
    text: str
    content: bytes
    headers: dict[str, str]
    url: str
    not_modified: bool = False

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.text) if self.text else None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 or self.not_modified


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: int = 25,
        max_retries: int = 3,
        backoff_base: float = 2.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> Response:
        host = urlsplit(url).netloc
        req_headers = dict(headers or {})
        if etag:
            req_headers["If-None-Match"] = etag
        if last_modified:
            req_headers["If-Modified-Since"] = last_modified

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            _limiter.wait(host)
            try:
                resp = self.session.get(
                    url, headers=req_headers, params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                self._backoff(attempt, f"{type(exc).__name__}: {exc}", url)
                continue

            if resp.status_code == 304:
                return Response(304, "", b"", dict(resp.headers), resp.url, not_modified=True)

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                self._backoff(attempt, f"HTTP {resp.status_code}", url, retry_after)
                continue

            if resp.status_code >= 400 and resp.status_code not in allow_status:
                raise HttpError(resp.status_code, url, resp.text[:400])

            return Response(
                resp.status_code, resp.text, resp.content, dict(resp.headers), resp.url
            )

        raise last_exc or HttpError(0, url, "exhausted retries")

    def _backoff(self, attempt: int, why: str, url: str, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after is not None else self.backoff_base * (2**attempt)
        delay += random.uniform(0, 0.5)          # jitter: don't sync with other pollers
        log.warning("retry %s in %.1fs (%s) %s", attempt + 1, delay, why, url)
        time.sleep(min(delay, 60.0))


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body}")
        self.status = status
        self.url = url
        self.body = body


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
