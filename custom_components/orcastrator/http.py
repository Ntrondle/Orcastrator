"""OctoPrint-compatible endpoints that OrcaSlicer uploads to.

OrcaSlicer host type "Octo/Klipper", host ``http://<ha>:8123/api/orcastrator``,
API key = a Home Assistant long-lived access token. It calls:

* ``GET  <host>/api/version``      (the "Test" button and before every upload)
* ``POST <host>/api/files/local``  multipart: ``file``, ``path``, ``print``,
  optional ``plateindex`` for .gcode.3mf uploads
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import IO

from aiohttp import BodyPartReader, web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import DATA_PRINTERS, MAX_UPLOAD_BYTES, URL_BASE, VERSION
from .gcode import (
    SliceError,
    extract_plate_gcode,
    gcode_name_for,
    match_printer,
    read_slice_info,
    safe_filename,
    safe_subdir,
)
from .printer import Job

_LOGGER = logging.getLogger(__name__)

SPOOL_DIR = Path(tempfile.gettempdir()) / "orcastrator"
_CHUNK = 1024 * 1024


def _authorize(hass: HomeAssistant, request: web.Request) -> None:
    """Accept a Home Assistant access token as OctoPrint API key."""
    token = request.headers.get("X-Api-Key", "").strip()
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    refresh_token = hass.auth.async_validate_access_token(token) if token else None
    if refresh_token is None or not refresh_token.user.is_active:
        # Raised (not returned) so HA's ban middleware counts the failed login.
        raise web.HTTPUnauthorized(
            text="Orcastrator: API key must be a Home Assistant long-lived access token."
        )


class OrcastratorVersionView(HomeAssistantView):
    """GET /api/version — lets OrcaSlicer recognise an OctoPrint host."""

    url = f"{URL_BASE}/api/version"
    name = "api:orcastrator:version"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        _authorize(hass, request)
        return self.json(
            {"api": "0.1", "server": "1.10.0", "text": f"OctoPrint (Orcastrator {VERSION})"}
        )


class OrcastratorUploadView(HomeAssistantView):
    """POST /api/files/local — receive a sliced file and route it to a printer."""

    url = f"{URL_BASE}/api/files/{{location}}"
    name = "api:orcastrator:files"
    requires_auth = False

    async def post(self, request: web.Request, location: str) -> web.Response:
        hass = request.app[KEY_HASS]
        _authorize(hass, request)
        if location not in ("local", "sdcard"):
            return _error(404, f"Unknown location '{location}'.")

        printers = hass.data.get(DATA_PRINTERS) or {}
        if not printers:
            return _error(503, "Orcastrator has no printers configured.")

        await hass.async_add_executor_job(
            lambda: SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        )
        upload = SPOOL_DIR / uuid.uuid4().hex
        gcode = upload.with_suffix(".gcode")
        try:
            fields, filename = await _receive(hass, request, upload)
            if filename is None:
                return _error(400, "No file in upload.")
            filename = safe_filename(filename)
            subdir = safe_subdir(fields.get("path", ""))
            start_print = fields.get("print", "false").strip().lower() == "true"

            if filename.lower().endswith(".3mf"):
                plate = _int_or_none(fields.get("plateindex"))
                await hass.async_add_executor_job(extract_plate_gcode, upload, gcode, plate)
                filename = gcode_name_for(filename, plate)
            else:
                await hass.async_add_executor_job(upload.rename, gcode)

            info = await hass.async_add_executor_job(read_slice_info, gcode)
            entry_id = match_printer(
                info, {eid: rt.config.match_names for eid, rt in printers.items()}
            )
        except Exception as err:
            await hass.async_add_executor_job(gcode.unlink, True)
            if isinstance(err, SliceError):
                _LOGGER.warning("Rejected upload: %s", err)
                return _error(400, str(err))
            if isinstance(err, _TooLarge):
                return _error(413, "File too large.")
            raise
        finally:
            await hass.async_add_executor_job(upload.unlink, True)

        # From here the printer runtime owns (and deletes) the G-code file.
        runtime = printers[entry_id]
        _LOGGER.info(
            "%s -> %s (%s, print=%s)", filename, runtime.config.name, info.describe(), start_print
        )
        runtime.async_submit(Job(gcode, filename, subdir, start_print))

        name = f"{subdir}/{filename}" if subdir else filename
        return self.json(
            {
                "done": True,
                "files": {
                    location: {
                        "name": filename,
                        "path": name,
                        "display": filename,
                        "origin": location,
                        "refs": {"resource": f"{runtime.config.url}/server/files/gcodes/{name}"},
                    }
                },
                "orcastrator": {"printer": runtime.config.name, "print": start_print},
            },
            status_code=201,
        )


class _TooLarge(Exception):
    pass


async def _receive(
    hass: HomeAssistant, request: web.Request, dest: Path
) -> tuple[dict[str, str], str | None]:
    """Stream the multipart body: small fields into a dict, the file to disk."""
    reader = await request.multipart()
    fields: dict[str, str] = {}
    filename: str | None = None
    while (part := await reader.next()) is not None:
        if not isinstance(part, BodyPartReader):
            continue
        if part.name == "file" and filename is None:
            filename = part.filename or "upload.gcode"
            fh: IO[bytes] = await hass.async_add_executor_job(dest.open, "wb")
            size = 0
            try:
                while chunk := await part.read_chunk(_CHUNK):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise _TooLarge
                    await hass.async_add_executor_job(fh.write, chunk)
            finally:
                await hass.async_add_executor_job(fh.close)
        elif part.name:
            fields[part.name] = (await part.read(decode=True)).decode("utf-8", "replace")
    return fields, filename


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _error(status: int, message: str) -> web.Response:
    # Plain text: OrcaSlicer shows the body as-is in its error dialog.
    return web.Response(status=status, text=f"Orcastrator: {message}")
