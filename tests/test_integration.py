"""End to end: OrcaSlicer-style upload -> power on -> wait -> upload to Moonraker."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import FormData
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.orcastrator.const import (
    CONF_ALIASES,
    CONF_AUTO_POWER_ON,
    CONF_BOOT_TIMEOUT,
    CONF_ERROR_GRACE,
    CONF_FIRMWARE_RESTART,
    CONF_MAX_RESTARTS,
    CONF_NOTIFY_ON_ERROR,
    CONF_POLL_INTERVAL,
    CONF_POWER_ENTITY,
    CONF_RESTART_BACKOFF,
    DOMAIN,
    SECTION_ADVANCED,
)
from custom_components.orcastrator.diagnostics import async_get_config_entry_diagnostics
from custom_components.orcastrator.http import SPOOL_DIR

from .helpers import VORON_PRESET, FakeMoonraker, make_gcode

POWER = "input_boolean.voron_power"
SENSOR = "sensor.voron_job"
CANCEL_BUTTON = "button.voron_cancel_job"
AUTO_POWER_SWITCH = "switch.voron_auto_power_on"
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


def _form(data: bytes, *, start: bool, filename: str = "Cube_PLA.gcode") -> FormData:
    form = FormData()
    form.add_field("print", "true" if start else "false")
    form.add_field("path", "")
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


async def test_version_requires_ha_token(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    await _setup(hass, serve)
    client = await hass_client_no_auth()

    resp = await client.get("/api/orcastrator/api/version")
    assert resp.status == 401
    resp = await client.get("/api/orcastrator/api/version", headers={"X-Api-Key": "nope"})
    assert resp.status == 401

    resp = await client.get(
        "/api/orcastrator/api/version", headers={"X-Api-Key": hass_access_token}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["api"] and body["text"].startswith("OctoPrint")


async def test_power_on_wait_restart_and_print(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    fake, _ = await _setup(hass, serve)
    client = await hass_client_no_auth()
    gcode = make_gcode(tag="Voron")
    events: list[str] = []
    hass.bus.async_listen("orcastrator_job", lambda e: events.append(e.data["state"]))

    resp = await client.post(
        "/api/orcastrator/api/files/local",
        data=_form(gcode, start=True),
        headers={"X-Api-Key": hass_access_token},
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["orcastrator"]["printer"] == "Voron"

    assert await _wait_done(hass) == "print_started"
    assert _state(hass, POWER).state == "on"
    # Booted into "error" (MCU not up) -> one FIRMWARE_RESTART -> ready.
    assert fake.restarts == 1
    assert len(fake.uploads) == 1
    upload = fake.uploads[0]
    assert upload.filename == "Cube_PLA.gcode"
    assert upload.fields["print"] == "true"
    assert upload.fields["root"] == "gcodes"
    assert upload.content == gcode
    assert events[0] == "checking_printer"
    assert "powering_on" in events and "waiting_for_printer" in events
    assert events[-1] == "print_started"
    assert not list(SPOOL_DIR.glob("*")), "spool file not cleaned up"


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
    assert not list(SPOOL_DIR.glob("*"))


async def test_error_on_running_printer_is_not_cleared(
    hass: HomeAssistant, serve: Any, hass_client_no_auth: Any, hass_access_token: str
) -> None:
    """Printer was already on and in shutdown: report, never FIRMWARE_RESTART."""
    fake, _ = await _setup(
        hass,
        serve,
        powered_on=True,
        boot_states=[],
        settled_state="shutdown",
        state_message="Heater extruder not heating at expected rate",
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
    assert fake.restarts == 0
    assert not fake.uploads
    attrs = _state(hass, SENSOR).attributes
    assert "not heating" in attrs["last_error"]
    notify.assert_called_once()
    assert "Cube_PLA.gcode" in notify.call_args.args[1]
    assert "not heating" in notify.call_args.args[1]


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
    assert "cancelled" in events
    assert not fake.uploads
    assert not list(SPOOL_DIR.glob("*"))


async def test_config_switches_update_options_without_reloading(
    hass: HomeAssistant, serve: Any
) -> None:
    _, entry = await _setup(hass, serve)
    original_runtime = entry.runtime_data

    assert _state(hass, AUTO_POWER_SWITCH).state == "on"
    assert _state(hass, FIRMWARE_RESTART_SWITCH).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": AUTO_POWER_SWITCH}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": FIRMWARE_RESTART_SWITCH}, blocking=True
    )
    await hass.async_block_till_done()

    assert _state(hass, AUTO_POWER_SWITCH).state == "off"
    assert _state(hass, FIRMWARE_RESTART_SWITCH).state == "off"
    assert entry.runtime_data is original_runtime
    assert original_runtime.config.auto_power_on is False
    assert original_runtime.config.firmware_restart is False
    assert entry.options[CONF_AUTO_POWER_ON] is False
    assert entry.options[CONF_FIRMWARE_RESTART] is False


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result.get("type") is FlowResultType.FORM
    placeholders = result.get("description_placeholders") or {}
    assert placeholders.get("url", "").endswith("/api/orcastrator")

    user_input = {
        CONF_NAME: "Mini CoreXY",
        CONF_URL: "192.168.1.52/",
        CONF_ALIASES: ["Mini CoreXY", " "],
        SECTION_ADVANCED: {
            CONF_AUTO_POWER_ON: True,
            CONF_BOOT_TIMEOUT: 240,
            CONF_FIRMWARE_RESTART: True,
            CONF_MAX_RESTARTS: 4,
            CONF_POLL_INTERVAL: 2,
            CONF_ERROR_GRACE: 5,
            CONF_RESTART_BACKOFF: 15,
            CONF_NOTIFY_ON_ERROR: False,
        },
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert isinstance(data, dict)
    assert data[CONF_URL] == "http://192.168.1.52"
    assert data[CONF_ALIASES] == ["Mini CoreXY"]
    assert data[CONF_MAX_RESTARTS] == 4
    assert data[CONF_NOTIFY_ON_ERROR] is False
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**user_input, CONF_NAME: "mini_corexy"}
    )
    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "already_configured"


async def test_options_flow_edit_and_rename(hass: HomeAssistant, serve: Any) -> None:
    _, entry = await _setup(hass, serve)
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

    updated = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Voron 2",
            CONF_URL: "192.168.1.50:7125/",
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
            },
        },
    )
    assert updated.get("type") is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.title == "Voron 2"
    assert entry.unique_id == "voron_2"
    assert entry.data[CONF_NAME] == "Voron 2"
    assert entry.runtime_data.config.name == "Voron 2"
    assert entry.runtime_data.config.url == "http://192.168.1.50:7125"
    assert entry.runtime_data.config.max_restarts == 2
    assert entry.runtime_data.config.notify_on_error is False


async def test_diagnostics_redacts_api_key(hass: HomeAssistant, serve: Any) -> None:
    _, entry = await _setup(hass, serve, extra_data={CONF_API_KEY: "super-secret-key"})
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry_data"][CONF_API_KEY] == "**REDACTED**"
    assert diag["effective_config"][CONF_API_KEY] == "**REDACTED**"
    assert diag["runtime"]["state"] == "idle"


@pytest.mark.parametrize("url", ["ftp://x", "http://"])
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
