"""Config flow: one entry per printer."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IGNORE, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_NAME, CONF_URL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
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
    CONF_AUTO_POWER_OFF: DEFAULT_AUTO_POWER_OFF,
    CONF_IDLE_TIMEOUT: DEFAULT_IDLE_TIMEOUT,
    CONF_COOLDOWN_TEMP: DEFAULT_COOLDOWN_TEMP,
    CONF_GRACEFUL_SHUTDOWN: DEFAULT_GRACEFUL_SHUTDOWN,
}


def _endpoint_url(hass: HomeAssistant) -> str:
    try:
        base = get_url(hass, prefer_external=False)
    except NoURLAvailableError:
        base = "http://<home-assistant>:8123"
    return f"{base.rstrip('/')}{URL_BASE}"


def _own_switch_entities(hass: HomeAssistant) -> list[str]:
    ent_reg = er.async_get(hass)
    return [
        entry.entity_id
        for entry in ent_reg.entities.values()
        if entry.platform == DOMAIN and entry.domain == Platform.SWITCH
    ]


def _advanced_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    def d_bool(key: str) -> bool:
        val = defaults.get(key)
        return bool(_ADVANCED_DEFAULTS[key] if val is None else val)

    def d_num(key: str, low: int, high: int) -> int:
        val = defaults.get(key)
        num = round(float(_ADVANCED_DEFAULTS[key] if val is None else val))
        return min(high, max(low, num))

    return vol.Schema(
        {
            vol.Required(CONF_AUTO_POWER_ON, default=d_bool(CONF_AUTO_POWER_ON)): (
                BooleanSelector()
            ),
            vol.Required(CONF_BOOT_TIMEOUT, default=d_num(CONF_BOOT_TIMEOUT, 30, 1800)): (
                NumberSelector(
                    NumberSelectorConfig(
                        min=30,
                        max=1800,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            ),
            vol.Required(CONF_FIRMWARE_RESTART, default=d_bool(CONF_FIRMWARE_RESTART)): (
                BooleanSelector()
            ),
            vol.Required(CONF_MAX_RESTARTS, default=d_num(CONF_MAX_RESTARTS, 1, 10)): (
                NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                )
            ),
            vol.Required(CONF_POLL_INTERVAL, default=d_num(CONF_POLL_INTERVAL, 1, 30)): (
                NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=30,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            ),
            vol.Required(CONF_ERROR_GRACE, default=d_num(CONF_ERROR_GRACE, 0, 120)): (
                NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=120,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            ),
            vol.Required(CONF_RESTART_BACKOFF, default=d_num(CONF_RESTART_BACKOFF, 5, 120)): (
                NumberSelector(
                    NumberSelectorConfig(
                        min=5,
                        max=120,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            ),
            vol.Required(CONF_NOTIFY_ON_ERROR, default=d_bool(CONF_NOTIFY_ON_ERROR)): (
                BooleanSelector()
            ),
            vol.Required(CONF_AUTO_POWER_OFF, default=d_bool(CONF_AUTO_POWER_OFF)): (
                BooleanSelector()
            ),
            vol.Required(CONF_IDLE_TIMEOUT, default=d_num(CONF_IDLE_TIMEOUT, 5, 480)): (
                NumberSelector(
                    NumberSelectorConfig(
                        min=5,
                        max=480,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            ),
            vol.Required(CONF_COOLDOWN_TEMP, default=d_num(CONF_COOLDOWN_TEMP, 25, 120)): (
                NumberSelector(
                    NumberSelectorConfig(
                        min=25,
                        max=120,
                        step=1,
                        unit_of_measurement="°C",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            ),
            vol.Required(CONF_GRACEFUL_SHUTDOWN, default=d_bool(CONF_GRACEFUL_SHUTDOWN)): (
                BooleanSelector()
            ),
        }
    )


def _flatten_input(user_input: Mapping[str, Any]) -> dict[str, Any]:
    flat = {k: v for k, v in user_input.items() if k != SECTION_ADVANCED}
    adv = user_input.get(SECTION_ADVANCED)
    if isinstance(adv, Mapping):
        flat.update(adv)
    return flat


def _parse_url(raw_input: str) -> str | None:
    raw = raw_input.strip()
    if not raw or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    if "://" not in raw:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raw = f"http://{raw}"
        else:
            raw = f"http://[{raw}]" if ip.version == 6 else f"http://{raw}"
    try:
        url = URL(raw)
    except ValueError:
        return None
    if (
        url.scheme not in ("http", "https")
        or not url.host
        or any(ch.isspace() for ch in url.host)
        or (url.explicit_port is not None and not 1 <= url.explicit_port <= 65535)
        or url.query_string
        or url.fragment
        or url.user is not None
        or url.password is not None
    ):
        return None
    return str(url).rstrip("/")


def _claimed_slugs(hass: HomeAssistant, exclude_entry_id: str | None = None) -> set[str]:
    claimed: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id or entry.source == SOURCE_IGNORE:
            continue
        if entry.unique_id:
            claimed.add(entry.unique_id)
        settings = entry_settings(entry)
        if name_slug := slug(str(settings.get(CONF_NAME, ""))):
            claimed.add(name_slug)
        for alias in settings.get(CONF_ALIASES) or ():
            if isinstance(alias, str) and (alias_slug := slug(alias)):
                claimed.add(alias_slug)
    return claimed


def _normalise(
    hass: HomeAssistant,
    user_input: Mapping[str, Any],
    errors: dict[str, str],
    *,
    defaults: Mapping[str, Any] = _ADVANCED_DEFAULTS,
    exclude_entry_id: str | None = None,
) -> dict[str, Any]:
    data = _flatten_input(user_input)
    name = str(data.get(CONF_NAME, "")).strip()
    data[CONF_NAME] = name
    name_slug = slug(name)
    if not name_slug:
        errors[CONF_NAME] = "invalid_name"

    cleaned_url = _parse_url(str(data.get(CONF_URL, "")))
    if cleaned_url is None:
        errors[CONF_URL] = "invalid_url"
    else:
        data[CONF_URL] = cleaned_url

    aliases: list[str] = []
    seen_alias_slugs: set[str] = set()
    for raw_alias in data.get(CONF_ALIASES) or []:
        if not isinstance(raw_alias, str):
            continue
        stripped = raw_alias.strip()
        if not stripped:
            continue
        alias_slug = slug(stripped)
        if not alias_slug:
            errors[CONF_ALIASES] = "invalid_alias"
            break
        if alias_slug not in seen_alias_slugs:
            seen_alias_slugs.add(alias_slug)
            aliases.append(stripped)
    data[CONF_ALIASES] = aliases

    if not data.get(CONF_POWER_ENTITY):
        data.pop(CONF_POWER_ENTITY, None)

    api_key = str(data.get(CONF_API_KEY) or "").strip()
    if api_key and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in api_key):
        data[CONF_API_KEY] = api_key
    else:
        data.pop(CONF_API_KEY, None)

    for key in (
        CONF_BOOT_TIMEOUT,
        CONF_MAX_RESTARTS,
        CONF_POLL_INTERVAL,
        CONF_ERROR_GRACE,
        CONF_RESTART_BACKOFF,
        CONF_IDLE_TIMEOUT,
        CONF_COOLDOWN_TEMP,
    ):
        raw_num = data.get(key)
        fallback = defaults.get(key, _ADVANCED_DEFAULTS[key])
        data[key] = round(float(fallback if raw_num is None else raw_num))

    for key in (
        CONF_AUTO_POWER_ON,
        CONF_FIRMWARE_RESTART,
        CONF_NOTIFY_ON_ERROR,
        CONF_AUTO_POWER_OFF,
        CONF_GRACEFUL_SHUTDOWN,
    ):
        raw_bool = data.get(key)
        fallback = defaults.get(key, _ADVANCED_DEFAULTS[key])
        data[key] = bool(fallback if raw_bool is None else raw_bool)

    if data[CONF_ERROR_GRACE] >= data[CONF_BOOT_TIMEOUT]:
        errors[CONF_ERROR_GRACE] = "invalid_timing"
    if data[CONF_POLL_INTERVAL] >= data[CONF_BOOT_TIMEOUT]:
        errors[CONF_POLL_INTERVAL] = "invalid_timing"

    if not errors:
        claimed = _claimed_slugs(hass, exclude_entry_id=exclude_entry_id)
        if name_slug in claimed:
            errors[CONF_NAME] = "already_configured"
        elif any(alias_slug in claimed for alias_slug in seen_alias_slugs):
            errors[CONF_ALIASES] = "alias_conflict"

    return data


def _build_flow_schema(
    flow: ConfigFlow | OptionsFlow,
    hass: HomeAssistant,
    suggested: Mapping[str, Any],
    errors: Mapping[str, str],
) -> vol.Schema:
    adv_suggested = {
        key: suggested[key] if suggested.get(key) is not None else _ADVANCED_DEFAULTS[key]
        for key in _ADVANCED_DEFAULTS
    }
    adv_schema = flow.add_suggested_values_to_schema(
        _advanced_schema(adv_suggested),  # type: ignore[arg-type]
        adv_suggested,
    )
    has_adv_error = any(key in _ADVANCED_DEFAULTS for key in errors)
    excluded = _own_switch_entities(hass)
    entity_cfg = (
        EntitySelectorConfig(
            domain=["switch", "light", "input_boolean"], exclude_entities=excluded
        )
        if excluded
        else EntitySelectorConfig(domain=["switch", "light", "input_boolean"])
    )

    outer = vol.Schema(
        {
            vol.Required(CONF_NAME): TextSelector(),
            vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(CONF_POWER_ENTITY): EntitySelector(entity_cfg),
            vol.Optional(CONF_ALIASES): TextSelector(TextSelectorConfig(multiple=True)),
            vol.Optional(CONF_API_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(SECTION_ADVANCED): section(
                adv_schema,
                {"collapsed": not has_adv_error},
            ),
        }
    )
    top_suggested = {
        key: suggested[key]
        for key in (CONF_NAME, CONF_URL, CONF_POWER_ENTITY, CONF_ALIASES, CONF_API_KEY)
        if suggested.get(key) is not None
    }
    return flow.add_suggested_values_to_schema(
        outer,  # type: ignore[arg-type]
        top_suggested,
    )


class OrcastratorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a printer."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise(self.hass, user_input, errors)
            if not errors:
                await self.async_set_unique_id(slug(data[CONF_NAME]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=data[CONF_NAME], data=data)
            suggested = _flatten_input(user_input)
        else:
            suggested = {}

        schema = _build_flow_schema(self, self.hass, suggested, errors)
        return self.async_show_form(
            step_id="user",
            data_schema=schema,  # type: ignore[arg-type]
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
        current = entry_settings(self.config_entry)
        if user_input is not None:
            data = _normalise(
                self.hass,
                user_input,
                errors,
                defaults=current,
                exclude_entry_id=self.config_entry.entry_id,
            )
            if not errors:
                name = data.pop(CONF_NAME)
                new_uid = slug(name)
                data.setdefault(CONF_POWER_ENTITY, None)
                data.setdefault(CONF_API_KEY, None)

                new_data = dict(self.config_entry.data)
                data_changed = False
                if new_data.get(CONF_NAME) != name:
                    new_data[CONF_NAME] = name
                    data_changed = True
                if data[CONF_API_KEY] is None and CONF_API_KEY in new_data:
                    new_data.pop(CONF_API_KEY, None)
                    data_changed = True

                if (
                    data_changed
                    or name != self.config_entry.title
                    or new_uid != self.config_entry.unique_id
                ):
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        title=name,
                        unique_id=new_uid,
                        data=new_data,
                        options=data,
                    )
                return self.async_create_entry(data=data)
            suggested = {**current, **_flatten_input(user_input)}
        else:
            suggested = current

        schema = _build_flow_schema(self, self.hass, suggested, errors)
        return self.async_show_form(
            step_id="init",
            data_schema=schema,  # type: ignore[arg-type]
            errors=errors,
            description_placeholders={"url": _endpoint_url(self.hass)},
        )
