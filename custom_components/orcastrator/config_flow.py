"""Config flow: one entry per printer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

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
    DOMAIN,
    SECTION_ADVANCED,
    URL_BASE,
)
from .gcode import slug
from .printer import entry_settings

_ADVANCED_DEFAULTS: dict[str, Any] = {
    CONF_AUTO_POWER_ON: DEFAULT_AUTO_POWER_ON,
    CONF_BOOT_TIMEOUT: DEFAULT_BOOT_TIMEOUT,
    CONF_FIRMWARE_RESTART: DEFAULT_FIRMWARE_RESTART,
    CONF_MAX_RESTARTS: DEFAULT_MAX_RESTARTS,
    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
    CONF_ERROR_GRACE: DEFAULT_ERROR_GRACE,
    CONF_RESTART_BACKOFF: DEFAULT_RESTART_BACKOFF,
    CONF_NOTIFY_ON_ERROR: DEFAULT_NOTIFY_ON_ERROR,
}


def _endpoint_url(hass: HomeAssistant) -> str:
    try:
        base = get_url(hass, prefer_external=False)
    except NoURLAvailableError:
        base = "http://<home-assistant>:8123"
    return f"{base.rstrip('/')}{URL_BASE}"


def _form_schema() -> vol.Schema:
    advanced_schema = vol.Schema(
        {
            vol.Required(CONF_AUTO_POWER_ON, default=DEFAULT_AUTO_POWER_ON): BooleanSelector(),
            vol.Required(CONF_BOOT_TIMEOUT, default=DEFAULT_BOOT_TIMEOUT): NumberSelector(
                NumberSelectorConfig(
                    min=30, max=1800, step=10, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_FIRMWARE_RESTART, default=DEFAULT_FIRMWARE_RESTART
            ): BooleanSelector(),
            vol.Required(CONF_MAX_RESTARTS, default=DEFAULT_MAX_RESTARTS): NumberSelector(
                NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=30, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_ERROR_GRACE, default=DEFAULT_ERROR_GRACE): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=120, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_RESTART_BACKOFF, default=DEFAULT_RESTART_BACKOFF): NumberSelector(
                NumberSelectorConfig(
                    min=5, max=120, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_NOTIFY_ON_ERROR, default=DEFAULT_NOTIFY_ON_ERROR): BooleanSelector(),
        }
    )
    return vol.Schema(
        {
            vol.Required(CONF_NAME): TextSelector(),
            vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(CONF_POWER_ENTITY): EntitySelector(
                EntitySelectorConfig(domain=["switch", "light", "input_boolean"])
            ),
            vol.Optional(CONF_ALIASES): TextSelector(TextSelectorConfig(multiple=True)),
            vol.Optional(CONF_API_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(SECTION_ADVANCED): section(
                advanced_schema,  # type: ignore[arg-type]
                {"collapsed": True},
            ),
        }
    )


def _flatten_input(user_input: Mapping[str, Any]) -> dict[str, Any]:
    flat = {k: v for k, v in user_input.items() if k != SECTION_ADVANCED}
    adv = user_input.get(SECTION_ADVANCED)
    if isinstance(adv, Mapping):
        flat.update(adv)
    return flat


def _to_suggested_values(flat: Mapping[str, Any]) -> dict[str, Any]:
    suggested: dict[str, Any] = {}
    for key in (CONF_NAME, CONF_URL, CONF_POWER_ENTITY, CONF_ALIASES, CONF_API_KEY):
        val = flat.get(key)
        if val is not None:
            suggested[key] = val
    suggested[SECTION_ADVANCED] = {
        key: flat[key] if flat.get(key) is not None else default
        for key, default in _ADVANCED_DEFAULTS.items()
    }
    return suggested


def _normalise(user_input: Mapping[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    data = _flatten_input(user_input)
    name = str(data.get(CONF_NAME, "")).strip()
    data[CONF_NAME] = name
    if not slug(name):
        errors[CONF_NAME] = "invalid_name"

    raw = str(data.get(CONF_URL, "")).strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        url = URL(raw)
    except ValueError:
        url = None
    if url is None or url.scheme not in ("http", "https") or not url.host:
        errors[CONF_URL] = "invalid_url"
    else:
        data[CONF_URL] = str(url).rstrip("/")

    data[CONF_ALIASES] = [a.strip() for a in (data.get(CONF_ALIASES) or []) if a.strip()]
    if not data.get(CONF_POWER_ENTITY):
        data.pop(CONF_POWER_ENTITY, None)
    if not data.get(CONF_API_KEY):
        data.pop(CONF_API_KEY, None)

    for key in (
        CONF_BOOT_TIMEOUT,
        CONF_MAX_RESTARTS,
        CONF_POLL_INTERVAL,
        CONF_ERROR_GRACE,
        CONF_RESTART_BACKOFF,
    ):
        raw_num = data.get(key)
        data[key] = int(_ADVANCED_DEFAULTS[key] if raw_num is None else raw_num)

    for key in (CONF_AUTO_POWER_ON, CONF_FIRMWARE_RESTART, CONF_NOTIFY_ON_ERROR):
        raw_bool = data.get(key)
        data[key] = bool(_ADVANCED_DEFAULTS[key] if raw_bool is None else raw_bool)

    return data


class OrcastratorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a printer."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise(user_input, errors)
            if not errors:
                await self.async_set_unique_id(slug(data[CONF_NAME]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=data[CONF_NAME], data=data)
            suggested = _flatten_input(user_input)
        else:
            suggested = {}

        schema = self.add_suggested_values_to_schema(
            _form_schema(),  # type: ignore[arg-type]
            _to_suggested_values(suggested),
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"url": _endpoint_url(self.hass)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OrcastratorOptionsFlow:
        return OrcastratorOptionsFlow()


class OrcastratorOptionsFlow(OptionsFlow):
    """Edit a printer, including renaming it or tuning advanced timings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise(user_input, errors)
            if not errors:
                new_uid = slug(data[CONF_NAME])
                if new_uid != self.config_entry.unique_id and any(
                    entry.entry_id != self.config_entry.entry_id and entry.unique_id == new_uid
                    for entry in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    errors[CONF_NAME] = "already_configured"
            if not errors:
                name = data.pop(CONF_NAME)
                new_uid = slug(name)
                if (
                    name != self.config_entry.data.get(CONF_NAME)
                    or new_uid != self.config_entry.unique_id
                ):
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        title=name,
                        unique_id=new_uid,
                        data={**self.config_entry.data, CONF_NAME: name},
                    )
                return self.async_create_entry(data=data)
            suggested = _flatten_input(user_input)
        else:
            suggested = entry_settings(self.config_entry)

        schema = self.add_suggested_values_to_schema(
            _form_schema(),  # type: ignore[arg-type]
            _to_suggested_values(suggested),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"url": _endpoint_url(self.hass)},
        )
