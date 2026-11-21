"""End to end: OrcaSlicer-style upload -> power on -> wait -> upload -> idle off."""

from __future__ import annotations

import asyncio
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import FormData
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.orcastrator import _purge_spool
from custom_components.orcastrator import http as http_mod
from custom_components.orcastrator import printer as printer_mod
from custom_components.orcastrator.const import (
    CONF_ALIASES,
    CONF_AUTO_POWER_OFF,
    CONF_AUTO_POWER_ON,
    CONF_BOOT_TIMEOUT,
    CONF_COOLDOWN_TEMP,
    CONF_ERROR_GRACE,
    CONF_FIRMWARE_RESTART,
    CONF_GRACEFUL_SHUTDOWN,
    CONF_IDLE_TIMEOUT,
    CONF_MAX_RESTARTS,
    CONF_NOTIFY_ON_ERROR,
    CONF_POLL_INTERVAL,
    CONF_POWER_ENTITY,
    CONF_RESTART_BACKOFF,
    DOMAIN,
    SECTION_ADVANCED,
)
from custom_components.orcastrator.diagnostics import async_get_config_entry_diagnostics

from .helpers import VORON_PRESET, FakeMoonraker, make_gcode

POWER = "input_boolean.voron_power"
SENSOR = "sensor.voron_job"
CANCEL_BUTTON = "button.voron_cancel_job"
AUTO_POWER_ON_SWITCH = "switch.voron_auto_power_on"
AUTO_POWER_OFF_SWITCH = "switch.voron_auto_power_off_when_idle"
FIRMWARE_RESTART_SWITCH = "switch.voron_firmware_restart_on_boot_error"
DONE = {"print_started", "uploaded", "failed", "print_queued", "cancelled"}


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, f"missing state for {entity_id}"
    return state


async def _setup(
    hass: HomeAssistant,
    serve: Any,
    *,
    powered_on: bool = False,
    always_reachable: bool = False,
    extra_data: dict[str, Any] | None = None,
    **fake_kwargs: Any,
) -> tuple[FakeMoonraker, MockConfigEntry]:
    await async_setup_component(
        hass,
        "input_boolean",
        {"input_boolean": {"voron_power": {"name": "Voron power", "initial": powered_on}}},
    )
    fake = FakeMoonraker(
        powered=(lambda: True) if always_reachable else (lambda: hass.states.is_state(POWER, "on")),
        **fake_kwargs,
    )
    url = await serve(fake.app())
    data: dict[str, Any] = {
        CONF_NAME: "Voron",
        CONF_URL: url,
        CONF_POWER_ENTITY: POWER,
        CONF_ALIASES: [VORON_PRESET],
        CONF_BOOT_TIMEOUT: 30,
        CONF_FIRMWARE_RESTART: True,
        CONF_POLL_INTERVAL: 0.01,
        CONF_ERROR_GRACE: 0,
        CONF_RESTART_BACKOFF: 0,
    }
    if extra_data:
        data.update(extra_data)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Voron",
        unique_id="voron",
        data=data,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return fake, entry


def _form(
    data: bytes,
    *,
    start: bool,
    filename: str = "Cube_PLA.gcode",
    path: str = "",
    plateindex: str | None = None,
) -> FormData:
    form = FormData(quote_fields=False)
    form.add_field("print", "true" if start else "false")
    form.add_field("path", path)
    if plateindex is not None:
        form.add_field("plateindex", plateindex)
    form.add_field("file", data, filename=filename, content_type="application/octet-stream")
    return form


async def _wait_done(hass: HomeAssistant, sensor_id: str = SENSOR) -> str:
    for _ in range(500):
        await asyncio.sleep(0.01)
        state = _state(hass, sensor_id).state
        if state in DONE:
            await hass.async_block_till_done(wait_background_tasks=True)
            return state
    raise AssertionError(f"job did not finish, state={_state(hass, sensor_id).state}")


