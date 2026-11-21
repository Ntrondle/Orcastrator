"""Job status sensor, one per printer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass
from homeassistant.core import HomeAssistant

from .entity import OrcastratorEntity
from .printer import JobState, PrinterRuntime

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import OrcastratorConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OrcastratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([OrcastratorJobSensor(entry.runtime_data)])


class OrcastratorJobSensor(OrcastratorEntity, RestoreSensor):
    """What Orcastrator is doing for this printer."""

    _attr_translation_key = "job"
    _attr_device_class = SensorDeviceClass.ENUM
    _unrecorded_attributes = frozenset({"updated", "idle_since", "idle_power_off_at"})

    def __init__(self, runtime: PrinterRuntime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.entry.entry_id}_job"
        self._attr_options = [s.value for s in JobState]
        self._sync_from_runtime()

    async def async_added_to_hass(self) -> None:
        if (
            self._runtime.state == JobState.IDLE
            and self._runtime.job is None
            and self._runtime.last_filename is None
            and (last_state := await self.async_get_last_state()) is not None
        ):
            self._runtime.restore_state(last_state.state, last_state.attributes)
        await super().async_added_to_hass()

    def _sync_from_runtime(self) -> None:
        rt = self._runtime
        self._attr_native_value = rt.state.value
        filename = rt.job.filename if rt.job else rt.last_filename
        start_print = rt.job.start_print if rt.job else rt.last_start_print
        self._attr_extra_state_attributes = {
            "filename": filename,
            "start_print": start_print,
            "detail": rt.detail,
            "last_error": rt.last_error,
            "queued_jobs": rt.queued,
            "idle_since": rt.idle_since.isoformat() if rt.idle_since else None,
            "idle_power_off_at": (
                rt.idle_power_off_at.isoformat() if rt.idle_power_off_at else None
            ),
            "updated": rt.updated.isoformat(),
        }
