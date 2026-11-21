"""Minimal Moonraker client: readiness, activity, restart, shutdown, upload."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=5)
_RESTART_TIMEOUT = aiohttp.ClientTimeout(total=30)
_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=600)
_UPLOAD_CHUNK = 256 * 1024


class MoonrakerError(Exception):
    """Moonraker refused a request or could not be reached."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MoonrakerUnreachableError(MoonrakerError):
    """Moonraker is offline, unreachable, or still booting behind a proxy."""


class MoonrakerAuthError(MoonrakerError):
    """Moonraker rejected the request credentials (HTTP 401/403)."""


@dataclass(frozen=True)
class PrinterActivity:
    """Snapshot of print, idle, and heater state reported by Klipper/Moonraker."""

    print_state: str
    print_filename: str
    idle_state: str
    heaters: dict[str, tuple[float | None, float | None]]

    @property
    def is_busy(self) -> bool:
        return self.print_state in ("printing", "paused")

    @property
    def any_heater_active(self) -> bool:
        return any((target or 0.0) > 0.0 for _, target in self.heaters.values())

    @property
    def is_idle(self) -> bool:
        return (
            not self.is_busy
            and self.idle_state != "Printing"
            and not self.any_heater_active
        )

    def is_cool(self, cooldown_temp: float) -> bool:
        """Return True once all extruders are below ``cooldown_temp`` (and no heater active)."""
        if self.any_heater_active:
            return False
        if not self.heaters:
            return False
        extruders = [
            temp for name, (temp, _) in self.heaters.items() if name.startswith("extruder")
        ]
        if not extruders:
            return True
        return all(temp is not None and temp < cooldown_temp for temp in extruders)


def safe_url(raw_url: str) -> str:
    """Strip any embedded credentials, query string, or fragment from a URL."""
    try:
        parsed = URL(raw_url)
    except ValueError:
        return raw_url
    cleaned = parsed.with_user(None).with_password(None).with_query(None).with_fragment(None)
    return str(cleaned).rstrip("/")


class _AsyncFilePayload(aiohttp.AsyncIterablePayload):
    """Stream a file in an executor with known Content-Length."""

    def __init__(self, path: Path, size: int, loop: asyncio.AbstractEventLoop) -> None:
        self._path = path
        self._loop = loop
        super().__init__(self._chunks(), content_type="application/octet-stream")
        self._size = size

    async def _chunks(self) -> AsyncIterator[bytes]:
        fh = await self._loop.run_in_executor(None, self._path.open, "rb")
        try:
            while chunk := await self._loop.run_in_executor(None, fh.read, _UPLOAD_CHUNK):
                yield chunk
        finally:
            await asyncio.shield(self._loop.run_in_executor(None, fh.close))


