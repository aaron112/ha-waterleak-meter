"""Config flow for the Water Leak Detection for Meters integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_LIMIT_MIN,
    CONF_NOTIFY_SERVICE,
    CONF_PULSE_FT3,
    CONF_QUIET_MIN,
    CONF_WATER_METER,
    DEFAULT_LIMIT_MIN,
    DEFAULT_NOTIFY_SERVICE,
    DEFAULT_PULSE_FT3,
    DEFAULT_QUIET_MIN,
    DOMAIN,
    LIMIT_MIN_MAX,
    LIMIT_MIN_MIN,
    PULSE_FT3_MAX,
    PULSE_FT3_MIN,
    QUIET_MIN_MAX,
    QUIET_MIN_MIN,
)


def _number_selector(
    min_value: float, max_value: float, unit: str, step: float = 1.0
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=min_value,
            max=max_value,
            step=step,
            mode="box",
            unit_of_measurement=unit,
        )
    )


def _notify_services(hass: HomeAssistant) -> list[str]:
    """Return the instance's notify services as 'notify.<name>' labels."""
    services = hass.services.async_services().get("notify", {})
    return sorted(f"notify.{name}" for name in services)


def _notify_default(hass: HomeAssistant, current: str | None = None) -> str:
    """Pick a default for the notification field.

    `None` means a fresh setup, so prefer a real service. An explicit value
    (including "") is preserved so the options flow never silently re-enables
    notifications the user turned off.
    """
    if current is not None:
        return current
    options = _notify_services(hass)
    if DEFAULT_NOTIFY_SERVICE in options:
        return DEFAULT_NOTIFY_SERVICE
    return options[0] if options else ""


def _notify_selector(hass: HomeAssistant) -> selector.SelectSelector:
    """Dropdown of the instance's notify services, editable + clearable."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_notify_services(hass),
            mode="dropdown",
            custom_value=True,
        )
    )


def _user_schema(hass: HomeAssistant) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_WATER_METER): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(CONF_QUIET_MIN, default=DEFAULT_QUIET_MIN): _number_selector(
                QUIET_MIN_MIN, QUIET_MIN_MAX, "min"
            ),
            vol.Required(CONF_LIMIT_MIN, default=DEFAULT_LIMIT_MIN): _number_selector(
                LIMIT_MIN_MIN, LIMIT_MIN_MAX, "min"
            ),
            vol.Required(
                CONF_PULSE_FT3, default=DEFAULT_PULSE_FT3
            ): _number_selector(PULSE_FT3_MIN, PULSE_FT3_MAX, "ft³", step=0.1),
            vol.Optional(
                CONF_NOTIFY_SERVICE, default=_notify_default(hass)
            ): _notify_selector(hass),
        }
    )


async def _validate(hass: HomeAssistant, user_input: dict[str, Any]) -> str | None:
    """Return an error key, or None if the input is valid."""
    notify = (user_input.get(CONF_NOTIFY_SERVICE) or "").strip()
    if not notify:
        return None
    if not notify.startswith("notify."):
        return "invalid_notify"
    service = notify.split(".", 1)[1]
    if not hass.services.has_service("notify", service):
        return "invalid_notify"
    return None


class WaterLeakConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Water Leak Detection for Meters."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            for entry in self._async_current_entries():
                if entry.options.get(CONF_WATER_METER) == user_input[CONF_WATER_METER]:
                    return self.async_abort(reason="already_configured")
            if error := await _validate(self.hass, user_input):
                errors[CONF_NOTIFY_SERVICE] = error
            else:
                return self.async_create_entry(
                    title="Water Leak Detection for Meters",
                    data={},
                    options=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(self.hass),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return WaterLeakOptionsFlowHandler(config_entry)


class WaterLeakOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow allowing edits from the UI."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if error := await _validate(self.hass, user_input):
                return self.async_show_form(
                    step_id="init",
                    data_schema=_option_schema(self.hass, self._entry.options),
                    errors={CONF_NOTIFY_SERVICE: error},
                )
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init", data_schema=_option_schema(self.hass, self._entry.options)
        )


def _option_schema(hass: HomeAssistant, options: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_WATER_METER, default=options.get(CONF_WATER_METER)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_QUIET_MIN, default=int(options.get(CONF_QUIET_MIN, DEFAULT_QUIET_MIN))
            ): _number_selector(QUIET_MIN_MIN, QUIET_MIN_MAX, "min"),
            vol.Required(
                CONF_LIMIT_MIN, default=int(options.get(CONF_LIMIT_MIN, DEFAULT_LIMIT_MIN))
            ): _number_selector(LIMIT_MIN_MIN, LIMIT_MIN_MAX, "min"),
            vol.Required(
                CONF_PULSE_FT3, default=float(options.get(CONF_PULSE_FT3, DEFAULT_PULSE_FT3))
            ): _number_selector(PULSE_FT3_MIN, PULSE_FT3_MAX, "ft³", step=0.1),
            vol.Optional(
                CONF_NOTIFY_SERVICE,
                default=_notify_default(
                    hass, (options.get(CONF_NOTIFY_SERVICE) or "").strip()
                ),
            ): _notify_selector(hass),
        }
    )