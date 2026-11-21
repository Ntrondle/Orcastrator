"""Diagnostics support for Orcastrator."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant

from . import OrcastratorConfigEntry
from .gcode import slug
from .moonraker import safe_url

TO_REDACT = {CONF_API_KEY}


def _redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    redacted = async_redact_data(data, TO_REDACT)
    if isinstance(redacted.get(CONF_URL), str):
        redacted[CONF_URL] = safe_url(redacted[CONF_URL])
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OrcastratorConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a configured printer entry."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return {
            "entry_data": _redact_mapping(dict(entry.data)),
            "entry_options": _redact_mapping(dict(entry.options)),
            "effective_config": None,
            "runtime": None,
        }

    cfg = runtime.config
    power_state = hass.states.get(cfg.power_entity) if cfg.power_entity else None
    return {
        "entry_data": _redact_mapping(dict(entry.data)),
        "entry_options": _redact_mapping(dict(entry.options)),
        "effective_config": _redact_mapping(asdict(cfg)),
        "match_names": list(cfg.match_names),
        "match_slugs": [s for name in cfg.match_names if (s := slug(name))],
        "power_entity_state": power_state.state if power_state else None,
        "runtime": {
            "state": runtime.state.value,
            "detail": runtime.detail,
            "last_error": runtime.last_error,
            "queued_jobs": runtime.queued,
            "filename": runtime.job.filename if runtime.job else runtime.last_filename,
            "start_print": (
                runtime.job.start_print if runtime.job else runtime.last_start_print
            ),
            "idle_since": runtime.idle_since.isoformat() if runtime.idle_since else None,
            "idle_power_off_at": (
                runtime.idle_power_off_at.isoformat() if runtime.idle_power_off_at else None
            ),
            "updated": runtime.updated.isoformat(),
        },
    }
