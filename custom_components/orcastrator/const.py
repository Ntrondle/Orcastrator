"""Constants for Orcastrator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .printer import PrinterRuntime

DOMAIN: Final = "orcastrator"
VERSION: Final = "0.1.0"

# Registry of configured printers, keyed by config entry id.
DATA_PRINTERS: HassKey[dict[str, PrinterRuntime]] = HassKey(DOMAIN)

CONF_POWER_ENTITY: Final = "power_entity"
CONF_ALIASES: Final = "aliases"
CONF_AUTO_POWER_ON: Final = "auto_power_on"
CONF_BOOT_TIMEOUT: Final = "boot_timeout"
CONF_FIRMWARE_RESTART: Final = "firmware_restart"
CONF_MAX_RESTARTS: Final = "max_restarts"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_ERROR_GRACE: Final = "error_grace"
CONF_RESTART_BACKOFF: Final = "restart_backoff"
CONF_NOTIFY_ON_ERROR: Final = "notify_on_error"
SECTION_ADVANCED: Final = "advanced"

DEFAULT_AUTO_POWER_ON: Final = True
DEFAULT_BOOT_TIMEOUT: Final = 300
DEFAULT_FIRMWARE_RESTART: Final = True
DEFAULT_MAX_RESTARTS: Final = 3
DEFAULT_POLL_INTERVAL: Final = 3
DEFAULT_ERROR_GRACE: Final = 10
DEFAULT_RESTART_BACKOFF: Final = 20
DEFAULT_NOTIFY_ON_ERROR: Final = True

# OrcaSlicer's "Octo/Klipper" host type is pointed at http://<ha>:8123/api/orcastrator
URL_BASE: Final = "/api/orcastrator"

EVENT_JOB: Final = "orcastrator_job"

MAX_UPLOAD_BYTES: Final = 2 * 1024**3
