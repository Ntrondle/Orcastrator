"""Button entities for Orcastrator printers."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OrcastratorConfigEntry
from .entity import OrcastratorEntity
from .printer import PrinterRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OrcastratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([OrcastratorCancelJobButton(entry.runtime_data)])


class OrcastratorCancelJobButton(OrcastratorEntity, ButtonEntity):
    """Cancel active and queued jobs waiting on this printer."""

    _attr_translation_key = "cancel_job"

    def __init__(self, runtime: PrinterRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_cancel_job"

    async def async_press(self) -> None:
        await self._runtime.async_cancel_jobs()
