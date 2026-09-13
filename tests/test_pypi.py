"""The HTTP layer: normalization, 404s, retries, timeouts, bounded concurrency."""

import threading

import httpx
import pytest

from helpers import json_response, make_client
from pypi_drift import pypi


@pytest.fixture
def no_backoff(monkeypatch):
    """Drop the inter-attempt pause, so retry tests stay fast."""
    monkeypatch.setattr(pypi, "RETRY_BACKOFF", 0.0)


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("requests", "requests"),
        ("Flask-SQLAlchemy", "flask-sqlalchemy"),
        ("Flask_SQLAlchemy", "flask-sqlalchemy"),
        ("zope.interface", "zope-interface"),
        ("a__b--c..d", "a-b-c-d"),
        ("  Requests  ", "requests"),
    ],
)
def test_normalize_name_follows_pep503(raw, normalized):
    assert pypi.normalize_name(raw) == normalized


def test_request_uses_the_normalized_name():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return json_response("2.0.0")

    with make_client(handler) as client:
        assert pypi.fetch_latest(client, pypi.normalize_name("Zope.Interface")).version == "2.0.0"
    assert seen == ["https://pypi.org/pypi/zope-interface/json"]


def test_fetch_latest_reads_info_version():
    with make_client(lambda request: json_response("3.0.1")) as client:
        outcome = pypi.fetch_latest(client, "requests")
    assert outcome.ok and outcome.version == "3.0.1"


def test_unknown_package_is_reported_not_raised():
    with make_client(lambda request: httpx.Response(404)) as client:
        outcome = pypi.fetch_latest(client, "ghost")
    assert not outcome.ok
    assert outcome.error == "not found on PyPI"


def test_a_404_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    with make_client(handler) as client:
        pypi.fetch_latest(client, "ghost")
    assert len(calls) == 1


def test_timeout_is_retried_once_then_reported(no_backoff):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("too slow", request=request)

    with make_client(handler) as client:
        outcome = pypi.fetch_latest(client, "slowpoke")
    assert len(calls) == pypi.DEFAULT_ATTEMPTS == 2
    assert outcome.error == "request timed out"


def test_a_transient_failure_recovers_on_the_retry(no_backoff):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return json_response("4.2.0")

    with make_client(handler) as client:
        outcome = pypi.fetch_latest(client, "flaky")
    assert len(calls) == 2
    assert outcome.version == "4.2.0"


def test_connection_failure_reports_its_reason():
    def handler(request):
        raise httpx.ConnectError("unreachable", request=request)

    with make_client(handler) as client:
        outcome = pypi.fetch_latest(client, "nowhere")
    assert "ConnectError" in outcome.error


def test_server_error_is_retried_and_then_reported(no_backoff):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    with make_client(handler) as client:
        outcome = pypi.fetch_latest(client, "wobbly")
    assert len(calls) == 2
    assert outcome.error == "PyPI returned HTTP 503"


def test_unexpected_status_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403)

    with make_client(handler) as client:
        outcome = pypi.fetch_latest(client, "forbidden")
    assert len(calls) == 1
    assert outcome.error == "PyPI returned HTTP 403"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"info": {}}),
        httpx.Response(200, json={"nope": 1}),
        httpx.Response(200, json={"info": {"version": ""}}),
        httpx.Response(200, json={"info": {"version": None}}),
    ],
)
def test_malformed_payloads_are_reported_not_raised(response):
    with make_client(lambda request: response) as client:
        outcome = pypi.fetch_latest(client, "weird")
    assert outcome.error == "unexpected response from PyPI"


def test_fetch_all_keys_results_by_normalized_name():
    with make_client(lambda request: json_response("1.0.0")) as client:
        outcomes = pypi.fetch_all(["Flask_SQLAlchemy", "requests"], client=client, workers=2)
    assert set(outcomes) == {"flask-sqlalchemy", "requests"}


def test_fetch_all_issues_one_request_per_distinct_package():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return json_response("1.0.0")

    with make_client(handler) as client:
        outcomes = pypi.fetch_all(
            ["requests", "Requests", "  requests  ", "Flask_SQLAlchemy", "flask.sqlalchemy"],
            client=client,
            workers=4,
        )
    assert len(calls) == 2
    assert set(outcomes) == {"requests", "flask-sqlalchemy"}


def test_fetch_all_of_nothing_makes_no_requests():
    def handler(request):  # pragma: no cover - must never run
        raise AssertionError("no request should be issued")

    with make_client(handler) as client:
        assert pypi.fetch_all([], client=client) == {}


def test_one_failure_does_not_stop_the_others():
    def handler(request):
        if "ghost" in str(request.url):
            return httpx.Response(404)
        return json_response("2.0.0")

    with make_client(handler) as client:
        outcomes = pypi.fetch_all(["requests", "ghost", "flask"], client=client, workers=3)
    assert outcomes["requests"].version == "2.0.0"
    assert outcomes["flask"].version == "2.0.0"
    assert outcomes["ghost"].error == "not found on PyPI"


def test_requests_run_concurrently_under_the_worker_cap():
    """Exactly `workers` requests must be in flight at once.

    The barrier is the assertion: it only releases once `workers` handlers have
    arrived together, so this deadlocks (rather than passing by luck) if the
    pool ever serialises. Nothing here depends on wall-clock timing.
    """
    workers = 10
    packages = ["pkg-{}".format(index) for index in range(50)]
    barrier = threading.Barrier(workers, timeout=30)
    lock = threading.Lock()
    state = {"live": 0, "peak": 0}

    def handler(request):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        barrier.wait()  # only passes if `workers` requests are concurrent
        with lock:
            state["live"] -= 1
        return json_response("1.0.0")

    with make_client(handler) as client:
        outcomes = pypi.fetch_all(packages, client=client, workers=workers)

    assert len(outcomes) == 50
    assert all(outcome.version == "1.0.0" for outcome in outcomes.values())
    assert state["peak"] == workers, "peak in-flight was {}".format(state["peak"])


