"""Work out which printer a sliced file is meant for.

Pure Python (no Home Assistant imports) so it can be tested on its own.

Two sources, in this order:

1. An explicit tag anywhere in a comment, e.g. in OrcaSlicer's printer
   "Notes" or the first line of "Machine start G-code"::

       ; orcastrator_printer = Voron 2.4

   If a tag is present it is authoritative: it must match a printer, or the
   upload is refused.
2. The printer preset name OrcaSlicer writes into the config block at the end
   of the file (``; printer_settings_id = Voron 2.4 300 0.4 nozzle``).
"""

from __future__ import annotations

import difflib
import os
import re
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .const import MAX_UPLOAD_BYTES

# The tag sits after the thumbnails near the top (start G-code) and in the
# config block at the bottom; the preset name only at the bottom.
SCAN_BYTES = 4 * 1024 * 1024
_MAX_VALUE_LEN = 256
_MAX_FILENAME_LEN = 200
_MAX_SUBDIR_LEN = 256
_MAX_SUBDIR_DEPTH = 8
_COPY_CHUNK = 1024 * 1024
_BGCODE_MAGIC = b"GCDE"

# Matches either a quoted value (supporting escaped \" inside config lines) or
# an unquoted value up to a quote/backslash/newline/semicolon.
_TAG_RE = re.compile(
    r"(?:^|[\s;]|\\[nr])orcastrator_printer[ \t]*[:=][ \t]*"
    r"(?:\\?\"([^\"\\\r\n]+)\\?\"|([^\s\"'\\\r\n;][^\"'\\\r\n;]*))",
    re.IGNORECASE,
)
_SETTING_RE = re.compile(
    r"^;[ \t]*(printer_settings_id|printer_model)[ \t]*=[ \t]*([^\r\n]*)$",
    re.MULTILINE,
)
_PLATE_RE = re.compile(r"^Metadata/plate_(\d{1,4})\.gcode$", re.IGNORECASE)


class SliceError(Exception):
    """The file cannot be routed to a printer."""


class FileTooLargeError(SliceError):
    """Extracted or uploaded file exceeds the size limit."""


@dataclass(frozen=True)
class SliceInfo:
    """Printer hints found in a G-code file."""

    tag: str | None = None
    preset: str | None = None
    model: str | None = None

    def describe(self) -> str:
        """Human readable summary for error messages."""
        parts = []
        if self.tag:
            parts.append(f"tag '{self.tag}'")
        if self.preset:
            parts.append(f"printer preset '{self.preset}'")
        if self.model:
            parts.append(f"printer model '{self.model}'")
        return ", ".join(parts) or "no printer information"


def slug(value: str) -> str:
    """Normalise a name for matching: 'Voron 2.4' == 'voron_2_4' == 'VORON-2.4'."""
    normalized = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^\w]+|_", "_", stripped.casefold()).strip("_")


def _clean_value(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw if ord(ch) >= 0x20 and ord(ch) != 0x7F).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_VALUE_LEN]


