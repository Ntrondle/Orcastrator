"""Orcastrator: send from OrcaSlicer to Home Assistant, which powers the
printer on, waits for Klipper and starts the print."""

from __future__ import annotations

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DATA_PRINTERS, DOMAIN
from .http import SPOOL_DIR, OrcastratorUploadView, OrcastratorVersionView
from .printer import PrinterRuntime, entry_settings, printer_config

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR, Platform.BUTTON, Platform.SWITCH]

type OrcastratorConfigEntry = ConfigEntry[PrinterRuntime]

__all__ = [
    "CONFIG_SCHEMA",
    "OrcastratorConfigEntry",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "entry_settings",
    "printer_config",
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the OctoPrint-compatible endpoints once."""
    printers: dict[str, PrinterRuntime] = {}
    hass.data[DATA_PRINTERS] = printers
    hass.http.register_view(OrcastratorVersionView())
    hass.http.register_view(OrcastratorUploadView())
    await hass.async_add_executor_job(_purge_spool)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OrcastratorConfigEntry) -> bool:
    """Set up one printer."""
    runtime = PrinterRuntime(hass, entry, printer_config(entry_settings(entry)))
    entry.runtime_data = runtime
    hass.data[DATA_PRINTERS][entry.entry_id] = runtime
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OrcastratorConfigEntry) -> bool:
    """Unload a printer; its running job (if any) is cancelled."""
    hass.data[DATA_PRINTERS].pop(entry.entry_id, None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: OrcastratorConfigEntry) -> None:
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and runtime.consume_suppress_reload():
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _purge_spool() -> None:
    """Drop files left behind by a restart in the middle of a job."""
    if not SPOOL_DIR.is_dir():
        return
    cutoff = time.time() - 24 * 3600
    for path in SPOOL_DIR.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
