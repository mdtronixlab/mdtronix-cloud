"""Config flow: link this Home Assistant to a MDtronix account with a short code.

The customer approves the code at RELAY_URL/link in a browser. HA never sees a password or a
Google credential. Protocol: docs/remote-access/PROTOCOL.md, section 1.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_REMOTE_URL, CONF_SUBDOMAIN, CONF_TUNNEL_TOKEN, CONF_TUNNEL_URL, DOMAIN, RELAY_URL

_LOGGER = logging.getLogger(__name__)

STEP_USER = "user"


class MDtronixCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Links this Home Assistant instance to an MDtronix account."""

    VERSION = 1

    def __init__(self) -> None:
        self._device_code: str = ""
        self._user_code: str = ""
        self._verification_uri: str = ""
        self._interval: int = 5
        self._relink_entry_id: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the code first. When the customer submits, check whether it has been approved."""
        if user_input is None:
            return await self._start_device_login()
        return await self._check_approval()

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
        return self._show_code_form()

    async def _check_approval(self) -> ConfigFlowResult:
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{RELAY_URL}/v1/ha/device/token", json={"device_code": self._device_code}
            ) as resp:
                body: dict[str, Any] = await resp.json(content_type=None)
                status = resp.status
        except (aiohttp.ClientError, TimeoutError):
            return self._show_code_form(error="cannot_connect")

        if status == 200:
            return await self._finish(body)

        error = body.get("error", "unknown")
        if error == "authorization_pending":
            return self._show_code_form(error="pending")
        if error == "slow_down":
            self._interval += 5
            return self._show_code_form(error="pending")
        if error in ("expired_token", "invalid_grant"):
            # The code expired or was used. Start again with a fresh one.
            return await self._start_device_login()
        if error == "instance_already_linked":
            return self.async_abort(reason="already_linked")
        if error == "account_suspended":
            return self.async_abort(reason="account_suspended")
        _LOGGER.debug("device login failed with %s", error)
        return self._show_code_form(error="cannot_connect")

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

    def _show_code_form(self, error: str | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=None,
            description_placeholders={
                "user_code": self._user_code,
                "verification_uri": self._verification_uri,
            },
            errors={"base": error} if error else None,
        )

    def _error_form(self, error: str) -> ConfigFlowResult:
        return self.async_show_form(step_id=STEP_USER, data_schema=None, errors={"base": error})
