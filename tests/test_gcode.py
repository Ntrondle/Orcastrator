"""Printer detection from G-code (no Home Assistant needed)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from custom_components.orcastrator.gcode import (
    SliceError,
    SliceInfo,
    extract_plate_gcode,
    gcode_name_for,
    match_printer,
    read_slice_info,
    safe_filename,
    safe_subdir,
)

from .helpers import VORON_PRESET, make_gcode

PRINTERS = {
    "voron": ["Voron", VORON_PRESET],
    "trident": ["Trident", "Voron Trident 250 0.4 nozzle"],
    "mini": ["Mini", "Mini CoreXY"],
}


def _write(tmp_path: Path, data: bytes, name: str = "a.gcode") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_tag_in_start_gcode(tmp_path: Path) -> None:
    info = read_slice_info(_write(tmp_path, make_gcode(tag="Trident")))
    assert info.tag == "Trident"
    assert info.preset == VORON_PRESET
    # Tag wins over the preset name.
    assert match_printer(info, PRINTERS) == "trident"


def test_tag_in_escaped_printer_notes(tmp_path: Path) -> None:
    notes = "My notes\norcastrator_printer=Mini CoreXY\nmore notes"
    info = read_slice_info(_write(tmp_path, make_gcode(notes=notes, preset=None)))
    assert info.tag == "Mini CoreXY"
    assert match_printer(info, PRINTERS) == "mini"


def test_preset_fallback_in_large_file(tmp_path: Path) -> None:
    # Config block is only in the tail; file larger than both scan windows.
    info = read_slice_info(_write(tmp_path, make_gcode(filler_bytes=12 * 1024 * 1024)))
    assert info.tag is None
    assert info.preset == VORON_PRESET
    assert info.model is None  # empty "; printer_model =" must not eat the next line
    assert match_printer(info, PRINTERS) == "voron"


def test_name_normalisation() -> None:
    assert match_printer(SliceInfo(tag="mini-corexy"), PRINTERS) == "mini"
    assert match_printer(SliceInfo(tag="VORON"), PRINTERS) == "voron"


def test_unknown_tag_is_authoritative() -> None:
    with pytest.raises(SliceError, match="No Orcastrator printer matches the tag"):
        match_printer(SliceInfo(tag="Nope", preset=VORON_PRESET), PRINTERS)


def test_no_info() -> None:
    with pytest.raises(SliceError, match="No printer information"):
        match_printer(SliceInfo(), PRINTERS)


def test_ambiguous() -> None:
    with pytest.raises(SliceError, match="More than one"):
        match_printer(SliceInfo(tag="x"), {"a": ["x"], "b": ["X"]})


def test_extract_3mf(tmp_path: Path) -> None:
    src = tmp_path / "Cube.gcode.3mf"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", make_gcode(tag="Voron"))
        zf.writestr("Metadata/plate_2.gcode", make_gcode(tag="Mini"))
    dest = tmp_path / "out.gcode"
    with pytest.raises(SliceError, match="several plates"):
        extract_plate_gcode(src, dest, None)
    extract_plate_gcode(src, dest, 2)
    assert read_slice_info(dest).tag == "Mini"
    assert gcode_name_for("Cube.gcode.3mf", 2) == "Cube.gcode"
    assert gcode_name_for("Cube.3mf", 2) == "Cube_plate_2.gcode"


def test_safe_paths() -> None:
    assert safe_filename("../../etc/x.gcode") == "x.gcode"
    assert safe_filename("C:\\Users\\me\\x.gcode") == "x.gcode"
    assert safe_subdir("/orca//parts/") == "orca/parts"
    with pytest.raises(SliceError):
        safe_subdir("a/../../b")
