"""Config flow for the IServ integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import IServApiClient, IServAuthError, IServConnectionError, normalize_host
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _unique_id(data: Mapping[str, Any]) -> str:
    """Return the unique id for a set of credentials."""
    return f"{data[CONF_HOST]}|{data[CONF_USERNAME]}".casefold()


async def _async_validate_input(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> dict[str, Any]:
    """Log in once to verify the credentials and normalise the entry data."""
    host = normalize_host(str(data[CONF_HOST]))
    client = IServApiClient(
        async_get_clientsession(hass),
        host,
        str(data[CONF_USERNAME]).strip(),
        str(data[CONF_PASSWORD]),
    )
    await client.async_login()
    return {
        CONF_HOST: host,
        CONF_USERNAME: str(data[CONF_USERNAME]).strip(),
        CONF_PASSWORD: str(data[CONF_PASSWORD]),
    }


class IServConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the IServ config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IServOptionsFlow:
        """Create the options flow."""
        return IServOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the IServ host and credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await _async_validate_input(self.hass, user_input)
            except ValueError:
                errors["base"] = "invalid_host"
            except IServAuthError:
                errors["base"] = "invalid_auth"
            except IServConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - never leak unexpected errors
                _LOGGER.exception("Unexpected error while configuring IServ")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(_unique_id(data))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"IServ ({data[CONF_USERNAME]})", data=data
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @callback
    def _async_get_entry(self) -> ConfigEntry | None:
        """Return the config entry this flow belongs to."""
        return self.hass.config_entries.async_get_entry(self.context["entry_id"])

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a re-authentication triggered by the coordinator."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the credentials again after IServ rejected the session."""
        errors: dict[str, str] = {}
        entry = self._async_get_entry()
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            data = {
                CONF_HOST: entry.data[CONF_HOST],
                CONF_USERNAME: user_input.get(
                    CONF_USERNAME, entry.data[CONF_USERNAME]
                ),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                new_data = await _async_validate_input(self.hass, data)
            except ValueError:
                errors["base"] = "invalid_host"
            except IServAuthError:
                errors["base"] = "invalid_auth"
            except IServConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error while re-authenticating IServ")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow changing the host or credentials of an existing entry."""
        errors: dict[str, str] = {}
        entry = self._async_get_entry()
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            try:
                data = await _async_validate_input(self.hass, user_input)
            except ValueError:
                errors["base"] = "invalid_host"
            except IServAuthError:
                errors["base"] = "invalid_auth"
            except IServConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error while reconfiguring IServ")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, data=data, title=f"IServ ({data[CONF_USERNAME]})"
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class IServOptionsFlow(OptionsFlow):
    """Handle the IServ options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the polling interval."""
        entry = self._async_get_entry()
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCAN_INTERVAL_MINUTES,
                            max=MAX_SCAN_INTERVAL_MINUTES,
                        ),
                    )
                }
            ),
        )

    @callback
    def _async_get_entry(self) -> ConfigEntry | None:
        """Return the config entry this options flow belongs to."""
        entry = getattr(self, "config_entry", None)
        if entry is None:
            entry = self.hass.config_entries.async_get_entry(self.handler)
        return entry