async def test_version_and_upload_require_ha_token(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    await _setup(hass, serve)
    client = await hass_client_no_auth()

    resp = await client.get("/api/orcastrator/api/version")
    assert resp.status == 401
    resp = await client.get("/api/orcastrator/api/version", headers={"X-Api-Key": "nope"})
    assert resp.status == 401
    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=False),
    )
    assert resp.status == 401

    resp = await client.get(
        "/api/orcastrator/api/version", headers={"X-Api-Key": hass_access_token}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["api"] and body["text"].startswith("OctoPrint")

    # Bearer token also works for upload.
    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Phoenix"), start=False),
        headers={"Authorization": f"Bearer {hass_access_token}"},
    )
    assert resp.status == 400


async def test_power_on_wait_restart_and_print_preserves_utf8_filename(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    fake, _ = await _setup(hass, serve)
    client = await hass_client_no_auth()
    gcode = make_gcode(tag="Voron")
    events: list[str] = []
    hass.bus.async_listen("orcastrator_job", lambda e: events.append(e.data["state"]))

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(gcode, start=True, filename="Pièce (1).gcode", path="parts/voron"),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    assert body["orcastrator"]["printer"] == "Voron"
    assert "Location" in resp.headers

    assert await _wait_done(hass) == "print_started"
    assert _state(hass, POWER).state == "on"
    # Booted into recoverable MCU error -> one FIRMWARE_RESTART -> ready.
    assert fake.restarts == 1
    assert len(fake.uploads) == 1
    upload = fake.uploads[0]
    assert upload.filename == "Pièce (1).gcode"
    assert upload.fields["path"] == "parts/voron"
    assert upload.fields["print"] == "true"
    assert upload.fields["root"] == "gcodes"
    assert upload.content == gcode
    assert events[0] == "checking_printer"
    assert "powering_on" in events and "waiting_for_printer" in events
    assert events[-1] == "print_started"
    assert not list(http_mod.SPOOL_DIR.glob("*")), "spool file not cleaned up"


async def test_psu_only_smart_plug_powers_on_and_restarts(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    """Pi/Moonraker stays online while the printer PSU switch is off."""
    fake, _ = await _setup(hass, serve, powered_on=False, always_reachable=True)
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201
    assert await _wait_done(hass) == "print_started"
    assert _state(hass, POWER).state == "on"
    assert fake.restarts == 1
    assert len(fake.uploads) == 1


async def test_psu_only_thermal_shutdown_not_auto_restarted(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    """Even after powering on a PSU plug, thermal/safety shutdown must NEVER FIRMWARE_RESTART."""
    fake, _ = await _setup(
        hass,
        serve,
        powered_on=False,
        always_reachable=True,
        boot_states=[],
        settled_state="shutdown",
        state_message="Heater extruder not heating at expected rate",
    )
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201
    assert await _wait_done(hass) == "failed"
    assert fake.restarts == 0
    assert not fake.uploads
    assert "not heating" in _state(hass, SENSOR).attributes["last_error"]


async def test_running_printer_never_restarts_even_on_mcu_message(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    """Printer was already powered ON: never FIRMWARE_RESTART behind user's back."""
    fake, _ = await _setup(
        hass,
        serve,
        powered_on=True,
        boot_states=[],
        settled_state="shutdown",
        state_message="Lost communication with MCU 'mcu'",
    )
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201
    assert await _wait_done(hass) == "failed"
    assert fake.restarts == 0
    assert not fake.uploads


async def test_busy_printer_rejected_before_upload(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    fake, _ = await _setup(
        hass,
        serve,
        powered_on=True,
        boot_states=[],
        settled_state="ready",
        print_state="printing",
        print_filename="Benchy.gcode",
    )
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201
    assert await _wait_done(hass) == "failed"
    assert not fake.uploads
    assert "printing: Benchy.gcode" in _state(hass, SENSOR).attributes["last_error"]


async def test_start_print_not_started_marks_failed(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    fake, _ = await _setup(
        hass,
        serve,
        powered_on=True,
        boot_states=[],
        settled_state="ready",
        start_print_ok=False,
    )
    client = await hass_client_no_auth()

    with patch("homeassistant.components.persistent_notification.async_create") as notify:
        resp = await client.post(
            "/api/orcastrator/api/files/local",
            data=_form(make_gcode(tag="Voron"), start=True),
            headers={"X-Api-Key": hass_access_token},
        )
        assert resp.status == 201
        assert await _wait_done(hass) == "failed"
    assert len(fake.uploads) == 1
    assert "did not start" in _state(hass, SENSOR).attributes["last_error"]
    notify.assert_called_once()


async def test_moonraker_401_fails_fast_instead_of_boot_timeout(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    _, _ = await _setup(
        hass,
        serve,
        powered_on=True,
        api_key="correct-secret",
        extra_data={CONF_API_KEY: "wrong-secret", CONF_BOOT_TIMEOUT: 60},
    )
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201
    assert await _wait_done(hass) == "failed"
    assert "401" in _state(hass, SENSOR).attributes["last_error"]


async def test_auto_power_on_disabled_fails_when_off(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    fake, _ = await _setup(
        hass, serve, powered_on=False, extra_data={CONF_AUTO_POWER_ON: False}
    )
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201
    assert await _wait_done(hass) == "failed"
    assert _state(hass, POWER).state == "off"
    assert "auto power-on is disabled" in _state(hass, SENSOR).attributes["last_error"]
    assert not fake.uploads


async def test_notify_on_error_disabled(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    await _setup(
        hass,
        serve,
        powered_on=True,
        boot_states=[],
        settled_state="shutdown",
        state_message="Heater fault",
        extra_data={CONF_NOTIFY_ON_ERROR: False},
    )
    client = await hass_client_no_auth()

    with patch("homeassistant.components.persistent_notification.async_create") as notify:
        resp = await client.post(
            "/api/orcastrator/api/files/local",
            data=_form(make_gcode(tag="Voron"), start=True),
            headers={"X-Api-Key": hass_access_token},
        )
        assert resp.status == 201
        assert await _wait_done(hass) == "failed"

    notify.assert_not_called()
    assert "Heater fault" in _state(hass, SENSOR).attributes["last_error"]


async def test_preset_routing_large_file_upload_only(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    """Printer already up: no power toggle, 20 MB passes HA's 16 MB body limit,
    'Upload' (not 'Upload and Print') does not start the print."""
    fake, _ = await _setup(
        hass, serve, boot_states=[], settled_state="ready", always_reachable=True
    )
    client = await hass_client_no_auth()
    gcode = make_gcode(filler_bytes=20 * 1024 * 1024)

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(gcode, start=False),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201, await resp.text()
    assert await _wait_done(hass) == "uploaded"
    assert _state(hass, POWER).state == "off"
    assert fake.uploads[0].fields["print"] == "false"
    assert fake.uploads[0].content == gcode


async def test_3mf_http_upload_and_bgcode_rejection(
    hass: HomeAssistant,
    serve: Any,
    hass_client_no_auth: Any,
    hass_access_token: str,
    tmp_path: Path,
) -> None:
    fake, _ = await _setup(hass, serve, powered_on=True, boot_states=[], settled_state="ready")
    client = await hass_client_no_auth()

    # .bgcode rejected immediately with 400.
    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(b"GCDE\x01\x00", start=True, filename="part.bgcode"),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 400
    assert "Binary G-code" in await resp.text()

    # Multi-plate .gcode.3mf unpacked by plateindex=2.
    archive = tmp_path / "Multi.gcode.3mf"
    plate2 = make_gcode(tag="Voron")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", make_gcode(tag="Trident"))
        zf.writestr("Metadata/plate_2.gcode", plate2)
    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(archive.read_bytes(), start=True, filename="Multi.gcode.3mf", plateindex="2"),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201
    assert await _wait_done(hass) == "print_started"
    assert fake.uploads[0].filename == "Multi.gcode"
    assert fake.uploads[0].content == plate2
    assert not list(http_mod.SPOOL_DIR.glob("*"))


async def test_unknown_printer_rejected(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    fake, _ = await _setup(hass, serve)
    client = await hass_client_no_auth()

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Trident"), start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 400
    assert "Trident" in await resp.text()
    await hass.async_block_till_done()
    assert _state(hass, POWER).state == "off"
    assert _state(hass, SENSOR).state == "idle"
    assert not fake.uploads
    assert not list(http_mod.SPOOL_DIR.glob("*"))


async def test_cancel_job_button(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    fake, _ = await _setup(
        hass,
        serve,
        powered_on=True,
        boot_states=["startup"],
        settled_state="startup",
        extra_data={CONF_BOOT_TIMEOUT: 30},
    )
    client = await hass_client_no_auth()
    events: list[str] = []
    hass.bus.async_listen("orcastrator_job", lambda e: events.append(e.data["state"]))

    assert _state(hass, CANCEL_BUTTON).state == "unavailable"

    resp1 = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True, filename="first.gcode"),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp1.status == 201
    for _ in range(100):
        await asyncio.sleep(0.01)
        if _state(hass, SENSOR).state == "waiting_for_printer":
            break
    assert _state(hass, SENSOR).state == "waiting_for_printer"
    assert _state(hass, CANCEL_BUTTON).state != "unavailable"

    resp2 = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(make_gcode(tag="Voron"), start=True, filename="second.gcode"),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp2.status == 201
    assert _state(hass, SENSOR).attributes["queued_jobs"] == 1

    await hass.services.async_call(
        "button", "press", {"entity_id": CANCEL_BUTTON}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _state(hass, SENSOR).state == "cancelled"
    assert _state(hass, SENSOR).attributes["queued_jobs"] == 0
    assert _state(hass, CANCEL_BUTTON).state == "unavailable"
    assert "cancelled" in events
    assert not fake.uploads
    assert not list(http_mod.SPOOL_DIR.glob("*"))


async def test_config_switches_toggle_together_without_reloading(
    hass: HomeAssistant, serve: Any
) -> None:
    _, entry = await _setup(hass, serve)
    original_runtime = entry.runtime_data

    assert _state(hass, AUTO_POWER_ON_SWITCH).state == "on"
    assert _state(hass, AUTO_POWER_OFF_SWITCH).state == "off"
    assert _state(hass, FIRMWARE_RESTART_SWITCH).state == "on"

    # Toggle multiple config switches at once: must NOT reload the entry.
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": [AUTO_POWER_ON_SWITCH, FIRMWARE_RESTART_SWITCH]},
        blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": AUTO_POWER_OFF_SWITCH}, blocking=True
    )
    await hass.async_block_till_done()

    assert _state(hass, AUTO_POWER_ON_SWITCH).state == "off"
    assert _state(hass, AUTO_POWER_OFF_SWITCH).state == "on"
    assert _state(hass, FIRMWARE_RESTART_SWITCH).state == "off"
    assert entry.runtime_data is original_runtime
    assert original_runtime.config.auto_power_on is False
    assert original_runtime.config.auto_power_off is True
    assert original_runtime.config.firmware_restart is False


async def test_idle_auto_power_off_respects_heaters_and_cooldown(
    hass: HomeAssistant, serve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(printer_mod, "SHUTDOWN_WAIT_TIMEOUT", 1.0)
    monkeypatch.setattr(printer_mod, "SHUTDOWN_SETTLE_DELAY", 0.0)

    fake, entry = await _setup(
        hass,
        serve,
        powered_on=True,
        boot_states=[],
        settled_state="ready",
        heaters={"extruder": (200.0, 200.0), "heater_bed": (100.0, 100.0)},
        extra_data={
            CONF_AUTO_POWER_OFF: True,
            CONF_IDLE_TIMEOUT: 30,
            CONF_COOLDOWN_TEMP: 50,
            CONF_GRACEFUL_SHUTDOWN: True,
        },
    )
    rt = entry.runtime_data

    # 1. Heaters have active targets -> stays on, no idle_since set.
    await rt.async_check_idle()
    assert rt.idle_since is None
    assert _state(hass, POWER).state == "on"

    # 2. Targets zeroed (still hot at 200 C) -> idle timer starts.
    fake.heaters = {"extruder": (200.0, 0.0), "heater_bed": (85.0, 0.0)}
    await rt.async_check_idle()
    assert rt.idle_since is not None
    assert rt.idle_power_off_at is not None

    # 3. Past 30 min timeout, but extruder still >= 50 C -> waits for cooldown!
    rt.idle_since = dt_util.utcnow() - timedelta(minutes=31)
    await rt.async_check_idle()
    assert fake.shutdowns == 0
    assert _state(hass, POWER).state == "on"

    # 4. If print/heating starts again, idle timer resets.
    fake.idle_state = "Printing"
    await rt.async_check_idle()
    assert rt.idle_since is None

    # 5. Idle + cool (< 50 C) past 30 min -> graceful Moonraker shutdown + switch off!
    fake.idle_state = "Idle"
    fake.heaters = {"extruder": (42.0, 0.0), "heater_bed": (35.0, 0.0)}
    await rt.async_check_idle()
    assert rt.idle_since is not None
    rt.idle_since = dt_util.utcnow() - timedelta(minutes=31)
    await rt.async_check_idle()
    await hass.async_block_till_done()

    assert fake.shutdowns == 1
    assert _state(hass, POWER).state == "off"
    assert _state(hass, SENSOR).state == "idle"
    assert rt.idle_since is None


async def test_config_flow_and_alias_collision(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result.get("type") is FlowResultType.FORM
    placeholders = result.get("description_placeholders") or {}
    assert placeholders.get("url", "").endswith("/api/orcastrator")

    user_input = {
        CONF_NAME: "Mini CoreXY",
        CONF_URL: "192.168.1.52/",
        CONF_ALIASES: ["Mini CoreXY", "Prusa Mini", " "],
        SECTION_ADVANCED: {
            CONF_AUTO_POWER_ON: True,
            CONF_BOOT_TIMEOUT: 240,
            CONF_FIRMWARE_RESTART: True,
            CONF_MAX_RESTARTS: 4,
            CONF_POLL_INTERVAL: 2,
            CONF_ERROR_GRACE: 5,
            CONF_RESTART_BACKOFF: 15,
            CONF_NOTIFY_ON_ERROR: False,
            CONF_AUTO_POWER_OFF: True,
            CONF_IDLE_TIMEOUT: 45,
            CONF_COOLDOWN_TEMP: 45,
            CONF_GRACEFUL_SHUTDOWN: True,
        },
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert isinstance(data, dict)
    assert data[CONF_URL] == "http://192.168.1.52"
    assert data[CONF_ALIASES] == ["Mini CoreXY", "Prusa Mini"]
    assert data[CONF_AUTO_POWER_OFF] is True
    assert data[CONF_IDLE_TIMEOUT] == 45
    assert data[CONF_COOLDOWN_TEMP] == 45
    await hass.async_block_till_done()

    # Duplicate name shows inline error (preserving inputs).
    dup = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    dup = await hass.config_entries.flow.async_configure(
        dup["flow_id"], {**user_input, CONF_NAME: "mini_corexy"}
    )
    assert dup.get("type") is FlowResultType.FORM
    assert dup.get("errors") == {CONF_NAME: "already_configured"}

    # Conflicting alias shows inline alias_conflict error.
    alias_dup = await hass.config_entries.flow.async_configure(
        dup["flow_id"],
        {
            CONF_NAME: "Other Printer",
            CONF_URL: "192.168.1.53",
            CONF_ALIASES: ["Prusa Mini"],
            SECTION_ADVANCED: {},
        },
    )
    assert alias_dup.get("type") is FlowResultType.FORM
    assert alias_dup.get("errors") == {CONF_ALIASES: "alias_conflict"}


async def test_options_flow_edit_rename_and_clear_api_key(
    hass: HomeAssistant, serve: Any
) -> None:
    _, entry = await _setup(hass, serve, extra_data={CONF_API_KEY: "secret-token"})
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Trident",
        unique_id="trident",
        data={CONF_NAME: "Trident", CONF_URL: "http://192.168.1.51", CONF_BOOT_TIMEOUT: 300},
    )
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.FORM
    placeholders = result.get("description_placeholders") or {}
    assert placeholders.get("url", "").endswith("/api/orcastrator")

    # Reject renaming to another configured printer's slug.
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "trident",
            CONF_URL: "192.168.1.50",
            SECTION_ADVANCED: {},
        },
    )
    assert bad.get("type") is FlowResultType.FORM
    assert bad.get("errors") == {CONF_NAME: "already_configured"}

    # Edit, rename, preserve omitted advanced values from defaults/current, and clear API key.
    updated = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Voron 2",
            CONF_URL: "192.168.1.50:7125/",
            CONF_POWER_ENTITY: POWER,
            CONF_ALIASES: ["Voron 2.4"],
            SECTION_ADVANCED: {
                CONF_AUTO_POWER_ON: False,
                CONF_BOOT_TIMEOUT: 180,
                CONF_FIRMWARE_RESTART: False,
                CONF_MAX_RESTARTS: 2,
                CONF_POLL_INTERVAL: 5,
                CONF_ERROR_GRACE: 12,
                CONF_RESTART_BACKOFF: 30,
                CONF_NOTIFY_ON_ERROR: False,
                CONF_AUTO_POWER_OFF: True,
                CONF_IDLE_TIMEOUT: 20,
                CONF_COOLDOWN_TEMP: 45,
                CONF_GRACEFUL_SHUTDOWN: False,
            },
        },
    )
    assert updated.get("type") is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.title == "Voron 2"
    assert entry.unique_id == "voron_2"
    assert entry.data[CONF_NAME] == "Voron 2"
    assert CONF_API_KEY not in entry.data
    assert entry.runtime_data.config.name == "Voron 2"
    assert entry.runtime_data.config.url == "http://192.168.1.50:7125"
    assert entry.runtime_data.config.api_key is None
    assert entry.runtime_data.config.auto_power_off is True
    assert entry.runtime_data.config.idle_timeout == 20


async def test_diagnostics_redacts_secrets_and_handles_unloaded(
    hass: HomeAssistant, serve: Any
) -> None:
    _, entry = await _setup(hass, serve, extra_data={CONF_API_KEY: "super-secret-key"})
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry_data"][CONF_API_KEY] == "**REDACTED**"
    assert diag["effective_config"][CONF_API_KEY] == "**REDACTED**"
    assert diag["power_entity_state"] == "off"
    assert diag["runtime"]["state"] == "idle"

    assert await hass.config_entries.async_unload(entry.entry_id)
    unloaded_diag = await async_get_config_entry_diagnostics(hass, entry)
    assert unloaded_diag["runtime"] is None


def test_purge_spool_clears_leftovers(isolated_spool_dir: Path) -> None:
    isolated_spool_dir.mkdir(parents=True, exist_ok=True)
    stale = isolated_spool_dir / "orphan.gcode"
    stale.write_text("G28\n")
    _purge_spool()
    assert not stale.exists()


def test_translation_parity() -> None:
    base = Path("custom_components/orcastrator")
    strings = json.loads((base / "strings.json").read_text())
    en = json.loads((base / "translations/en.json").read_text())
    fr = json.loads((base / "translations/fr.json").read_text())
    assert strings == en

    def keys_shape(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: keys_shape(v) for k, v in obj.items()}
        return None

    assert keys_shape(strings) == keys_shape(fr)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x",
        "http://",
        "http://192.168.1.50/#/",
        "http://user:pass@192.168.1.50",
        "http://192.168.1.50:0",
    ],
)
async def test_config_flow_bad_url(hass: HomeAssistant, url: str) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "X",
            CONF_URL: url,
            SECTION_ADVANCED: {},
        },
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("errors") == {CONF_URL: "invalid_url"}
