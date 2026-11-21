"""Configuration switches for Orcastrator printers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .const import CONF_AUTO_POWER_OFF, CONF_AUTO_POWER_ON, CONF_FIRMWARE_RESTART
from .entity import OrcastratorEntity
from .printer import PrinterRuntime

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import OrcastratorConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OrcastratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        [
            OrcastratorConfigSwitch(runtime, CONF_AUTO_POWER_ON, "auto_power_on"),
            OrcastratorConfigSwitch(runtime, CONF_AUTO_POWER_OFF, "auto_power_off"),
            OrcastratorConfigSwitch(runtime, CONF_FIRMWARE_RESTART, "firmware_restart"),
        ]
    )


class OrcastratorConfigSwitch(OrcastratorEntity, SwitchEntity):
    """Toggle a printer option without editing config entry options."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: PrinterRuntime, option_key: str, translation_key: str) -> None:
        super().__init__(runtime)
        self._option_key = option_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{runtime.entry.entry_id}_{translation_key}"
        self._sync_from_runtime()

    def _sync_from_runtime(self) -> None:
        cfg = self._runtime.config
        self._attr_is_on = bool(getattr(cfg, self._option_key))
        self._attr_available = (
            self._option_key not in (CONF_AUTO_POWER_ON, CONF_AUTO_POWER_OFF)
            or cfg.power_entity is not None
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._runtime.async_set_option(self._option_key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._runtime.async_set_option(self._option_key, False)
