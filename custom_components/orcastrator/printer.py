"""Per-printer job runner: power on, wait for Klipper, upload, print, idle off."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from homeassistant.core import CALLBACK_TYPE, Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
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
    DEFAULT_AUTO_POWER_OFF,
    DEFAULT_AUTO_POWER_ON,
    DEFAULT_BOOT_TIMEOUT,
    DEFAULT_COOLDOWN_TEMP,
    DEFAULT_ERROR_GRACE,
    DEFAULT_FIRMWARE_RESTART,
    DEFAULT_GRACEFUL_SHUTDOWN,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_NOTIFY_ON_ERROR,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RESTART_BACKOFF,
    EVENT_JOB,
)
from .moonraker import Moonraker, MoonrakerError, PrinterActivity

_LOGGER = logging.getLogger(__name__)

IDLE_CHECK_INTERVAL = 60.0
SHUTDOWN_WAIT_TIMEOUT = 25.0
SHUTDOWN_SETTLE_DELAY = 5.0
MAX_QUEUED_JOBS = 10

_MCU_RECOVERABLE_MARKERS = (
    "unable to connect",
    "lost communication with mcu",
    "timeout on connect",
    "cannot connect to mcu",
    "mcu protocol error",
)


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
    POWERING_OFF = "powering_off"
    CANCELLED = "cancelled"
    FAILED = "failed"


_TERMINAL_STATES = frozenset(
    {
        JobState.IDLE.value,
        JobState.PRINTING.value,
        JobState.PRINT_QUEUED.value,
        JobState.UPLOADED.value,
        JobState.CANCELLED.value,
        JobState.FAILED.value,
    }
)


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
    auto_power_off: bool = DEFAULT_AUTO_POWER_OFF
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    cooldown_temp: float = DEFAULT_COOLDOWN_TEMP
    graceful_shutdown: bool = DEFAULT_GRACEFUL_SHUTDOWN

    @property
    def match_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def entry_settings(entry: ConfigEntry) -> dict[str, Any]:
    """Current settings: options overlay initial data while keeping entry data name."""
    return {**entry.data, **entry.options, CONF_NAME: entry.data[CONF_NAME]}


def _num_val(settings: Mapping[str, Any], key: str, default: float) -> float:
    raw = settings.get(key)
    return float(default if raw is None else raw)


def _int_val(settings: Mapping[str, Any], key: str, default: int) -> int:
    raw = settings.get(key)
    return int(default if raw is None else raw)


def _bool_val(settings: Mapping[str, Any], key: str, default: bool) -> bool:
    raw = settings.get(key)
    return bool(default if raw is None else raw)


def printer_config(settings: Mapping[str, Any]) -> PrinterConfig:
    """Build a PrinterConfig from stored entry data or options."""
    raw_aliases = settings.get(CONF_ALIASES) or ()
    return PrinterConfig(
        name=settings[CONF_NAME],
        url=settings[CONF_URL],
        api_key=(str(settings.get(CONF_API_KEY) or "").strip() or None),
        power_entity=settings.get(CONF_POWER_ENTITY) or None,
        aliases=tuple(a for a in raw_aliases if isinstance(a, str) and a.strip()),
        boot_timeout=_num_val(settings, CONF_BOOT_TIMEOUT, DEFAULT_BOOT_TIMEOUT),
        auto_power_on=_bool_val(settings, CONF_AUTO_POWER_ON, DEFAULT_AUTO_POWER_ON),
        firmware_restart=_bool_val(settings, CONF_FIRMWARE_RESTART, DEFAULT_FIRMWARE_RESTART),
        poll_interval=_num_val(settings, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        max_restarts=_int_val(settings, CONF_MAX_RESTARTS, DEFAULT_MAX_RESTARTS),
        error_grace=_num_val(settings, CONF_ERROR_GRACE, DEFAULT_ERROR_GRACE),
        restart_backoff=_num_val(settings, CONF_RESTART_BACKOFF, DEFAULT_RESTART_BACKOFF),
        notify_on_error=_bool_val(settings, CONF_NOTIFY_ON_ERROR, DEFAULT_NOTIFY_ON_ERROR),
        auto_power_off=_bool_val(settings, CONF_AUTO_POWER_OFF, DEFAULT_AUTO_POWER_OFF),
        idle_timeout=_int_val(settings, CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT),
        cooldown_temp=_num_val(settings, CONF_COOLDOWN_TEMP, DEFAULT_COOLDOWN_TEMP),
        graceful_shutdown=_bool_val(
            settings, CONF_GRACEFUL_SHUTDOWN, DEFAULT_GRACEFUL_SHUTDOWN
        ),
    )


@dataclass
class Job:
    """One file sent from the slicer."""

    path: Path
    filename: str
    subdir: str
    start_print: bool
    context: Context | None = None
    received: datetime = field(default_factory=dt_util.utcnow)


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _is_recoverable_mcu_error(message: str) -> bool:
    """Return True only when Klipper's error clearly indicates an unready MCU."""
    lowered = message.casefold()
    return any(marker in lowered for marker in _MCU_RECOVERABLE_MARKERS)


