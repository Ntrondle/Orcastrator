"""Job status sensor, one per printer."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OrcastratorConfigEntry
from .entity import OrcastratorEntity
from .printer import JobState, PrinterRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OrcastratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([OrcastratorJobSensor(entry.runtime_data)])


class OrcastratorJobSensor(OrcastratorEntity, SensorEntity):
    """What Orcastrator is doing for this printer."""

    _attr_translation_key = "job"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, runtime: PrinterRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_job"
        self._attr_options = [s.value for s in JobState]
        self._sync_from_runtime()

    def _sync_from_runtime(self) -> None:
        rt = self._runtime
        self._attr_native_value = rt.state.value
        self._attr_extra_state_attributes = {
            "filename": rt.job.filename if rt.job else None,
            "start_print": rt.job.start_print if rt.job else None,
            "detail": rt.detail,
            "last_error": rt.last_error,
            "queued_jobs": rt.queued,
            "updated": rt.updated.isoformat(),
        }
