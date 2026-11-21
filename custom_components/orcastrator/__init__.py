"""Orcastrator: send from OrcaSlicer to Home Assistant, which powers the
printer on, waits for Klipper and starts the print."""

from __future__ import annotations

from homeassistant.components import persistent_notification
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
    "async_remove_entry",
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
    await hass.async_add_executor_job(_purge_spool)
    hass.http.register_view(OrcastratorVersionView())
    hass.http.register_view(OrcastratorUploadView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OrcastratorConfigEntry) -> bool:
    """Set up one printer."""
    runtime = PrinterRuntime(hass, entry, printer_config(entry_settings(entry)))
    entry.runtime_data = runtime
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.data[DATA_PRINTERS][entry.entry_id] = runtime
    runtime.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OrcastratorConfigEntry) -> bool:
    """Unload a printer; cancel running/queued jobs cleanly."""
    printers = hass.data.get(DATA_PRINTERS) or {}
    runtime = printers.pop(entry.entry_id, None)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        if runtime is not None:
            printers[entry.entry_id] = runtime
        return False
    if runtime is not None:
        await runtime.async_shutdown()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: OrcastratorConfigEntry) -> None:
    """Dismiss any lingering persistent notification when a printer is deleted."""
    persistent_notification.async_dismiss(hass, f"orcastrator_{entry.entry_id}")


async def _async_reload(hass: HomeAssistant, entry: OrcastratorConfigEntry) -> None:
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and printer_config(entry_settings(entry)) == runtime.config:
        return
    hass.config_entries.async_schedule_reload(entry.entry_id)


def _purge_spool() -> None:
    """Drop files left behind by a restart or crash."""
    if SPOOL_DIR.is_symlink() or not SPOOL_DIR.is_dir():
        return
    for path in SPOOL_DIR.iterdir():
        try:
            if not path.is_symlink() and path.is_file():
                path.unlink()
        except OSError:
            pass
