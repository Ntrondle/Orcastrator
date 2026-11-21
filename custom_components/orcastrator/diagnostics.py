"""Diagnostics support for Orcastrator."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from . import OrcastratorConfigEntry

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OrcastratorConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a configured printer entry."""
    runtime = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "effective_config": async_redact_data(asdict(runtime.config), TO_REDACT),
        "runtime": {
            "state": runtime.state.value,
            "detail": runtime.detail,
            "last_error": runtime.last_error,
            "queued_jobs": runtime.queued,
            "filename": runtime.job.filename if runtime.job else None,
            "start_print": runtime.job.start_print if runtime.job else None,
            "updated": runtime.updated.isoformat(),
        },
    }
