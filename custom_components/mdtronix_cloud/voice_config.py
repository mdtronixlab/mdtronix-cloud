"""Configuration for Home Assistant's own Google Assistant and Alexa handlers.

Home Assistant already maps every device type that Google and Alexa support: lights, blinds, locks,
thermostats, fans, media players, sensors and more. We reuse those handlers and supply only what they
need from us: which entities are exposed, the PIN for locks and alarms, and the account they answer for.
Both handlers are Apache-2.0 code in Home Assistant core, used here as libraries.

Read the owner's options on each call, so an options change applies without reloading the entry.
"""

from __future__ import annotations

import logging
import time

import aiohttp
from homeassistant.components.alexa.config import AbstractConfig as AlexaAbstractConfig
from homeassistant.components.alexa.errors import NoTokenAvailable
from homeassistant.components.google_assistant.const import DOMAIN_TO_GOOGLE_TYPES
from homeassistant.components.google_assistant.helpers import AbstractConfig as GoogleAbstractConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ALEXA_EVENT_GATEWAY,
    CONF_EXPOSED_ALEXA,
    CONF_EXPOSED_ENTITIES,
    CONF_EXPOSED_GOOGLE,
    CONF_TUNNEL_TOKEN,
    CONF_VOICE_PIN,
    RELAY_URL,
)

_LOGGER = logging.getLogger(__name__)

# Refetch a bit before the token's real expiry, so a borrowed token never goes stale mid-request.
TOKEN_EXPIRY_SAFETY_MARGIN_S = 60

# Domains that can never be exposed. Camera streaming needs a cloud service that we don't provide, and
# groups duplicate the entities inside them.
EXCLUDED_DOMAINS = frozenset({"camera", "group"})
# Lock and alarm commands are only accepted with the PIN. Google's handler enforces it, and we require it
# for all of them, so an unlock or disarm by voice never works without the owner's PIN.
PIN_DOMAINS = frozenset({"lock", "alarm_control_panel"})
VOICE_DOMAINS = tuple(sorted(d for d in DOMAIN_TO_GOOGLE_TYPES if d not in EXCLUDED_DOMAINS))
DEFAULT_LOCALE = "en-US"


def exposed_entity_ids(entry: ConfigEntry, platform: str) -> frozenset[str]:
    """The devices the owner exposed to one platform, minus any in a domain that can never be exposed.

    Homes saved before the per-platform choice have one list, which applies to both platforms.
    """
    key = CONF_EXPOSED_GOOGLE if platform == "google" else CONF_EXPOSED_ALEXA
    chosen = entry.options.get(key, entry.options.get(CONF_EXPOSED_ENTITIES, []))
    return frozenset(entity_id for entity_id in chosen if entity_id.partition(".")[0] not in EXCLUDED_DOMAINS)


def voice_pin(entry: ConfigEntry) -> str | None:
    return entry.options.get(CONF_VOICE_PIN) or None


