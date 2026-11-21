"""Per-printer job runner: power on, wait for Klipper, upload, print."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_API_KEY,
    CONF_NAME,
    CONF_URL,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
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
    DEFAULT_AUTO_POWER_ON,
    DEFAULT_BOOT_TIMEOUT,
    DEFAULT_ERROR_GRACE,
    DEFAULT_FIRMWARE_RESTART,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_NOTIFY_ON_ERROR,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RESTART_BACKOFF,
    EVENT_JOB,
)
from .moonraker import Moonraker, MoonrakerError

_LOGGER = logging.getLogger(__name__)


class JobState(StrEnum):
    """What Orcastrator is doing for a printer."""

    IDLE = "idle"
    CHECKING = "checking_printer"
    POWERING_ON = "powering_on"
    WAITING = "waiting_for_printer"
    UPLOADING = "uploading"
    PRINTING = "print_started"
    PRINT_QUEUED = "print_queued"
    UPLOADED = "uploaded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class JobError(Exception):
    """A job could not be completed."""


@dataclass(frozen=True)
class PrinterConfig:
    """Settings of one printer."""

    name: str
    url: str
    api_key: str | None
    power_entity: str | None
    aliases: tuple[str, ...]
    boot_timeout: float
    auto_power_on: bool = DEFAULT_AUTO_POWER_ON
    firmware_restart: bool = DEFAULT_FIRMWARE_RESTART
    poll_interval: float = DEFAULT_POLL_INTERVAL
    max_restarts: int = DEFAULT_MAX_RESTARTS
    error_grace: float = DEFAULT_ERROR_GRACE
    restart_backoff: float = DEFAULT_RESTART_BACKOFF
    notify_on_error: bool = DEFAULT_NOTIFY_ON_ERROR

    @property
    def match_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def entry_settings(entry: ConfigEntry) -> dict[str, Any]:
    """Current settings: options (if edited) replace the initial data."""
    if entry.options:
        return {CONF_NAME: entry.data[CONF_NAME], **entry.options}
    return dict(entry.data)


def printer_config(settings: Mapping[str, Any]) -> PrinterConfig:
    """Build a PrinterConfig from stored entry data or options."""
    return PrinterConfig(
        name=settings[CONF_NAME],
        url=settings[CONF_URL],
        api_key=settings.get(CONF_API_KEY) or None,
        power_entity=settings.get(CONF_POWER_ENTITY) or None,
        aliases=tuple(a for a in settings.get(CONF_ALIASES, []) if a.strip()),
        boot_timeout=float(settings.get(CONF_BOOT_TIMEOUT, DEFAULT_BOOT_TIMEOUT)),
        auto_power_on=bool(settings.get(CONF_AUTO_POWER_ON, DEFAULT_AUTO_POWER_ON)),
        firmware_restart=bool(settings.get(CONF_FIRMWARE_RESTART, DEFAULT_FIRMWARE_RESTART)),
        poll_interval=float(settings.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
        max_restarts=int(settings.get(CONF_MAX_RESTARTS, DEFAULT_MAX_RESTARTS)),
        error_grace=float(settings.get(CONF_ERROR_GRACE, DEFAULT_ERROR_GRACE)),
        restart_backoff=float(settings.get(CONF_RESTART_BACKOFF, DEFAULT_RESTART_BACKOFF)),
        notify_on_error=bool(settings.get(CONF_NOTIFY_ON_ERROR, DEFAULT_NOTIFY_ON_ERROR)),
    )


@dataclass
class Job:
    """One file sent from the slicer."""

    path: Path
    filename: str
    subdir: str
    start_print: bool
    received: datetime = field(default_factory=dt_util.utcnow)


class PrinterRuntime:
    """Runs jobs for one printer, one at a time."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, config: PrinterConfig
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.config = config
        self.client = Moonraker(
            async_get_clientsession(hass), config.url, config.api_key
        )
        self.state = JobState.IDLE
        self.job: Job | None = None
        self.detail: str | None = None
        self.last_error: str | None = None
        self.updated: datetime = dt_util.utcnow()
        self.queued = 0
        self._lock = asyncio.Lock()
        self._listeners: list[CALLBACK_TYPE] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._cancelling = False
        self._suppress_reload = False

    @property
    def notification_id(self) -> str:
        return f"orcastrator_{self.entry.entry_id}"

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify_listeners(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def _set_state(self, state: JobState, detail: str | None = None) -> None:
        if state == self.state and detail == self.detail:
            return
        self.state = state
        self.detail = detail
        self.updated = dt_util.utcnow()
        _LOGGER.debug("%s: %s %s", self.config.name, state, detail or "")
        self.hass.bus.async_fire(EVENT_JOB, self.as_event())
        self._notify_listeners()

    def as_event(self) -> dict[str, Any]:
        return {
            "printer": self.config.name,
            "entry_id": self.entry.entry_id,
            "state": str(self.state),
            "detail": self.detail,
            "filename": self.job.filename if self.job else None,
            "error": self.last_error if self.state == JobState.FAILED else None,
        }

    def consume_suppress_reload(self) -> bool:
        """Return True once if an in-place switch update triggered the listener."""
        if self._suppress_reload:
            self._suppress_reload = False
            return True
        return False

    async def async_set_option(self, key: str, value: Any) -> None:
        """Update a runtime option in-place and persist it without reloading."""
        merged = entry_settings(self.entry)
        merged[key] = value
        self.config = printer_config(merged)
        new_options = {k: v for k, v in merged.items() if k != CONF_NAME}
        if dict(self.entry.options) != new_options:
            self._suppress_reload = True
            changed = self.hass.config_entries.async_update_entry(
                self.entry, options=new_options
            )
            if not changed:
                self._suppress_reload = False
        self._notify_listeners()

    @callback
    def async_submit(self, job: Job) -> None:
        """Queue a job and return immediately; jobs run one at a time."""
        self.queued += 1
        started = [False]
        task = self.entry.async_create_background_task(
            self.hass,
            self._run(job, started),
            f"orcastrator {self.config.name} {job.filename}",
        )
        self._tasks.add(task)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            self._tasks.discard(done_task)
            if not started[0]:
                self.queued = max(0, self.queued - 1)
                self.hass.async_add_executor_job(job.path.unlink, True)
                self._notify_listeners()

        task.add_done_callback(_on_done)
        self._notify_listeners()

    async def async_cancel_jobs(self) -> bool:
        """Cancel all active and queued jobs for this printer."""
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            return False
        self._cancelling = True
        try:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._cancelling = False
        persistent_notification.async_dismiss(self.hass, self.notification_id)
        return True

    async def _run(self, job: Job, started: list[bool]) -> None:
        started[0] = True
        acquired = False
        try:
            async with self._lock:
                acquired = True
                self.queued = max(0, self.queued - 1)
                self.job = job
                self._notify_listeners()
                await self._process(job)
        except asyncio.CancelledError:
            if not acquired:
                self.queued = max(0, self.queued - 1)
            if self._cancelling:
                if acquired:
                    self._set_state(JobState.CANCELLED)
                else:
                    self._notify_listeners()
            raise
        finally:
            await self.hass.async_add_executor_job(job.path.unlink, True)

    async def _process(self, job: Job) -> None:
        cfg = self.config
        try:
            self._set_state(JobState.CHECKING)
            await self._ensure_ready()
            self._set_state(JobState.UPLOADING)
            result = await self.client.upload(
                job.path, job.filename, job.subdir, job.start_print
            )
        except Exception as err:  # every failure must reach the user
            if isinstance(err, (JobError, MoonrakerError, HomeAssistantError)):
                _LOGGER.warning("%s: %s failed: %s", cfg.name, job.filename, err)
            else:
                _LOGGER.exception("%s: %s failed", cfg.name, job.filename)
            self.last_error = str(err) or type(err).__name__
            self._set_state(JobState.FAILED)
            if self.config.notify_on_error:
                persistent_notification.async_create(
                    self.hass,
                    f"**{job.filename}** was not sent to {cfg.name}.\n\n{err}",
                    title=f"Orcastrator: {cfg.name}",
                    notification_id=self.notification_id,
                )
            return

        self.last_error = None
        persistent_notification.async_dismiss(self.hass, self.notification_id)
        if result.get("print_started"):
            self._set_state(JobState.PRINTING)
        elif result.get("print_queued"):
            self._set_state(JobState.PRINT_QUEUED)
        else:
            if job.start_print and self.config.notify_on_error:
                # Uploaded but Klipper did not start it (busy printing?).
                persistent_notification.async_create(
                    self.hass,
                    f"**{job.filename}** was uploaded to {cfg.name} but the print "
                    "did not start (printer busy?).",
                    title=f"Orcastrator: {cfg.name}",
                    notification_id=self.notification_id,
                )
            self._set_state(JobState.UPLOADED)

    def _is_power_off(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_OFF

    async def _ensure_ready(self) -> None:
        """Make sure Klipper is ready, powering the printer on if needed."""
        cfg = self.config
        info = await self.client.server_info()
        if info and info.get("klippy_state") == "ready":
            return

        power_off = self._is_power_off(cfg.power_entity) if cfg.power_entity else False
        # Auto-restart Klipper when the printer was offline OR its PSU plug
        # was switched off (e.g. Pi/Moonraker stays on while MCU PSU is off).
        # An error on a printer that was already powered on is never cleared
        # behind the user's back.
        was_offline = info is None or power_off
        if was_offline and cfg.power_entity:
            if cfg.auto_power_on:
                await self._power_on(cfg.power_entity)
            elif power_off:
                raise JobError(
                    f"Printer is off ({cfg.power_entity}) and auto power-on is disabled."
                )
        await self._wait_ready(allow_restart=was_offline)

    async def _power_on(self, entity_id: str) -> None:
        state = self.hass.states.get(entity_id)
        if state is None:
            raise JobError(f"Power entity {entity_id} does not exist.")
        if state.state == STATE_UNAVAILABLE:
            raise JobError(f"Power entity {entity_id} is unavailable.")
        if state.state == STATE_ON:
            return
        self._set_state(JobState.POWERING_ON, entity_id)
        await self.hass.services.async_call(
            entity_id.split(".", 1)[0], "turn_on", {"entity_id": entity_id}, blocking=True
        )

    async def _wait_ready(self, allow_restart: bool) -> None:
        deadline = time.monotonic() + self.config.boot_timeout
        error_since: float | None = None
        last_restart: float | None = None
        restarts = 0
        state = "offline"
        while True:
            cfg = self.config
            info = await self.client.server_info()
            state = str(info.get("klippy_state", "unknown")) if info else "offline"
            if state == "ready":
                return
            self._set_state(JobState.WAITING, state)
            now = time.monotonic()

            if state in ("error", "shutdown"):
                error_since = error_since or now
                if now - error_since >= cfg.error_grace:
                    if not (allow_restart and cfg.firmware_restart) or restarts >= cfg.max_restarts:
                        message = await self.client.state_message()
                        raise JobError(
                            f"Klipper is in '{state}' state"
                            + (f": {message}" if message else ".")
                        )
                    if last_restart is None or now - last_restart >= cfg.restart_backoff:
                        _LOGGER.info("%s: Klipper %s, sending FIRMWARE_RESTART", cfg.name, state)
                        try:
                            await self.client.firmware_restart()
                        except MoonrakerError as err:
                            _LOGGER.debug("%s: firmware restart failed: %s", cfg.name, err)
                        restarts += 1
                        last_restart = now
                        error_since = None
            else:
                error_since = None

            if now >= deadline:
                raise JobError(
                    f"Printer not ready after {int(cfg.boot_timeout)} s "
                    f"(Klipper state: {state})."
                )
            await asyncio.sleep(cfg.poll_interval)
