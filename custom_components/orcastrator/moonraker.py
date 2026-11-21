"""Minimal Moonraker client: readiness, firmware restart, upload."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiohttp

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=5)
_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=600)


class MoonrakerError(Exception):
    """Moonraker refused a request or could not be reached."""


class Moonraker:
    """Talks to one printer's Moonraker."""

    def __init__(
        self, session: aiohttp.ClientSession, url: str, api_key: str | None
    ) -> None:
        self._session = session
        self.url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key} if api_key else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: aiohttp.ClientTimeout = _PROBE_TIMEOUT,
        **kwargs: Any,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self.url}{path}",
                headers=self._headers,
                timeout=timeout,
                **kwargs,
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                if resp.status >= 400:
                    raise MoonrakerError(_error_text(resp.status, body))
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MoonrakerError(f"{self.url} unreachable: {err or type(err).__name__}") from err
        if not isinstance(body, dict):
            raise MoonrakerError(f"Unexpected reply from {self.url}{path}")
        return body.get("result", body)

    async def server_info(self) -> dict[str, Any] | None:
        """Return /server/info, or None while the host is not answering."""
        try:
            return await self._request("GET", "/server/info")
        except MoonrakerError:
            return None

    async def state_message(self) -> str:
        """Klipper's explanation of its current state (best effort)."""
        try:
            info = await self._request("GET", "/printer/info")
        except MoonrakerError:
            return ""
        return str(info.get("state_message", "")).strip()

    async def firmware_restart(self) -> None:
        """FIRMWARE_RESTART: reconnect all MCUs."""
        await self._request("POST", "/printer/firmware_restart")

    async def upload(
        self, path: Path, filename: str, subdir: str, start_print: bool
    ) -> dict[str, Any]:
        """Upload a G-code file, optionally starting the print."""
        loop = asyncio.get_running_loop()
        fh = await loop.run_in_executor(None, path.open, "rb")
        try:
            form = aiohttp.FormData()
            form.add_field("root", "gcodes")
            if subdir:
                form.add_field("path", subdir)
            form.add_field("print", "true" if start_print else "false")
            form.add_field(
                "file", fh, filename=filename, content_type="application/octet-stream"
            )
            return await self._request(
                "POST", "/server/files/upload", data=form, timeout=_UPLOAD_TIMEOUT
            )
        finally:
            await loop.run_in_executor(None, fh.close)


def _error_text(status: int, body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"Moonraker HTTP {status}: {err['message']}"
    return f"Moonraker HTTP {status}"
