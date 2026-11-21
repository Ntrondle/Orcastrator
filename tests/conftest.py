"""Fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from aiohttp.test_utils import TestServer

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
async def serve(socket_enabled: None) -> AsyncGenerator[Callable[[Any], Any]]:
    """Start aiohttp apps on localhost; returns their base URL."""
    servers: list[TestServer] = []

    async def _serve(app: Any) -> str:
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        servers.append(server)
        return str(server.make_url("")).rstrip("/")

    yield _serve
    for server in servers:
        await server.close()
