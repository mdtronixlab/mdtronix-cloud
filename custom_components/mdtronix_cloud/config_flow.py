"""Config flow: link this Home Assistant to a MDtronix account with a short code.

Setup opens the approval page in the user's browser on its own (Home Assistant's "external step"),
and a background task polls the relay for approval, the same way the integration's own tunnel client
would, but once per interval instead of once. Nothing in Home Assistant needs a click once the browser
tab is open: approving the code there is enough to finish adding the integration. The customer never
sees a password or a Google credential here. Protocol: docs/remote-access/PROTOCOL.md, section 1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
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

STEP_USER = "user"

# Errors from /v1/ha/device/token (PROTOCOL.md 1.3) that stop polling, mapped to an abort reason.
_ABORT_REASONS = {
    "instance_already_linked": "already_linked",
    "account_suspended": "account_suspended",
    "expired_token": "timeout",
}


class MDtronixCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Links this Home Assistant instance to an MDtronix account."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MDtronixCloudOptionsFlow:
        return MDtronixCloudOptionsFlow()

    def __init__(self) -> None:
        self._device_code: str = ""
        self._user_code: str = ""
        self._verification_uri: str = ""
        self._interval: int = 5
        self._expires_in: int = 900
        self._relink_entry_id: str | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._approved_body: dict[str, Any] | None = None
        self._error: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Starts the device login. The only form here is a retry button, shown if the relay can't be reached."""
        return await self._start_device_login()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The relay revoked this home's token, so link again."""
        self._relink_entry_id = self.context["entry_id"]
        return await self._start_device_login()

    async def _start_device_login(self) -> ConfigFlowResult:
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(f"{RELAY_URL}/v1/ha/device") as resp:
                if resp.status != 200:
                    return self._error_form("cannot_connect")
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError):
            return self._error_form("cannot_connect")

        self._device_code = data["device_code"]
        self._user_code = data["user_code"]
        self._verification_uri = data["verification_uri_complete"]
        self._interval = int(data.get("interval", 5))
        self._expires_in = int(data.get("expires_in", 900))
        self._approved_body = None
        self._error = None
        # eager_start=False: the task must not start until this step has returned and the flow manager has
        # recorded it, not run inline here (a real sleep() yields first either way, but this doesn't rely on it).
        self._poll_task = self.hass.async_create_task(
            self._poll_for_approval(), f"{DOMAIN}_device_login_{self.flow_id}", eager_start=False
        )
        return self.async_external_step(
            step_id="waiting",
            url=self._verification_uri,
            description_placeholders={
                "user_code": self._user_code,
                "verification_uri": self._verification_uri,
            },
        )

    async def _poll_for_approval(self) -> None:
        """Polls the relay in the background until the code is approved, fails, or expires.

        Nothing else advances the flow past its external step, so this drives it to completion itself
        once it has an answer (see _drive_to_completion). That is how setup finishes without the owner
        coming back to Home Assistant and clicking anything once they have approved the code.
        """
        session = async_get_clientsession(self.hass)
        deadline = self.hass.loop.time() + self._expires_in
        interval = self._interval
        while self.hass.loop.time() < deadline:
            await asyncio.sleep(interval)
            try:
                async with session.post(
                    f"{RELAY_URL}/v1/ha/device/token", json={"device_code": self._device_code}
                ) as resp:
                    body: dict[str, Any] = await resp.json(content_type=None)
                    status = resp.status
            except (aiohttp.ClientError, TimeoutError):
                continue  # One dropped request is worth retrying; the deadline above still applies.

            if status == 200:
                self._approved_body = body
                break
            error = body.get("error", "unknown")
            if error == "slow_down":
                interval += 5
                continue
            if error == "authorization_pending":
                continue
            # expired_token, invalid_grant, instance_already_linked, account_suspended: stop polling.
            self._error = error
            break
        else:
            self._error = "expired_token"

        await self._drive_to_completion()

    async def _drive_to_completion(self) -> None:
        """Advances the flow past its external step. The frontend reacts to the result; it doesn't
        call back in on its own, so this keeps calling configure until the flow reaches a real step."""
        try:
            result = await self.hass.config_entries.flow.async_configure(self.flow_id)
            while result["type"] is FlowResultType.EXTERNAL_STEP_DONE:
                result = await self.hass.config_entries.flow.async_configure(self.flow_id)
        except UnknownFlow:
            pass  # The flow was cancelled (the dialog was closed); nothing left to advance.

    async def async_step_waiting(self, user_input: Any = None) -> ConfigFlowResult:
        """Reached once polling has an answer. There is nothing to show here; it moves straight on."""
        if self._approved_body is not None:
            return self.async_external_step_done(next_step_id="finish")
        if self._error is not None:
            return self.async_external_step_done(next_step_id="failed")
        # Not resolved yet. Re-show the same external step rather than erroring on a stray call.
        return self.async_external_step(step_id="waiting", url=self._verification_uri)

    async def async_step_finish(self, user_input: Any = None) -> ConfigFlowResult:
        assert self._approved_body is not None
        return await self._finish(self._approved_body)

    async def async_step_failed(self, user_input: Any = None) -> ConfigFlowResult:
        return self.async_abort(reason=_ABORT_REASONS.get(self._error or "", "cannot_connect"))

    async def _finish(self, body: dict[str, Any]) -> ConfigFlowResult:
        data = {
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

    def _error_form(self, error: str) -> ConfigFlowResult:
        return self.async_show_form(step_id=STEP_USER, data_schema=None, errors={"base": error})

    @callback
    def async_remove(self) -> None:
        """The flow was cancelled or finished. Stop polling the relay for a code nobody is waiting on."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()


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
