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
    "maya.tase.co.il": 2.0,
    "mayafiles.tase.co.il": 2.0,
    "datawise.tase.co.il": 2.0,
    "news.google.com": 2.0,
    "stooq.com": 2.0,
    "query1.finance.yahoo.com": 2.0,
    # Measured, not guessed: six StockTitan requests in a row returned 200,200,
    # 200,200,200,200 then 429 on the seventh. One request every two seconds
    # walks all 22 names in under a minute and has not tripped it.
    "www.stocktitan.net": 0.5,
    "stockanalysis.com": 1.0,
    # fda.gov redirects a too-quick second request to an abuse-detection page
    # that answers 404 - which read as "the feed moved" and left the pharma
    # sleeve with no regulator channel for 5055 consecutive passes.
    "www.fda.gov": 0.4,
    "www.globenewswire.com": 1.0,
    # Incapsula. Polling the halt feed hard turns it into a 200 that carries
    # a JavaScript challenge instead of the feed - which reads as "nothing
    # halted" unless the body is checked. See collect/halts.py::_is_bot_wall.
    "www.nasdaqtrader.com": 0.2,
    "feeds.finance.yahoo.com": 2.0,
}
DEFAULT_RATE = 2.0

# Hosts that serve a self-identifying crawler and turn away one wearing a
# browser string. sec.gov *requires* a contact address; fda.gov silently
# redirects Chrome to an abuse-detection page. Both are public data published
# for programmatic use, and both prefer to be told who is asking.
HONEST_UA_HOSTS = ("sec.gov", "fda.gov")

# Used for every host not in HONEST_UA_HOSTS - see HttpClient._ua_for.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


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
        self.user_agent = user_agent
        # One client is shared by every collector, and collectors fetch from
        # worker threads. requests.Session is not thread-safe where it matters
        # here: the cookie jar and redirect handling mutate shared state, and
        # Google News and MAYA both set cookies. So the Session is per-thread,
        # built lazily and carrying the same default headers - a single-threaded
        # caller still sees exactly one session for the life of the client.
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                }
            )
            self._local.session = session
        return session

    def _ua_for(self, host: str) -> str:
        """The SEC *mandates* a contact-bearing User-Agent and will ban clients
        without one, so sec.gov always gets ours.

        The IR platforms are the opposite problem: several of them (Q4 Inc hosts
        such as ir.liveperson.com and investors.paloaltonetworks.com) do not
        reject an unfamiliar agent, they simply never answer - each one burned
        ~90s per pass in read timeouts and three retries while returning 200 in
        1.5s to an ordinary browser string. These are public press-release feeds
        published for syndication; the request is the same, only the header
        differs.

        fda.gov turned out to be a third case, and an expensive one. It is not
        rate that trips it - it is the browser string itself. Measured on one
        host, seconds apart, on the same URL:

            research UA -> HTTP 200, 15940 bytes of valid RSS
            Chrome UA   -> HTTP 302 -> /apology_objects/abuse-detection-apology
                           .html -> HTTP 404

        which is the "HTTP 404 - feed may have moved" this system had been
        reporting. In production that had happened 5055 consecutive times
        without a single success since deployment, taking the FDA press and
        MedWatch channel - recalls, safety communications, approvals - off a
        six-name pharma and device sleeve entirely, while the source row read
        "ran OK, 0 items". A crawler that says who it is gets served; one
        pretending to be Chrome gets shown the door.
        """
        if any(host == h or host.endswith("." + h) for h in HONEST_UA_HOSTS):
            return self.user_agent
        return BROWSER_UA

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
        req_headers.setdefault("User-Agent", self._ua_for(host))
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

    def post(
        self,
        url: str,
        *,
        json: Any,
        headers: dict[str, str] | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> Response:
        """Same politeness as `get`, for the one source that refuses a GET.

        USASpending's award search answers **405 to GET** - the filter set is a
        nested document that will not fit in a query string - so a POST is the
        only way to reach it. Everything that makes `get` safe applies equally
        and is repeated here rather than shared, because the two differ in one
        respect worth keeping visible: there is no ETag or If-Modified-Since. A
        POST is not conditionally cacheable, so this method has no 304 branch
        and callers must not expect `not_modified`.
        """
        host = urlsplit(url).netloc
        req_headers = dict(headers or {})
        req_headers.setdefault("User-Agent", self._ua_for(host))
        req_headers.setdefault("Content-Type", "application/json")

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            _limiter.wait(host)
            try:
                resp = self.session.post(
                    url, json=json, headers=req_headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                self._backoff(attempt, f"{type(exc).__name__}: {exc}", url)
                continue

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