class Moonraker:
    """Talks to one printer's Moonraker."""

    def __init__(
        self, session: aiohttp.ClientSession, url: str, api_key: str | None
    ) -> None:
        self._session = session
        self.url = url.rstrip("/")
        cleaned_key = (api_key or "").strip()
        self._headers = {"X-Api-Key": cleaned_key} if cleaned_key else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: aiohttp.ClientTimeout = _PROBE_TIMEOUT,
        **kwargs: Any,
    ) -> dict[str, Any]:
        display_url = safe_url(self.url)
        try:
            async with self._session.request(
                method,
                f"{self.url}{path}",
                headers=self._headers,
                timeout=timeout,
                allow_redirects=False,
                **kwargs,
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                if resp.status in (401, 403):
                    raise MoonrakerAuthError(
                        _error_text(resp.status, resp.reason, body), status=resp.status
                    )
                if resp.status in (502, 503, 504):
                    raise MoonrakerUnreachableError(
                        _error_text(resp.status, resp.reason, body), status=resp.status
                    )
                if resp.status >= 400:
                    raise MoonrakerError(
                        _error_text(resp.status, resp.reason, body), status=resp.status
                    )
        except aiohttp.ClientSSLError as err:
            raise MoonrakerError(
                f"{display_url} TLS error: {err or type(err).__name__}"
            ) from err
        except (
            aiohttp.ClientConnectorError,
            aiohttp.ClientOSError,
            aiohttp.ServerDisconnectedError,
            TimeoutError,
        ) as err:
            raise MoonrakerUnreachableError(
                f"{display_url} unreachable: {err or type(err).__name__}"
            ) from err
        except aiohttp.ClientError as err:
            raise MoonrakerError(
                f"{display_url} request failed: {err or type(err).__name__}"
            ) from err

        if not isinstance(body, dict):
            raise MoonrakerError(f"Unexpected reply from {display_url}{path}")
        result = body.get("result", body)
        if not isinstance(result, dict):
            raise MoonrakerError(f"Unexpected reply from {display_url}{path}")
        return result

    async def server_info(self) -> dict[str, Any] | None:
        """Return /server/info, or None while the host is unreachable/booting."""
        try:
            return await self._request("GET", "/server/info")
        except MoonrakerUnreachableError:
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
        await self._request("POST", "/printer/firmware_restart", timeout=_RESTART_TIMEOUT)

    async def query_objects(self, objects: Sequence[str]) -> dict[str, Any]:
        """Query Klipper printer objects via /printer/objects/query."""
        query = "&".join(quote(obj, safe="") for obj in objects)
        result = await self._request("GET", f"/printer/objects/query?{query}")
        status = result.get("status")
        if not isinstance(status, dict):
            raise MoonrakerError(
                f"Unexpected /printer/objects/query reply from {safe_url(self.url)}"
            )
        return status

    async def printer_activity(self) -> PrinterActivity:
        """Query print status, idle_timeout, and active/cooling heater temperatures."""
        status = await self.query_objects(("print_stats", "idle_timeout", "heaters"))
        print_stats = status.get("print_stats")
        idle_timeout = status.get("idle_timeout")
        heaters_obj = status.get("heaters")

        print_state = (
            str(print_stats.get("state", "standby")).casefold()
            if isinstance(print_stats, dict)
            else "standby"
        )
        print_filename = (
            str(print_stats.get("filename") or "")
            if isinstance(print_stats, dict)
            else ""
        )
        idle_state = (
            str(idle_timeout.get("state", "Idle"))
            if isinstance(idle_timeout, dict)
            else "Idle"
        )
        available_heaters = (
            [str(h) for h in heaters_obj.get("available_heaters", []) if isinstance(h, str)]
            if isinstance(heaters_obj, dict)
            else []
        )

        heaters: dict[str, tuple[float | None, float | None]] = {}
        if available_heaters:
            heater_status = await self.query_objects(available_heaters)
            for name in available_heaters:
                entry = heater_status.get(name)
                if isinstance(entry, dict):
                    temp = _as_float(entry.get("temperature"))
                    target = _as_float(entry.get("target"))
                    heaters[name] = (temp, target)

        return PrinterActivity(
            print_state=print_state,
            print_filename=print_filename,
            idle_state=idle_state,
            heaters=heaters,
        )

    async def shutdown_host(self) -> None:
        """Ask Moonraker to cleanly shut down the printer host OS."""
        try:
            await self._request("POST", "/machine/shutdown", timeout=_PROBE_TIMEOUT)
        except MoonrakerUnreachableError:
            # Host may drop the connection immediately while halting.
            return

    async def upload(
        self, path: Path, filename: str, subdir: str, start_print: bool
    ) -> dict[str, Any]:
        """Upload a G-code file, optionally starting the print."""
        loop = asyncio.get_running_loop()
        size = await loop.run_in_executor(None, lambda: path.stat().st_size)
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("root", "gcodes")
        if subdir:
            form.add_field("path", subdir)
        form.add_field("print", "true" if start_print else "false")
        form.add_field(
            "file",
            _AsyncFilePayload(path, size, loop),
            filename=filename,
            content_type="application/octet-stream",
        )
        return await self._request(
            "POST", "/server/files/upload", data=form, timeout=_UPLOAD_TIMEOUT
        )


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _error_text(status: int, reason: str | None, body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"Moonraker HTTP {status}: {err['message']}"
    if reason:
        return f"Moonraker HTTP {status} ({reason})"
    return f"Moonraker HTTP {status}"