def test_the_worker_cap_is_an_upper_bound_not_just_a_hint():
    """With the cap at 2, a third concurrent request must never appear."""
    barrier = threading.Barrier(2, timeout=30)
    lock = threading.Lock()
    state = {"live": 0, "peak": 0}

    def handler(request):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        barrier.wait()
        with lock:
            state["live"] -= 1
        return json_response("1.0.0")

    with make_client(handler) as client:
        pypi.fetch_all(["pkg-{}".format(i) for i in range(8)], client=client, workers=2)

    assert state["peak"] == 2


def test_worker_pool_is_never_larger_than_the_work():
    sizes = []
    real_pool = pypi.ThreadPoolExecutor

    class RecordingPool(real_pool):
        def __init__(self, max_workers=None, **kwargs):
            sizes.append(max_workers)
            super().__init__(max_workers=max_workers, **kwargs)

    pypi.ThreadPoolExecutor = RecordingPool
    try:
        with make_client(lambda request: json_response("1.0.0")) as client:
            pypi.fetch_all(["only-one"], client=client, workers=32)
    finally:
        pypi.ThreadPoolExecutor = real_pool
    assert sizes == [1]


def test_build_client_is_configured_with_the_timeout_and_pool_size():
    client = pypi.build_client(timeout=3.5, workers=4)
    try:
        assert client.timeout.connect == 3.5
        assert client.headers["user-agent"] == "pypi-drift/{}".format(pypi.__version__)
        limits = client._transport._pool._max_connections
        assert limits == 4, "connection pool must not be narrower than the workers"
    finally:
        client.close()


def test_user_agent_advertises_no_url_that_does_not_exist():
    assert "://" not in pypi.USER_AGENT


@pytest.mark.parametrize(
    "name",
    [
        "q?a=1",      # would become a query string and report another package
        "a/b",        # would add a path segment
        "spa ced",
        "../etc",
        "pkg\x00nul",
        "-leading-dash",
    ],
)
def test_names_outside_the_pep508_grammar_are_refused_without_a_request(name):
    def handler(request):  # pragma: no cover - must never run
        raise AssertionError("no request should be issued for {!r}".format(name))

    with make_client(handler) as client:
        outcome = pypi.fetch_latest(client, name)
    assert outcome.error == "invalid package name"


def test_a_refused_name_does_not_stop_the_other_packages():
    def handler(request):
        return json_response("2.0.0")

    with make_client(handler) as client:
        outcomes = pypi.fetch_all(["requests", "q?a=1", "flask"], client=client, workers=3)
    assert outcomes["requests"].version == "2.0.0"
    assert outcomes["flask"].version == "2.0.0"
    assert outcomes["q?a=1"].error == "invalid package name"


def test_the_name_is_percent_escaped_into_the_url():
    """Even a grammar-valid name is escaped, so the path can never be widened."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return json_response("1.0.0")

    with make_client(handler) as client:
        pypi.fetch_latest(client, "zope-interface")
    assert seen == ["https://pypi.org/pypi/zope-interface/json"]


def test_a_worker_exception_does_not_take_the_run_down():
    """A non-httpx error (httpx.InvalidURL, say) must not kill the other lookups."""
    original = pypi.fetch_latest

    def exploding(client, name, **kwargs):
        if name == "detonate":
            raise httpx.InvalidURL("no host")
        return original(client, name, **kwargs)

    pypi.fetch_latest = exploding
    try:
        with make_client(lambda request: json_response("2.0.0")) as client:
            outcomes = pypi.fetch_all(
                ["requests", "detonate", "flask"], client=client, workers=3
            )
    finally:
        pypi.fetch_latest = original

    assert outcomes["requests"].version == "2.0.0"
    assert outcomes["flask"].version == "2.0.0"
    assert "lookup failed" in outcomes["detonate"].error


def test_retry_after_on_a_429_is_honored_and_capped():
    def response(status, **headers):
        return httpx.Response(status, headers=headers)

    assert pypi.retry_delay(response(429, **{"Retry-After": "2"})) == 2.0
    assert pypi.retry_delay(response(429, **{"Retry-After": "9999"})) == pypi.MAX_RETRY_DELAY
    # The HTTP-date form is not worth parsing; fall back rather than guess.
    assert pypi.retry_delay(response(429, **{"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) == (
        pypi.RETRY_BACKOFF
    )
    assert pypi.retry_delay(response(429)) == pypi.RETRY_BACKOFF
    assert pypi.retry_delay(response(503)) == pypi.RETRY_BACKOFF
    assert pypi.retry_delay(None) == pypi.RETRY_BACKOFF


def test_a_rate_limited_host_is_not_hit_twice_without_a_pause(monkeypatch):
    slept = []
    monkeypatch.setattr(pypi.time, "sleep", slept.append)

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "1"})

    with make_client(handler) as client:
        outcome = pypi.fetch_latest(client, "hammered")

    assert outcome.error == "PyPI returned HTTP 429"
    assert slept == [1.0], "the retry must pause, honoring Retry-After"


def test_no_pause_is_taken_after_the_final_attempt(monkeypatch):
    slept = []
    monkeypatch.setattr(pypi.time, "sleep", slept.append)

    with make_client(lambda request: httpx.Response(503)) as client:
        pypi.fetch_latest(client, "wobbly")

    assert len(slept) == pypi.DEFAULT_ATTEMPTS - 1 == 1
