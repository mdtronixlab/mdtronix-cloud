"""Config flow: link this Home Assistant to a MDtronix account with its email and password.

Setup and reauth both show one form and POST straight to the relay's POST /v1/ha/login, which
answers with the tunnel token in a single round trip. There's no browser step and no code to
copy: the customer creates their MDtronix Cloud account and waits for staff approval on the
website first, then signs in here with the same email and password. Home Assistant never stores
the password, only the email (for display and to pre-fill reauth) and the tunnel token.
Protocol: docs/remote-access/PROTOCOL.md, section 1.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_EXPOSED_ALEXA,
    CONF_EXPOSED_GOOGLE,
    CONF_REMOTE_URL,
    CONF_SUBDOMAIN,
    CONF_TUNNEL_TOKEN,
    CONF_TUNNEL_URL,
    CONF_VOICE_PIN,
    DOMAIN,
    RELAY_URL,
)
from .voice_config import VOICE_DOMAINS, exposed_entity_ids

_LOGGER = logging.getLogger(__name__)

CONF_REPLACE = "replace"

# Errors from POST /v1/ha/login (PROTOCOL.md 1.1) that stop the flow outright, because nothing on
# this form can fix them — the customer has to act outside Home Assistant and come back.
_ABORT_REASONS = {
    "not_approved": "not_approved",
    "account_suspended": "account_suspended",
}


class MDtronixCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Links this Home Assistant instance to an MDtronix account."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MDtronixCloudOptionsFlow:
        return MDtronixCloudOptionsFlow()

    def __init__(self) -> None:
        self._relink_entry_id: str | None = None
        self._offer_replace = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._login_step(user_input, step_id="user")

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The relay revoked this home's token, so sign in again and replace it."""
        self._relink_entry_id = self.context["entry_id"]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._relink_entry_id) if self._relink_entry_id else None
        prefill = {CONF_EMAIL: entry.data.get(CONF_EMAIL, "")} if entry else None
        return await self._login_step(user_input, step_id="reauth_confirm", prefill=prefill)

    async def _login_step(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
        prefill: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Shared by setup and reauth: one email+password form, one POST to the relay."""
        is_reauth = self._relink_entry_id is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            replace = is_reauth or bool(user_input.get(CONF_REPLACE))
            try:
                session = async_get_clientsession(self.hass)
                async with session.post(
                    f"{RELAY_URL}/v1/ha/login",
                    json={"email": user_input[CONF_EMAIL], "password": user_input[CONF_PASSWORD], "replace": replace},
                ) as resp:
                    status = resp.status
                    body: dict[str, Any] = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                if status == 200:
                    return await self._finish(user_input[CONF_EMAIL], body)
                error = body.get("error", "unknown")
                if error in _ABORT_REASONS:
                    return self.async_abort(reason=_ABORT_REASONS[error])
                if error == "invalid_credentials":
                    errors["base"] = "invalid_credentials"
                elif error == "instance_already_linked":
                    errors["base"] = "instance_already_linked"
                    self._offer_replace = True
                else:
                    errors["base"] = "cannot_connect"
            prefill = user_input

        schema: dict[Any, Any] = {
            vol.Required(CONF_EMAIL, default=(prefill or {}).get(CONF_EMAIL, "")): str,
            vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
        # Only offered after a conflict, and never on reauth: reauth already implies replacing this
        # home's own token, with nothing for the owner to choose.
        if self._offer_replace and not is_reauth:
            schema[vol.Optional(CONF_REPLACE, default=False)] = bool

        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(schema), errors=errors)

    async def _finish(self, email: str, body: dict[str, Any]) -> ConfigFlowResult:
        data = {
            CONF_EMAIL: email,
            CONF_TUNNEL_TOKEN: body["tunnel_token"],
            CONF_TUNNEL_URL: body["tunnel_url"],
            CONF_SUBDOMAIN: body["subdomain"],
            CONF_REMOTE_URL: body["remote_url"],
        }

        if self._relink_entry_id is not None:
            entry = self.hass.config_entries.async_get_entry(self._relink_entry_id)
            if entry is None:
                return self.async_abort(reason="reauth_failed")
            self.hass.config_entries.async_update_entry(entry, data=data)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(body["subdomain"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=body["remote_url"], data=data)


class MDtronixCloudOptionsFlow(OptionsFlow):
    """Chooses which devices Google Home and Alexa can control, and the PIN for locks and alarms."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        if user_input is not None:
            pin = user_input.get(CONF_VOICE_PIN)
            if pin is None:
                # The field was left out of the form, so keep the PIN that is already set.
                pin = entry.options.get(CONF_VOICE_PIN, "")
            pin = pin.strip()
            if pin and not (pin.isdigit() and 4 <= len(pin) <= 8):
                errors[CONF_VOICE_PIN] = "invalid_pin"
            else:
                return self.async_create_entry(
                    data={
                        CONF_EXPOSED_GOOGLE: list(user_input.get(CONF_EXPOSED_GOOGLE) or []),
                        CONF_EXPOSED_ALEXA: list(user_input.get(CONF_EXPOSED_ALEXA) or []),
                        CONF_VOICE_PIN: pin,
                    }
                )

        devices = EntitySelector(EntitySelectorConfig(domain=list(VOICE_DOMAINS), multiple=True))
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_EXPOSED_GOOGLE,
                    default=sorted(exposed_entity_ids(entry, "google")),
                ): devices,
                vol.Optional(
                    CONF_EXPOSED_ALEXA,
                    default=sorted(exposed_entity_ids(entry, "alexa")),
                ): devices,
                vol.Optional(CONF_VOICE_PIN): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
