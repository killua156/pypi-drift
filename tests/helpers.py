"""Stub builders shared by the test modules.

These live here rather than in ``conftest.py`` because a test module has to
*import* them, and importing conftest only works under pytest's default
``prepend`` import mode. ``pythonpath = ["tests"]`` in pyproject.toml puts this
module on the path for every import mode.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

import httpx


def json_response(version: str) -> httpx.Response:
    """A minimal PyPI project response carrying ``info.version``."""
    return httpx.Response(200, json={"info": {"name": "stub", "version": version}})


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """An httpx client whose every request is served by ``handler``."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def version_map_handler(versions: Dict[str, Optional[str]]):
    """Serve a normalized-name -> version map; ``None`` (or a miss) yields a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.strip("/").split("/")[1]
        version = versions.get(name)
        if version is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return json_response(version)

    return handler
