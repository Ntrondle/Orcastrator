"""OctoPrint-compatible endpoints that OrcaSlicer uploads to.

OrcaSlicer host type "Octo/Klipper", host ``http://<ha>:8123/api/orcastrator``,
API key = a Home Assistant long-lived access token. It calls:

* ``GET  <host>/api/version``      (the "Test" button and before every upload)
* ``POST <host>/api/files/local``  multipart: ``file``, ``path``, ``print``,
  optional ``plateindex`` for .gcode.3mf uploads
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import tempfile
import uuid
from pathlib import Path
from typing import IO
from urllib.parse import quote

from aiohttp import BodyPartReader, ClientPayloadError, web
from aiohttp.http_exceptions import BadHttpMessage
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import User
from homeassistant.components.http.auth_util import async_user_not_allowed_do_auth
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import DATA_PRINTERS, MAX_UPLOAD_BYTES, URL_BASE, VERSION
from .gcode import (
    FileTooLargeError,
    SliceError,
    extract_plate_gcode,
    gcode_name_for,
    match_printer,
    read_slice_info,
    safe_filename,
    safe_subdir,
)
from .printer import MAX_QUEUED_JOBS, Job

_LOGGER = logging.getLogger(__name__)

SPOOL_DIR = Path(tempfile.gettempdir()) / "orcastrator"
_CHUNK = 1024 * 1024
_FIELD_CHUNK = 4096
_MAX_FIELD_BYTES = 4096
_MAX_PARTS = 16
_ALLOWED_FIELDS = frozenset({"path", "print", "select", "plateindex"})
_TRUE_VALUES = frozenset({"true", "1", "yes", "y"})


def _authorize(
    hass: HomeAssistant, request: web.Request, *, require_control: bool = False
) -> User:
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
    user = refresh_token.user
    if async_user_not_allowed_do_auth(hass, user, request) is not None:
        raise web.HTTPUnauthorized(
            text="Orcastrator: access denied from this network location."
        )
    if require_control and (
        user.system_generated
        or (not user.is_admin and any(g.id == GROUP_ID_READ_ONLY for g in user.groups))
    ):
        raise web.HTTPForbidden(
            text="Orcastrator: user does not have permission to start prints."
        )
    return user


class OrcastratorVersionView(HomeAssistantView):
    """GET /api/version — lets OrcaSlicer recognise an OctoPrint host."""

    url = f"{URL_BASE}/api/version"
    name = "api:orcastrator:version"
    requires_auth = False

    def __init__(self) -> None:
        self.extra_urls = [f"{URL_BASE}//api/version"]

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

    def __init__(self) -> None:
        self.extra_urls = [f"{URL_BASE}//api/files/{{location}}"]

    async def post(self, request: web.Request, location: str) -> web.Response:
        hass = request.app[KEY_HASS]
        user = _authorize(hass, request, require_control=True)
        if location not in ("local", "sdcard"):
            return _error(404, f"Unknown location '{location}'.")

        printers = hass.data.get(DATA_PRINTERS) or {}
        if not printers:
            return _error(503, "Orcastrator has no printers configured.")

        if not request.content_type.startswith("multipart/"):
            return _error(415, "Expected multipart/form-data upload.")
        if (
            request.content_length is not None
            and request.content_length > MAX_UPLOAD_BYTES + 1024 * 1024
        ):
            return _error(413, "File too large.")

        await hass.async_add_executor_job(_ensure_spool_dir)
        upload = SPOOL_DIR / uuid.uuid4().hex
        gcode = upload.with_suffix(".gcode")
        handed_off = False
        try:
            fields, filename = await _receive(hass, request, upload)
            if filename is None:
                return _error(400, "No file in upload.")
            filename = safe_filename(filename)
            if filename.lower().endswith(".bgcode"):
                raise SliceError(
                    "Binary G-code (.bgcode) is not supported. "
                    "Disable 'Supports binary G-code' in OrcaSlicer's Printer settings."
                )
            subdir = safe_subdir(fields.get("path", ""))
            start_print = fields.get("print", "false").strip().lower() in _TRUE_VALUES

            if filename.lower().endswith(".3mf"):
                plate = _parse_plate(fields.get("plateindex"))
                resolved_plate = await hass.async_add_executor_job(
                    extract_plate_gcode, upload, gcode, plate
                )
                filename = gcode_name_for(filename, resolved_plate)
            else:
                await hass.async_add_executor_job(upload.rename, gcode)

            info = await hass.async_add_executor_job(read_slice_info, gcode)
            entry_id = match_printer(
                info, {eid: rt.config.match_names for eid, rt in printers.items()}
            )
            runtime = printers.get(entry_id)
            if runtime is None:
                return _error(503, "Printer is currently reloading or unavailable.")
            if runtime.queued >= MAX_QUEUED_JOBS:
                return _error(
                    429, f"{runtime.config.name} job queue is full ({MAX_QUEUED_JOBS} jobs)."
                )

            _LOGGER.info(
                "%s -> %s (%s, print=%s)",
                filename,
                runtime.config.name,
                info.describe(),
                start_print,
            )
            runtime.async_submit(
                Job(gcode, filename, subdir, start_print, context=Context(user_id=user.id))
            )
            handed_off = True
        except FileTooLargeError as err:
            return _error(413, str(err))
        except _TooLarge:
            return _error(413, "File too large.")
        except SliceError as err:
            _LOGGER.warning("Rejected upload: %s", err)
            return _error(400, str(err))
        except (ValueError, KeyError, BadHttpMessage, ClientPayloadError) as err:
            return _error(400, f"Malformed upload request: {err}")
        except OSError as err:
            if err.errno == errno.ENOSPC:
                return _error(507, "Home Assistant spool storage is full.")
            raise
        finally:
            to_remove = [upload] if handed_off else [upload, gcode]
            await asyncio.shield(hass.async_add_executor_job(_unlink_paths, to_remove))

        name = f"{subdir}/{filename}" if subdir else filename
        resource_url = f"{str(request.url).rstrip('/')}/{quote(name, safe='/')}"
        return self.json(
            {
                "done": True,
                "files": {
                    location: {
                        "name": filename,
                        "path": name,
                        "display": filename,
                        "origin": location,
                        "refs": {"resource": resource_url},
                    }
                },
                "orcastrator": {"printer": runtime.config.name, "print": start_print},
            },
            status_code=201,
            headers={"Location": resource_url},
        )


class _TooLarge(Exception):
    pass


def _ensure_spool_dir() -> None:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)


def _unlink_paths(paths: list[Path]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


async def _receive(
    hass: HomeAssistant, request: web.Request, dest: Path
) -> tuple[dict[str, str], str | None]:
    """Stream the multipart body: small fields into a dict, the file to disk."""
    reader = await request.multipart()
    fields: dict[str, str] = {}
    filename: str | None = None
    parts = 0
    while (part := await reader.next()) is not None:
        parts += 1
        if parts > _MAX_PARTS:
            raise SliceError("Too many multipart fields in upload.")
        if not isinstance(part, BodyPartReader):
            continue
        if part.name == "file":
            if filename is not None:
                raise SliceError("Multiple files in one upload are not supported.")
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
                await asyncio.shield(hass.async_add_executor_job(fh.close))
        elif part.name in _ALLOWED_FIELDS:
            buf = bytearray()
            while chunk := await part.read_chunk(_FIELD_CHUNK):
                buf.extend(chunk)
                if len(buf) > _MAX_FIELD_BYTES:
                    raise SliceError(f"Form field '{part.name}' is too large.")
            fields[part.name] = buf.decode("utf-8", "replace")
        else:
            await part.release()
    return fields, filename


def _parse_plate(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as err:
        raise SliceError(f"Invalid plateindex '{value}'.") from err


def _error(status: int, message: str) -> web.Response:
    # Plain text: OrcaSlicer shows the body as-is in its error dialog.
    return web.Response(status=status, text=f"Orcastrator: {message}")