class GoogleVoiceConfig(GoogleAbstractConfig):
    """What Google's smart home handler needs from this home."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, agent_user_id: str) -> None:
        super().__init__(hass)
        self._entry = entry
        self._agent_user_id = agent_user_id

    @property
    def enabled(self) -> bool:
        return True

    @property
    def entity_config(self) -> dict:
        return {}

    @property
    def secure_devices_pin(self) -> str | None:
        return voice_pin(self._entry)

    @property
    def should_report_state(self) -> bool:
        # Pushes state changes to Google's Home Graph as they happen, instead of only answering
        # Google's own requests. See async_report_state for how the token is fetched and the call made.
        return True

    def get_local_user_id(self, webhook_id: str) -> None:
        return None

    def get_local_webhook_id(self, agent_user_id: str) -> None:
        return None

    def get_agent_user_id_from_context(self, context) -> str:
        return self._agent_user_id

    def get_agent_user_id_from_webhook(self, webhook_id: str) -> None:
        return None

    def should_expose(self, entity_id: str) -> bool:
        return entity_id in exposed_entity_ids(self._entry, "google")

    def should_2fa(self, state: State) -> bool:
        return state.entity_id.partition(".")[0] in PIN_DOMAINS

    async def async_report_state(self, message, agent_user_id, event_id=None):
        """Reports state to Google's Home Graph through the relay, which makes the actual Home Graph
        call itself rather than handing this integration a bearer token to use directly.

        That matters because Home Graph trusts whatever agentUserId is in the request, and a home's
        agentUserId (its subdomain) isn't a secret — if this integration held the shared Home Graph
        token directly, any one compromised or malicious home could forge a state report for a
        *different* customer by just naming their agentUserId. Routing through the relay means
        agentUserId always comes from the relay's own authenticated lookup of this home's tunnel
        token, never from anything this integration sends.
        """
        session = async_get_clientsession(self.hass)
        headers = {"authorization": f"Bearer {self._entry.data[CONF_TUNNEL_TOKEN]}"}
        body = {"payload": message}
        if event_id:
            body["requestId"] = event_id
        try:
            async with session.post(f"{RELAY_URL}/v1/ha/google-report-state", json=body, headers=headers) as resp:
                if resp.status >= 400:
                    _LOGGER.warning("Home Graph rejected a state report: %s %s", resp.status, await resp.text())
                return resp.status
        except Exception:  # noqa: BLE001 - any failure here means "couldn't report this time," not a crash;
            # this fires on every entity state change for every Google-exposed entity, so it must never let
            # an unexpected response from the relay (or anything else) propagate as an uncaught exception.
            _LOGGER.warning("Could not reach the relay to report state", exc_info=True)
            return None

    async def async_connect_agent_user(self, agent_user_id: str) -> None:
        # Google only asks for the agent user once it has synced, and it is always this home.
        return None

    async def async_disconnect_agent_user(self, agent_user_id: str) -> None:
        return None

    def async_get_agent_users(self) -> list[str]:
        return [self._agent_user_id]


class AlexaVoiceConfig(AlexaAbstractConfig):
    """What Alexa's smart home handler needs from this home."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, user_identifier: str) -> None:
        super().__init__(hass)
        self._entry = entry
        self._user_identifier = user_identifier
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def should_report_state(self) -> bool:
        # Pushes ChangeReport events for exposed entities as they change, instead of only
        # answering Alexa's own requests. See async_get_access_token for how the token is fetched.
        return True

    @property
    def endpoint(self) -> str:
        return ALEXA_EVENT_GATEWAY

    @property
    def locale(self) -> str:
        return DEFAULT_LOCALE

    def user_identifier(self) -> str:
        return self._user_identifier

    def should_expose(self, entity_id: str) -> bool:
        return entity_id in exposed_entity_ids(self._entry, "alexa")

    async def async_get_access_token(self) -> str:
        """A token for calling Alexa's Event Gateway directly (ChangeReport), reused while fresh.

        The skill's "Send Alexa Events" LWA credentials live on the relay, not here, so this asks
        the relay for a token instead of holding the client secret in this public HACS package. The
        relay authenticates the request with the home's own tunnel token (the same one already used
        to attach the tunnel), so no new per-home secret is needed.
        """
        if self._token and self._token_expires_at - TOKEN_EXPIRY_SAFETY_MARGIN_S > time.time():
            return self._token

        session = async_get_clientsession(self.hass)
        headers = {"authorization": f"Bearer {self._entry.data[CONF_TUNNEL_TOKEN]}"}
        try:
            async with session.get(f"{RELAY_URL}/v1/ha/alexa-report-token", headers=headers) as resp:
                if resp.status != 200:
                    raise NoTokenAvailable
                data = await resp.json()
        except Exception as err:
            if isinstance(err, NoTokenAvailable):
                raise
            raise NoTokenAvailable from err

        self._token = data["access_token"]
        self._token_expires_at = time.time() + float(data.get("expires_in", 0))
        return self._token

    def async_invalidate_access_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0
