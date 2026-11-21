"""Printer detection from G-code (no Home Assistant needed)."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from custom_components.orcastrator.gcode import (
    SCAN_BYTES,
    FileTooLargeError,
    SliceError,
    SliceInfo,
    extract_plate_gcode,
    gcode_name_for,
    match_printer,
    read_slice_info,
    safe_filename,
    safe_subdir,
    slug,
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
    notes = 'My notes\norcastrator_printer = "Mini CoreXY"\nmore notes'
    info = read_slice_info(_write(tmp_path, make_gcode(notes=notes, preset=None)))
    assert info.tag == "Mini CoreXY"
    assert match_printer(info, PRINTERS) == "mini"


def test_printer_notes_beats_filament_notes(tmp_path: Path) -> None:
    data = b"\n".join(
        [
            b"; CONFIG_BLOCK_START",
            b"; filament_notes = orcastrator_printer = Trident",
            b"; printer_notes = orcastrator_printer = Mini CoreXY",
            b"; CONFIG_BLOCK_END",
        ]
    )
    info = read_slice_info(_write(tmp_path, data))
    assert info.tag == "Mini CoreXY"
    assert match_printer(info, PRINTERS) == "mini"


def test_crlf_line_endings_and_empty_model(tmp_path: Path) -> None:
    info = read_slice_info(_write(tmp_path, make_gcode(newline="\r\n")))
    assert info.tag is None
    assert info.preset == VORON_PRESET
    assert info.model is None
    assert match_printer(info, PRINTERS) == "voron"


def test_preset_fallback_in_large_file(tmp_path: Path) -> None:
    # Config block is only in the tail; file larger than both scan windows.
    info = read_slice_info(_write(tmp_path, make_gcode(filler_bytes=12 * 1024 * 1024)))
    assert info.tag is None
    assert info.preset == VORON_PRESET
    assert info.model is None  # empty "; printer_model =" must not eat the next line
    assert match_printer(info, PRINTERS) == "voron"


def test_split_boundary_line_in_large_file(tmp_path: Path) -> None:
    # Put a tag straddling the exact 4 MiB head boundary; tail has the intact tag.
    pad = b"G1 X1 Y1\n" * ((SCAN_BYTES - 15) // 9)
    filler = pad + b"; orcastrator_printer = Trident\n" + b"G1 X1 Y1\n" * 600_000
    tail = (
        b"\n; CONFIG_BLOCK_START\n; printer_notes = orcastrator_printer = Trident\n"
        b"; CONFIG_BLOCK_END\n"
    )
    info = read_slice_info(_write(tmp_path, filler + tail))
    assert info.tag == "Trident"


def test_setting_regex_linear_on_adversarial_comment(tmp_path: Path) -> None:
    # Previously triggered O(N^2) backtracking in _SETTING_RE.
    evil = b"; printer_settings_id = a" + b" " * 50_000 + b"b\n"
    start = time.monotonic()
    info = read_slice_info(_write(tmp_path, evil))
    assert time.monotonic() - start < 0.5
    assert info.preset is not None and info.preset.startswith("a")


def test_name_normalisation_and_unicode_accents() -> None:
    assert slug("Prusa Édition") == "prusa_edition"
    assert slug("Büro") == "buro"
    assert match_printer(SliceInfo(tag="mini-corexy"), PRINTERS) == "mini"
    assert match_printer(SliceInfo(tag="VORON"), PRINTERS) == "voron"
    assert match_printer(SliceInfo(tag="Prusa Édition"), {"p": ["Prusa Edition"]}) == "p"


def test_unknown_tag_includes_hint_and_configured_printers() -> None:
    with pytest.raises(SliceError, match=r"Did you mean 'Voron'.*Configured printers: Voron"):
        match_printer(SliceInfo(tag="Vorom", preset=VORON_PRESET), PRINTERS)


def test_no_info() -> None:
    with pytest.raises(SliceError, match="No printer information"):
        match_printer(SliceInfo(), PRINTERS)


def test_ambiguous() -> None:
    with pytest.raises(SliceError, match=r"More than one.*A, B"):
        match_printer(SliceInfo(tag="x"), {"a": ["A", "x"], "b": ["B", "X"]})


def test_bgcode_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, b"GCDE\x01\x00\x00\x00binary", name="part.bgcode")
    with pytest.raises(SliceError, match="Binary G-code"):
        read_slice_info(path)


def test_extract_3mf_and_zip_bomb_cap(tmp_path: Path) -> None:
    src = tmp_path / "Cube.gcode.3mf"
    with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.gcode", make_gcode(tag="Voron"))
        zf.writestr("Metadata/plate_2.gcode", make_gcode(tag="Mini"))
    dest = tmp_path / "out.gcode"
    with pytest.raises(SliceError, match="several plates"):
        extract_plate_gcode(src, dest, None)
    assert extract_plate_gcode(src, dest, 2) == 2
    assert read_slice_info(dest).tag == "Mini"
    assert gcode_name_for("Cube.gcode.3mf", 2) == "Cube.gcode"
    assert gcode_name_for("Cube.3mf", 2) == "Cube_plate_2.gcode"

    with pytest.raises(FileTooLargeError, match="maximum allowed size"):
        extract_plate_gcode(src, dest, 1, max_bytes=32)

    single = tmp_path / "SinglePlate2.3mf"
    with zipfile.ZipFile(single, "w") as zf:
        zf.writestr("metadata/Plate_2.gcode", make_gcode(tag="Mini"))
    resolved = extract_plate_gcode(single, dest, None)
    assert resolved == 2
    assert gcode_name_for("SinglePlate2.3mf", resolved) == "SinglePlate2_plate_2.gcode"

    bad = _write(tmp_path, b"not-a-zip", name="bad.3mf")
    with pytest.raises(SliceError, match="could not be read"):
        extract_plate_gcode(bad, dest, None)


def test_safe_paths() -> None:
    assert safe_filename("../../etc/x.gcode") == "x.gcode"
    assert safe_filename("C:\\Users\\me\\x.gcode") == "x.gcode"
    assert safe_subdir("/orca//parts/") == "orca/parts"
    with pytest.raises(SliceError):
        safe_subdir("a/../../b")
    with pytest.raises(SliceError):
        safe_subdir("a/ .. /b")
    with pytest.raises(SliceError):
        safe_filename("bad\r\n.gcode")
    with pytest.raises(SliceError):
        safe_filename(".hidden.gcode")
