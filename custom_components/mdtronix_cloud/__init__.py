"""MDtronix Cloud: remote access to this Home Assistant through the MDtronix relay.

Protocol: docs/remote-access/PROTOCOL.md. The tunnel client is in tunnel.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_SUBDOMAIN, CONF_TUNNEL_TOKEN, CONF_TUNNEL_URL, DEFAULT_LOCAL_PORT, DOMAIN
from .proxy_check import covers_loopback
from .tunnel import TunnelClient
from .voice import VoiceAssistant

_LOGGER = logging.getLogger(__name__)

INTEGRATION_VERSION = "0.1.0"
PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]

ISSUE_LOOPBACK_TRUSTED = "loopback_trusted_network"
ISSUE_PROXIES_MISSING = "trusted_proxies_missing"
ISSUE_INACTIVE = "remote_access_inactive"


def state_signal(entry_id: str) -> str:
    """Dispatcher signal that announces the tunnel's connected state for one entry."""
    return f"{DOMAIN}_state_{entry_id}"


def _local_port(hass: HomeAssistant) -> int:
    """HA's own HTTP port, so tunnelled requests reach the same server. Falls back to the default."""
    http = getattr(hass, "http", None)
    port = getattr(http, "server_port", None)
    return int(port) if port else DEFAULT_LOCAL_PORT


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the tunnel for this home. It runs for as long as the entry is loaded."""
    # Cookies from HA's own responses must not end up in a shared jar, so use a dummy one.
    session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    voice = VoiceAssistant(hass, entry, subdomain=entry.data[CONF_SUBDOMAIN])
    await voice.async_initialize()

    async def on_revoked() -> None:
        # The relay revoked this token (unlinked, or replaced from another HA). Ask the user to link again.
        entry.async_start_reauth(hass)

    async def on_inactive() -> None:
        # The relay refused the tunnel because the subscription isn't active. Tell the owner why it's offline.
        _set_issue(hass, ISSUE_INACTIVE, True, ir.IssueSeverity.WARNING)

    def on_state_change(connected: bool) -> None:
        if connected:
            _set_issue(hass, ISSUE_INACTIVE, False, ir.IssueSeverity.WARNING)
        async_dispatcher_send(hass, state_signal(entry.entry_id), connected)

    client = TunnelClient(
        tunnel_url=entry.data[CONF_TUNNEL_URL],
        token=entry.data[CONF_TUNNEL_TOKEN],
        local_base=f"http://127.0.0.1:{_local_port(hass)}",
        session=session,
        ha_version=HA_VERSION,
        client_version=INTEGRATION_VERSION,
        on_revoked=on_revoked,
        on_inactive=on_inactive,
        on_state_change=on_state_change,
        on_voice=voice.handle,
    )
    _async_check_proxy_setup(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    tunnel_task = entry.async_create_background_task(hass, client.run(), f"{DOMAIN}_tunnel_{entry.entry_id}")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = (client, session, voice, tunnel_task)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the tunnel and close its session."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    client, session, voice, tunnel_task = hass.data[DOMAIN].pop(entry.entry_id)
    # Home Assistant also cancels this task on unload, but not synchronously with this function, so a
    # connection attempt still in progress (e.g. a slow or blocked DNS/TCP handshake) could otherwise
    # outlive it. Cancelling and awaiting it here means nothing from this entry is left running once
    # this function returns, not even a connection attempt that client.close() can't interrupt because
    # it hasn't produced a socket yet.
    tunnel_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await tunnel_task
    await client.close()
    await session.close()
    voice.async_deinitialize()
    _set_issue(hass, ISSUE_INACTIVE, False, ir.IssueSeverity.WARNING)
    return True


def _set_issue(hass: HomeAssistant, issue_id: str, active: bool, severity: ir.IssueSeverity) -> None:
    if active:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=issue_id,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


def _async_check_proxy_setup(hass: HomeAssistant) -> None:
    """Raise repair issues for settings that would make remote access unsafe or misleading."""
    http = getattr(hass, "http", None)
    proxies = list(getattr(http, "trusted_proxies", None) or [])

    trusted_networks: list[object] = []
    for provider in hass.auth.auth_providers:
        if provider.type == "trusted_networks":
            trusted_networks.extend(provider.trusted_networks)

    _set_issue(
        hass,
        ISSUE_LOOPBACK_TRUSTED,
        covers_loopback(trusted_networks),
        ir.IssueSeverity.ERROR,
    )
    _set_issue(
        hass,
        ISSUE_PROXIES_MISSING,
        not covers_loopback(proxies),
        ir.IssueSeverity.WARNING,
    )
