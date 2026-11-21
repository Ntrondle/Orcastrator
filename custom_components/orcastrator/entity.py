"""Shared base entity for Orcastrator printer devices."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, VERSION
from .printer import PrinterRuntime


class OrcastratorEntity(Entity):
    """Base entity attached to an Orcastrator printer device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: PrinterRuntime) -> None:
        self._runtime = runtime
        entry_id = runtime.entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=runtime.config.name,
            manufacturer="Orcastrator",
            model="Klipper (Moonraker)",
            sw_version=VERSION,
            configuration_url=runtime.config.url,
        )

    def _sync_from_runtime(self) -> None:
        """Update cached entity attributes from the runtime."""

    @callback
    def _handle_runtime_update(self) -> None:
        self._sync_from_runtime()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._sync_from_runtime()
        self.async_on_remove(self._runtime.async_add_listener(self._handle_runtime_update))