def _is_klippy_ready(info: dict[str, Any] | None) -> bool:
    return bool(
        info
        and info.get("klippy_connected") is True
        and info.get("klippy_state") == "ready"
    )


class PrinterRuntime:
    """Runs jobs for one printer, one at a time, and monitors idle power-off."""

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
        self.last_filename: str | None = None
        self.last_start_print: bool | None = None
        self.detail: str | None = None
        self.last_error: str | None = None
        self.updated: datetime = dt_util.utcnow()
        self.idle_since: datetime | None = None
        self.idle_power_off_at: datetime | None = None

        self._pending: deque[Job] = deque()
        self._active_job: Job | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._cancel_lock = asyncio.Lock()
        self._power_lock = asyncio.Lock()
        self._cancel_requested = False
        self._shutting_down = False
        self._listeners: set[CALLBACK_TYPE] = set()
        self._idle_unsubs: list[CALLBACK_TYPE] = []

    @property
    def queued(self) -> int:
        return len(self._pending)

    @property
    def has_active_or_queued_jobs(self) -> bool:
        return self._active_job is not None or bool(self._pending)

    @property
    def notification_id(self) -> str:
        return f"orcastrator_{self.entry.entry_id}"

    @callback
    def async_start(self) -> None:
        """Start background idle monitoring if configured."""
        self._shutting_down = False
        self._sync_idle_monitor()

    async def async_shutdown(self) -> None:
        """Stop idle monitors and cancel active/queued jobs without emitting CANCELLED."""
        self._shutting_down = True
        self._clear_idle_monitor()
        while self._pending:
            queued_job = self._pending.popleft()
            await asyncio.shield(
                self.hass.async_add_executor_job(_unlink_quiet, queued_job.path)
            )
        task = self._worker_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.add(update)
        return lambda: self._listeners.discard(update)

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

    def restore_state(self, state_str: str, attrs: Mapping[str, Any]) -> bool:
        """Restore terminal sensor state after HA restart without firing events."""
        if state_str in _TERMINAL_STATES:
            self.state = JobState(state_str)
        elif state_str in {s.value for s in JobState}:
            self.state = JobState.IDLE
        else:
            return False
        filename = attrs.get("filename")
        if isinstance(filename, str):
            self.last_filename = filename
        start_print = attrs.get("start_print")
        if isinstance(start_print, bool):
            self.last_start_print = start_print
        last_error = attrs.get("last_error")
        if isinstance(last_error, str) and self.state == JobState.FAILED:
            self.last_error = last_error
        return True

    def as_event(self) -> dict[str, Any]:
        filename = self.job.filename if self.job else self.last_filename
        return {
            "printer": self.config.name,
            "entry_id": self.entry.entry_id,
            "state": str(self.state),
            "detail": self.detail,
            "filename": filename,
            "error": self.last_error if self.state == JobState.FAILED else None,
        }

    async def async_set_option(self, key: str, value: Any) -> None:
        """Update a runtime option in-place and persist it without reloading."""
        if getattr(self.config, key) == value:
            return
        merged = entry_settings(self.entry)
        merged[key] = value
        self.config = printer_config(merged)
        if key == CONF_AUTO_POWER_OFF:
            self._sync_idle_monitor()
        new_options = {k: v for k, v in merged.items() if k != CONF_NAME}
        if dict(self.entry.options) != new_options:
            self.hass.config_entries.async_update_entry(
                self.entry, options=new_options
            )
        self._notify_listeners()

    @callback
    def async_submit(self, job: Job) -> None:
        """Queue a job and return immediately; jobs run one at a time."""
        self.idle_since = None
        self.idle_power_off_at = None
        self._pending.append(job)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = self.entry.async_create_background_task(
                self.hass,
                self._worker_loop(),
                f"orcastrator {self.config.name}",
            )
        self._notify_listeners()

    async def async_cancel_jobs(self) -> bool:
        """Cancel active and queued jobs for this printer."""
        async with self._cancel_lock:
            had_work = self.has_active_or_queued_jobs
            if not had_work:
                return False
            while self._pending:
                queued_job = self._pending.popleft()
                await asyncio.shield(
                    self.hass.async_add_executor_job(_unlink_quiet, queued_job.path)
                )
            task = self._worker_task
            if task is not None and not task.done():
                self._cancel_requested = True
                try:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                finally:
                    self._cancel_requested = False
            persistent_notification.async_dismiss(self.hass, self.notification_id)
            self._notify_listeners()
            return True

    async def _worker_loop(self) -> None:
        try:
            while self._pending:
                job = self._pending.popleft()
                self._active_job = job
                self.job = job
                self.last_filename = job.filename
                self.last_start_print = job.start_print
                self._notify_listeners()
                try:
                    await self._process(job)
                finally:
                    self._active_job = None
                    await asyncio.shield(
                        self.hass.async_add_executor_job(_unlink_quiet, job.path)
                    )
        except asyncio.CancelledError:
            if self._cancel_requested and not self._shutting_down:
                self._set_state(JobState.CANCELLED)
            raise
        finally:
            self._worker_task = None
            self._notify_listeners()

    async def _process(self, job: Job) -> None:
        cfg = self.config
        try:
            self._set_state(JobState.CHECKING)
            await self._ensure_ready(job)
            if job.start_print:
                activity = await self._safe_printer_activity()
                if activity is not None and activity.is_busy:
                    detail = (
                        f": {activity.print_filename}" if activity.print_filename else ""
                    )
                    raise JobError(
                        f"Printer is busy ({activity.print_state}{detail})."
                    )
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
        elif job.start_print:
            err_msg = (
                f"{job.filename} was uploaded to {cfg.name}, "
                "but Klipper did not start the print (printer busy?)."
            )
            self.last_error = err_msg
            self._set_state(JobState.FAILED)
            if self.config.notify_on_error:
                persistent_notification.async_create(
                    self.hass,
                    f"**{job.filename}** was uploaded to {cfg.name} but the print "
                    "did not start (printer busy?).",
                    title=f"Orcastrator: {cfg.name}",
                    notification_id=self.notification_id,
                )
        else:
            self._set_state(JobState.UPLOADED)

    async def _safe_printer_activity(self) -> PrinterActivity | None:
        try:
            return await self.client.printer_activity()
        except MoonrakerError as err:
            _LOGGER.debug("%s: printer activity check skipped: %s", self.config.name, err)
            return None

    def _is_power_off(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_OFF

    async def _ensure_ready(self, job: Job) -> None:
        """Make sure Klipper is ready, powering the printer on if needed."""
        async with self._power_lock:
            info = await self.client.server_info()
            cfg = self.config
            power_off = self._is_power_off(cfg.power_entity) if cfg.power_entity else False
            if not job.start_print and info is not None:
                return
            if _is_klippy_ready(info) and not power_off:
                return

            was_offline = info is None or power_off
            powered_on = False
            if was_offline and cfg.power_entity:
                if cfg.auto_power_on:
                    powered_on = await self._power_on(cfg.power_entity, job.context)
                elif power_off:
                    raise JobError(
                        f"Printer is off ({cfg.power_entity}) and auto power-on is disabled."
                    )
        await self._wait_ready(allow_restart=powered_on)

    async def _power_on(self, entity_id: str, context: Context | None) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            raise JobError(f"Power entity {entity_id} does not exist.")
        if state.state == STATE_UNAVAILABLE:
            raise JobError(f"Power entity {entity_id} is unavailable.")
        if state.state == STATE_ON:
            return False
        self._set_state(JobState.POWERING_ON, entity_id)
        await self.hass.services.async_call(
            entity_id.split(".", 1)[0],
            "turn_on",
            {"entity_id": entity_id},
            blocking=True,
            context=context,
        )
        return True

    async def _wait_ready(self, allow_restart: bool) -> None:
        deadline = time.monotonic() + self.config.boot_timeout
        error_since: float | None = None
        last_restart: float | None = None
        restarts = 0
        saw_power_on = False
        state = "offline"
        while True:
            cfg = self.config
            if cfg.power_entity:
                power_state = self.hass.states.get(cfg.power_entity)
                if power_state is not None:
                    if power_state.state == STATE_ON:
                        saw_power_on = True
                    elif saw_power_on and power_state.state in (STATE_OFF, STATE_UNAVAILABLE):
                        raise JobError(
                            f"Power entity {cfg.power_entity} became {power_state.state} "
                            "while waiting for printer."
                        )

            info = await self.client.server_info()
            if _is_klippy_ready(info):
                return
            state = str(info.get("klippy_state", "unknown")) if info else "offline"
            self._set_state(JobState.WAITING, state)
            now = time.monotonic()

            if state in ("error", "shutdown"):
                error_since = error_since or now
                if now - error_since >= cfg.error_grace:
                    if last_restart is not None and now - last_restart < cfg.restart_backoff:
                        pass
                    else:
                        message = await self.client.state_message()
                        can_restart = (
                            allow_restart
                            and cfg.firmware_restart
                            and bool(info and info.get("klippy_connected"))
                            and _is_recoverable_mcu_error(message)
                        )
                        if not can_restart or restarts >= cfg.max_restarts:
                            raise JobError(
                                f"Klipper is in '{state}' state"
                                + (f": {message}" if message else ".")
                            )
                        _LOGGER.info("%s: Klipper %s, sending FIRMWARE_RESTART", cfg.name, state)
                        try:
                            await self.client.firmware_restart()
                        except MoonrakerError as err:
                            _LOGGER.debug("%s: firmware restart failed: %s", cfg.name, err)
                        restarts += 1
                        last_restart = time.monotonic()
                        error_since = None
            else:
                error_since = None

            if now >= deadline:
                detail = ""
                if state in ("error", "shutdown"):
                    msg = await self.client.state_message()
                    if msg:
                        detail = f" - {msg}"
                raise JobError(
                    f"Printer not ready after {int(cfg.boot_timeout)} s "
                    f"(Klipper state: {state}{detail})."
                )
            await asyncio.sleep(cfg.poll_interval)

    def _clear_idle_monitor(self) -> None:
        while self._idle_unsubs:
            unsub = self._idle_unsubs.pop()
            unsub()
        self.idle_since = None
        self.idle_power_off_at = None

    @callback
    def _sync_idle_monitor(self) -> None:
        self._clear_idle_monitor()
        if (
            self._shutting_down
            or not self.config.auto_power_off
            or not self.config.power_entity
        ):
            return
        self._idle_unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self.config.power_entity],
                self._on_power_state_change,
            )
        )
        self._idle_unsubs.append(
            async_track_time_interval(
                self.hass,
                self._on_idle_timer,
                timedelta(seconds=IDLE_CHECK_INTERVAL),
                name=f"orcastrator idle {self.config.name}",
                cancel_on_shutdown=True,
            )
        )

    @callback
    def _on_power_state_change(self, event: Event[EventStateChangedData]) -> None:
        if self.idle_since is not None or self.idle_power_off_at is not None:
            self.idle_since = None
            self.idle_power_off_at = None
            self._notify_listeners()

    async def _on_idle_timer(self, _now: datetime) -> None:
        await self.async_check_idle()

    def _reset_idle_timer(self) -> None:
        if self.idle_since is not None or self.idle_power_off_at is not None:
            self.idle_since = None
            self.idle_power_off_at = None
            self._notify_listeners()

    async def async_check_idle(self) -> None:
        """Check whether the printer is idle + cool and power it off after timeout."""
        cfg = self.config
        if (
            self._shutting_down
            or not cfg.auto_power_off
            or not cfg.power_entity
            or self.has_active_or_queued_jobs
            or self._power_lock.locked()
        ):
            self._reset_idle_timer()
            return

        power_state = self.hass.states.get(cfg.power_entity)
        if power_state is None or power_state.state != STATE_ON:
            self._reset_idle_timer()
            return

        try:
            info = await self.client.server_info()
            if not _is_klippy_ready(info):
                self._reset_idle_timer()
                return
            activity = await self.client.printer_activity()
        except MoonrakerError:
            self._reset_idle_timer()
            return

        if not activity.is_idle:
            self._reset_idle_timer()
            return

        now = dt_util.utcnow()
        if self.idle_since is None:
            self.idle_since = now
            self.idle_power_off_at = now + timedelta(minutes=cfg.idle_timeout)
            self._notify_listeners()
            return

        if now - self.idle_since < timedelta(minutes=cfg.idle_timeout):
            return

        if not activity.is_cool(cfg.cooldown_temp):
            _LOGGER.debug(
                "%s: idle timeout reached, waiting for extruder cooldown (< %.0f C)",
                cfg.name,
                cfg.cooldown_temp,
            )
            return

        await self._async_power_off_idle()

    async def _async_power_off_idle(self) -> None:
        async with self._power_lock:
            cfg = self.config
            entity_id = cfg.power_entity
            if (
                self._shutting_down
                or not cfg.auto_power_off
                or not entity_id
                or self.has_active_or_queued_jobs
            ):
                return
            power_state = self.hass.states.get(entity_id)
            if power_state is None or power_state.state != STATE_ON:
                self._reset_idle_timer()
                return

            try:
                activity = await self.client.printer_activity()
            except MoonrakerError:
                self._reset_idle_timer()
                return
            if not activity.is_idle or not activity.is_cool(cfg.cooldown_temp):
                if not activity.is_idle:
                    self._reset_idle_timer()
                return

            self._set_state(JobState.POWERING_OFF, "idle_timeout")
            if cfg.graceful_shutdown:
                try:
                    await self.client.shutdown_host()
                except MoonrakerError as err:
                    _LOGGER.warning("%s: Moonraker host shutdown failed: %s", cfg.name, err)
                else:
                    await self._wait_host_offline()

            await self.hass.services.async_call(
                entity_id.split(".", 1)[0],
                "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )
            self.idle_since = None
            self.idle_power_off_at = None
            self._set_state(JobState.IDLE, "powered_off_idle")

    async def _wait_host_offline(self) -> None:
        deadline = time.monotonic() + SHUTDOWN_WAIT_TIMEOUT
        poll = min(self.config.poll_interval, 2.0)
        while time.monotonic() < deadline:
            try:
                if await self.client.server_info() is None:
                    break
            except MoonrakerError:
                break
            await asyncio.sleep(poll)
        await asyncio.sleep(SHUTDOWN_SETTLE_DELAY)
