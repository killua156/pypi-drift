"""Talk to PyPI: one read-only GET per distinct package, concurrently, with a retry.

HTTP lives here and nowhere else, so the verdict logic stays pure. Nothing in
this module raises for a failed lookup -- a failure is returned as a
:class:`FetchOutcome` carrying its reason, because one unreachable package must
never abort the run.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import httpx

from . import __version__

#: The only endpoint this tool ever touches.
URL_TEMPLATE = "https://pypi.org/pypi/{name}/json"

DEFAULT_TIMEOUT = 10.0
DEFAULT_WORKERS = 8
#: One retry means two attempts in total.
DEFAULT_ATTEMPTS = 2

#: Statuses worth a second attempt; 404 is definitive and 4xx is not our fault.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Wait this long before a retry, so a rate-limiting host is not hit twice at once.
RETRY_BACKOFF = 0.5
#: Never honor a Retry-After longer than this; a hung host must not stall the run.
MAX_RETRY_DELAY = 5.0

_NORMALIZE_RE = re.compile(r"[-_.]+")
#: PEP 508 project-name grammar. Anything else is not a package we can ask about.
_VALID_NAME_RE = re.compile(r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", re.IGNORECASE)

USER_AGENT = "pypi-drift/{}".format(__version__)


@dataclass(frozen=True)
class FetchOutcome:
    """Either the latest version string, or the reason it could not be read."""

    version: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.version is not None


def normalize_name(name: str) -> str:
    """PEP 503 normalization -- what gets sent to PyPI, never what gets printed."""
    return _NORMALIZE_RE.sub("-", name.strip()).lower()


def is_valid_name(name: str) -> bool:
    """True when ``name`` matches the PEP 508 project-name grammar.

    Guards the URL: without this, ``q?a=1`` would become a query string and
    report a different package's version, and ``a/b`` an extra path segment.
    """
    return bool(_VALID_NAME_RE.match(name))


def build_client(timeout: float = DEFAULT_TIMEOUT, workers: int = DEFAULT_WORKERS) -> httpx.Client:
    """A client sized for the worker pool. Tests replace this to avoid the network."""
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        limits=httpx.Limits(max_connections=max(workers, 1), max_keepalive_connections=max(workers, 1)),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def fetch_latest(
    client: httpx.Client,
    name: str,
    timeout: Optional[float] = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> FetchOutcome:
    """Read ``info.version`` for one already-normalized package name.

    ``info.version`` is PyPI's own idea of the latest release, which excludes
    pre-releases unless a project has nothing else. Never raises.
    """
    if not is_valid_name(name):
        return FetchOutcome(error="invalid package name")

    url = URL_TEMPLATE.format(name=urllib.parse.quote(name, safe=""))
    request_kwargs = {} if timeout is None else {"timeout": timeout}
    total = max(attempts, 1)
    reason = "unknown error"

    for attempt in range(total):
        response = None
        try:
            response = client.get(url, **request_kwargs)
        except httpx.TimeoutException:
            reason = "request timed out"
        except httpx.HTTPError as exc:
            reason = "request failed: {}".format(exc.__class__.__name__)
        else:
            if response.status_code == 404:
                return FetchOutcome(error="not found on PyPI")
            if response.status_code in RETRYABLE_STATUS:
                reason = "PyPI returned HTTP {}".format(response.status_code)
            elif response.status_code != 200:
                return FetchOutcome(error="PyPI returned HTTP {}".format(response.status_code))
            else:
                return _read_version(response)
        if attempt + 1 < total:
            time.sleep(retry_delay(response))
    return FetchOutcome(error=reason)


def retry_delay(response: Optional[httpx.Response]) -> float:
    """How long to wait before retrying, honoring ``Retry-After`` on a 429."""
    if response is not None and response.status_code == 429:
        header = response.headers.get("Retry-After")
        if header:
            try:
                seconds = float(header.strip())
            except ValueError:  # the HTTP-date form; not worth parsing
                return RETRY_BACKOFF
            return max(0.0, min(seconds, MAX_RETRY_DELAY))
    return RETRY_BACKOFF


def _read_version(response: httpx.Response) -> FetchOutcome:
    try:
        version = response.json()["info"]["version"]
    except (ValueError, KeyError, TypeError):
        return FetchOutcome(error="unexpected response from PyPI")
    if not isinstance(version, str) or not version.strip():
        return FetchOutcome(error="unexpected response from PyPI")
    return FetchOutcome(version=version.strip())


def fetch_all(
    names: Iterable[str],
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    client: Optional[httpx.Client] = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> Dict[str, FetchOutcome]:
    """Look up every distinct package concurrently, capped at ``workers`` in flight.

    Returns a mapping keyed by PEP 503 normalized name. Duplicate spellings of
    the same package share a single request.
    """
    unique: List[str] = []
    seen = set()
    for raw in names:
        key = normalize_name(raw)
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    if not unique:
        return {}

    owns_client = client is None
    if client is None:
        client = build_client(timeout, workers)

    def lookup(name: str) -> FetchOutcome:
        try:
            return fetch_latest(client, name, timeout=timeout, attempts=attempts)
        except Exception as exc:  # a worker must never take the run down with it
            return FetchOutcome(error="lookup failed: {}".format(exc.__class__.__name__))

    try:
        pool_size = max(1, min(int(workers), len(unique)))
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            outcomes = list(pool.map(lookup, unique))
    finally:
        if owns_client:
            client.close()

    return dict(zip(unique, outcomes))
