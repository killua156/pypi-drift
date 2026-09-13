"""Fixtures shared by the test modules. Stub builders live in ``helpers.py``."""

from __future__ import annotations

from typing import Dict, Optional

import httpx
import pytest

from helpers import make_client, version_map_handler


@pytest.fixture
def stub_pypi(monkeypatch):
    """Point the package's client factory at a stubbed transport.

    Usage: ``stub_pypi({"requests": "3.0.1", "ghost": None})`` -- keys are PEP 503
    normalized names, ``None`` means the package 404s.
    """

    def install(versions: Dict[str, Optional[str]]):
        handler = version_map_handler(versions)
        monkeypatch.setattr(
            "pypi_drift.pypi.build_client",
            lambda *args, **kwargs: make_client(handler),
        )
        return handler

    return install


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make httpx's real transports fail, for every test in the suite.

    This covers the only way this package reaches the network -- it is not a
    general sandbox, and raw sockets or urllib would still get out.
    """

    def explode(*args, **kwargs):
        raise AssertionError("the test suite must not touch the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", explode)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", explode)


@pytest.fixture
def write_csv(tmp_path):
    def write(text: str, name: str = "pins.csv"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    return write
