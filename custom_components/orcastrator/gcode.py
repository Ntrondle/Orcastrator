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

import os
import re
import shutil
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

# The tag sits after the thumbnails near the top (start G-code) and in the
# config block at the bottom; the preset name only at the bottom.
SCAN_BYTES = 4 * 1024 * 1024

# Inside the config block multi-line values are escaped, so a value ends at a
# literal backslash ("\n"), a quote, a semicolon or the end of the line.
_TAG_RE = re.compile(r"orcastrator_printer\s*[:=]\s*\"?([^\"\\\r\n;]+)", re.IGNORECASE)
_SETTING_RE = re.compile(
    r"^;[ \t]*(printer_settings_id|printer_model)[ \t]*=[ \t]*(.*?)[ \t]*$", re.MULTILINE
)
_PLATE_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")


class SliceError(Exception):
    """The file cannot be routed to a printer."""


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
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _scan_text(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size <= 2 * SCAN_BYTES:
            data = fh.read()
        else:
            head = fh.read(SCAN_BYTES)
            fh.seek(size - SCAN_BYTES)
            data = head + b"\n" + fh.read()
    return data.decode("utf-8", errors="replace")


def read_slice_info(path: Path) -> SliceInfo:
    """Read the printer hints from a G-code file (blocking)."""
    text = _scan_text(path)
    tag = None
    for line in text.splitlines():
        if not line.lstrip().startswith(";"):
            continue
        if (match := _TAG_RE.search(line)) and (value := match.group(1).strip()):
            tag = value
            break
    settings = {key: value for key, value in _SETTING_RE.findall(text) if value}
    return SliceInfo(
        tag=tag,
        preset=settings.get("printer_settings_id"),
        model=settings.get("printer_model"),
    )


def match_printer(info: SliceInfo, printers: Mapping[str, Iterable[str]]) -> str:
    """Return the id of the printer the file belongs to.

    ``printers`` maps a printer id to the names it answers to (its own name
    plus any aliases / OrcaSlicer preset names).
    """
    keys = {pid: {slug(n) for n in names if slug(n)} for pid, names in printers.items()}

    def lookup(value: str) -> list[str]:
        wanted = slug(value)
        return [pid for pid, names in keys.items() if wanted in names]

    if info.tag:
        hits = lookup(info.tag)
        source = f"tag 'orcastrator_printer = {info.tag}'"
    elif info.preset:
        hits = lookup(info.preset)
        source = f"printer preset '{info.preset}'"
    else:
        raise SliceError(
            "No printer information in the G-code. Add "
            "'orcastrator_printer = <name>' to the printer's Notes in OrcaSlicer."
        )

    if not hits:
        raise SliceError(f"No Orcastrator printer matches the {source}.")
    if len(hits) > 1:
        raise SliceError(f"More than one Orcastrator printer matches the {source}.")
    return hits[0]


def extract_plate_gcode(src: Path, dest: Path, plate: int | None) -> None:
    """Extract the G-code of one plate from a .gcode.3mf (blocking)."""
    try:
        archive = zipfile.ZipFile(src)
    except zipfile.BadZipFile as err:
        raise SliceError("Uploaded .3mf is not a valid archive.") from err
    with archive:
        plates = {
            int(m.group(1)): name
            for name in archive.namelist()
            if (m := _PLATE_RE.match(name))
        }
        if not plates:
            raise SliceError("Uploaded .3mf contains no sliced G-code.")
        if plate is None:
            if len(plates) > 1:
                raise SliceError("Uploaded .3mf has several plates; send one plate.")
            plate = next(iter(plates))
        if plate not in plates:
            raise SliceError(f"Uploaded .3mf has no plate {plate}.")
        with archive.open(plates[plate]) as inner, dest.open("wb") as out:
            shutil.copyfileobj(inner, out, 1024 * 1024)


def gcode_name_for(filename: str, plate: int | None) -> str:
    """Name to give the extracted G-code of an uploaded .3mf."""
    stem = filename[: -len(".3mf")] if filename.lower().endswith(".3mf") else filename
    if stem.lower().endswith(".gcode"):
        return stem
    return f"{stem}_plate_{plate or 1}.gcode"


def safe_filename(name: str) -> str:
    """Strip any directory part from an uploaded file name."""
    base = os.path.basename(name.replace("\\", "/")).strip()
    if base in ("", ".", ".."):
        raise SliceError("Upload has no file name.")
    return base


def safe_subdir(path: str) -> str:
    """Sanitise the optional target folder OrcaSlicer sends."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise SliceError("Invalid upload path.")
    return "/".join(parts)
