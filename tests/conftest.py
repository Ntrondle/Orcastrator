"""Fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer

import custom_components.orcastrator as init_mod
from custom_components.orcastrator import http as http_mod

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def isolated_spool_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give every test its own spool directory so tests cannot collide."""
    spool = tmp_path / "spool"
    monkeypatch.setattr(http_mod, "SPOOL_DIR", spool)
    monkeypatch.setattr(init_mod, "SPOOL_DIR", spool)
    return spool


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