def _scan_text(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        magic = fh.read(len(_BGCODE_MAGIC))
        if magic == _BGCODE_MAGIC:
            raise SliceError(
                "Binary G-code (.bgcode) is not supported. "
                "Disable 'Supports binary G-code' in OrcaSlicer's Printer settings."
            )
        fh.seek(0)
        if size <= 2 * SCAN_BYTES:
            data = fh.read()
        else:
            head = fh.read(SCAN_BYTES).rsplit(b"\n", 1)[0]
            fh.seek(size - SCAN_BYTES)
            tail = fh.read().split(b"\n", 1)[-1]
            data = head + b"\n" + tail
    text = data.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _extract_tag(line: str) -> str | None:
    for match in _TAG_RE.finditer(line):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if value := _clean_value(raw):
            return value
    return None


def read_slice_info(path: Path) -> SliceInfo:
    """Read the printer hints from a G-code file (blocking)."""
    text = _scan_text(path)

    notes_tag: str | None = None
    comment_tag: str | None = None
    start_gcode_tag: str | None = None
    in_config_block = False

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped == "; CONFIG_BLOCK_START":
            in_config_block = True
            continue
        if stripped == "; CONFIG_BLOCK_END":
            in_config_block = False
            continue

        if in_config_block:
            if stripped.startswith(";"):
                body = stripped[1:].lstrip()
                if body.startswith("printer_notes") and "=" in body:
                    if tag := _extract_tag(body.split("=", 1)[1]):
                        notes_tag = notes_tag or tag
                elif (
                    body.startswith("machine_start_gcode")
                    and "=" in body
                    and (tag := _extract_tag(body.split("=", 1)[1]))
                ):
                    start_gcode_tag = start_gcode_tag or tag
            continue

        if ";" in line and comment_tag is None:
            _, _, comment = line.partition(";")
            if tag := _extract_tag(comment):
                comment_tag = tag

    settings: dict[str, str] = {}
    for key, raw_val in _SETTING_RE.findall(text):
        if cleaned := _clean_value(raw_val):
            settings[key] = cleaned

    return SliceInfo(
        tag=notes_tag or comment_tag or start_gcode_tag,
        preset=settings.get("printer_settings_id"),
        model=settings.get("printer_model"),
    )


def match_printer(info: SliceInfo, printers: Mapping[str, Iterable[str]]) -> str:
    """Return the id of the printer the file belongs to.

    ``printers`` maps a printer id to the names it answers to (its own name
    plus any aliases / OrcaSlicer preset names).
    """
    display_names: dict[str, str] = {}
    keys: dict[str, set[str]] = {}
    for pid, names in printers.items():
        name_list = list(names)
        display_names[pid] = name_list[0] if name_list else pid
        keys[pid] = {s for n in name_list if (s := slug(n))}

    def lookup(value: str) -> list[str]:
        wanted = slug(value)
        if not wanted:
            return []
        return [pid for pid, names in keys.items() if wanted in names]

    if info.tag:
        hits = lookup(info.tag)
        source = f"tag 'orcastrator_printer = {info.tag}'"
        wanted_val = info.tag
    elif info.preset:
        hits = lookup(info.preset)
        source = f"printer preset '{info.preset}'"
        wanted_val = info.preset
    else:
        configured = ", ".join(display_names.values())
        hint = f" Configured printers: {configured}." if configured else ""
        raise SliceError(
            "No printer information in the G-code. Add "
            "'orcastrator_printer = <name>' to the printer's Notes in OrcaSlicer."
            + hint
        )

    if not hits:
        configured = ", ".join(display_names.values())
        suggestion = ""
        all_names = [n for names in printers.values() for n in names if n]
        if close := difflib.get_close_matches(wanted_val, all_names, n=1, cutoff=0.6):
            suggestion = f" Did you mean '{close[0]}'?"
        suffix = f" Configured printers: {configured}." if configured else ""
        raise SliceError(f"No Orcastrator printer matches the {source}.{suggestion}{suffix}")
    if len(hits) > 1:
        matched = ", ".join(display_names[pid] for pid in hits)
        raise SliceError(
            f"More than one Orcastrator printer matches the {source}: {matched}."
        )
    return hits[0]


def extract_plate_gcode(
    src: Path, dest: Path, plate: int | None, *, max_bytes: int = MAX_UPLOAD_BYTES
) -> int:
    """Extract the G-code of one plate from a .gcode.3mf (blocking)."""
    if plate is not None and plate < 1:
        raise SliceError(f"Invalid plate index {plate}.")
    try:
        with zipfile.ZipFile(src) as archive:
            plates = {
                int(m.group(1)): name
                for name in archive.namelist()
                if (m := _PLATE_RE.match(name.replace("\\", "/").lstrip("./")))
            }
            if not plates:
                raise SliceError("Uploaded .3mf contains no sliced G-code.")
            if plate is None:
                if len(plates) > 1:
                    raise SliceError("Uploaded .3mf has several plates; send one plate.")
                plate = next(iter(plates))
            if plate not in plates:
                available = ", ".join(str(p) for p in sorted(plates))
                raise SliceError(
                    f"Uploaded .3mf has no plate {plate} (available: {available})."
                )
            info = archive.getinfo(plates[plate])
            if info.file_size > max_bytes:
                raise FileTooLargeError("Extracted G-code exceeds maximum allowed size.")
            written = 0
            with archive.open(info) as inner, dest.open("wb") as out:
                while chunk := inner.read(_COPY_CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise FileTooLargeError("Extracted G-code exceeds maximum allowed size.")
                    out.write(chunk)
            return plate
    except SliceError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        EOFError,
        RuntimeError,
        NotImplementedError,
    ) as err:
        raise SliceError(f"Uploaded .3mf could not be read: {err}") from err


def gcode_name_for(filename: str, plate: int | None) -> str:
    """Name to give the extracted G-code of an uploaded .3mf."""
    stem = filename[: -len(".3mf")] if filename.lower().endswith(".3mf") else filename
    if stem.lower().endswith(".gcode"):
        base_stem = stem[: -len(".gcode")]
        if not base_stem or base_stem in (".", ".."):
            raise SliceError("Upload has no file name.")
        return stem
    if not stem or stem in (".", ".."):
        raise SliceError("Upload has no file name.")
    resolved_plate = plate if plate is not None else 1
    return f"{stem}_plate_{resolved_plate}.gcode"


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def safe_filename(name: str) -> str:
    """Strip any directory part from an uploaded file name."""
    if _has_control_chars(name):
        raise SliceError("Upload file name contains invalid characters.")
    cleaned = re.sub(r"^[A-Za-z]:", "", name.replace("\\", "/"))
    base = os.path.basename(cleaned).strip()
    if (
        base in ("", ".", "..")
        or base.startswith(".")
        or len(base) > _MAX_FILENAME_LEN
    ):
        raise SliceError("Upload has an invalid file name.")
    return base


def safe_subdir(path: str) -> str:
    """Sanitise the optional target folder OrcaSlicer sends."""
    if _has_control_chars(path):
        raise SliceError("Invalid upload path.")
    parts = [p.strip() for p in path.replace("\\", "/").split("/")]
    parts = [p for p in parts if p not in ("", ".")]
    if any(p == ".." or ":" in p or p.startswith(".") for p in parts):
        raise SliceError("Invalid upload path.")
    if len(parts) > _MAX_SUBDIR_DEPTH:
        raise SliceError("Invalid upload path.")
    joined = "/".join(parts)
    if len(joined) > _MAX_SUBDIR_LEN:
        raise SliceError("Invalid upload path.")
    return joined
