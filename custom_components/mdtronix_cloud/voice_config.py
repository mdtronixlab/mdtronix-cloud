"""Configuration for Home Assistant's own Google Assistant and Alexa handlers.

Home Assistant already maps every device type that Google and Alexa support: lights, blinds, locks,
thermostats, fans, media players, sensors and more. We reuse those handlers and supply only what they
need from us: which entities are exposed, the PIN for locks and alarms, and the account they answer for.
Both handlers are Apache-2.0 code in Home Assistant core, used here as libraries.

Read the owner's options on each call, so an options change applies without reloading the entry.
"""

from __future__ import annotations

from homeassistant.components.alexa.config import AbstractConfig as AlexaAbstractConfig
from homeassistant.components.google_assistant.const import DOMAIN_TO_GOOGLE_TYPES
from homeassistant.components.google_assistant.helpers import AbstractConfig as GoogleAbstractConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State

from .const import CONF_EXPOSED_ALEXA, CONF_EXPOSED_ENTITIES, CONF_EXPOSED_GOOGLE, CONF_VOICE_PIN

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
        # Google is answered on request. Proactive reports need Google's HomeGraph, which isn't set up yet.
        return False

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

    @property
    def endpoint(self) -> None:
        # Proactive state reports aren't sent yet, so there is no event gateway endpoint.
        return None

    @property
    def locale(self) -> str:
        return DEFAULT_LOCALE

    def user_identifier(self) -> str:
        return self._user_identifier

    def should_expose(self, entity_id: str) -> bool:
        return entity_id in exposed_entity_ids(self._entry, "alexa")

    async def async_get_access_token(self) -> None:
        # Requests arrive through the relay, which has already checked the account.
        return None

    def async_invalidate_access_token(self) -> None:
        return None
