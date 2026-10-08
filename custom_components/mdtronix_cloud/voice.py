"""Voice requests from the relay: Google Home and Alexa.

The relay checks the account and passes each request here over the tunnel (PROTOCOL.md section 2.5).
Home Assistant's own Google and Alexa handlers answer them, configured by voice_config.py.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from homeassistant.components.alexa import smart_home as alexa_smart_home
from homeassistant.components.google_assistant import smart_home as google_smart_home
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant

from .voice_config import AlexaVoiceConfig, GoogleVoiceConfig, exposed_entity_ids, voice_pin

SOURCE = "mdtronix_cloud"
GOOGLE_EXECUTE = "action.devices.EXECUTE"
GOOGLE_QUERY = "action.devices.QUERY"
# Alexa's unlock has no PIN step, so it's refused. The lock still locks by voice.
ALEXA_UNLOCK = ("Alexa.LockController", "Unlock")
# Alexa asks for the alarm's PIN when disarming. Home Assistant passes that PIN to the alarm, which only
# checks it if the alarm has a code. We require the owner's voice PIN instead, so disarm can't skip it.
ALEXA_DISARM = ("Alexa.SecurityPanelController", "Disarm")


class VoiceAssistant:
    """Answers voice requests for one home. One instance lives as long as the config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, subdomain: str) -> None:
        self._hass = hass
        self._entry = entry
        self._subdomain = subdomain
        self._google = GoogleVoiceConfig(hass, entry, agent_user_id=subdomain)
        self._alexa = AlexaVoiceConfig(hass, entry, user_identifier=subdomain)

    async def async_initialize(self) -> None:
        await self._alexa.async_initialize()

    def async_deinitialize(self) -> None:
        self._alexa.async_deinitialize()

    async def handle(self, platform: str, body: str) -> tuple[int, str]:
        """Answers one voice request with an HTTP-style status and a JSON body."""
        try:
            request: Any = json.loads(body)
        except ValueError:
            return 400, json.dumps({"error": "invalid_request"})
        if not isinstance(request, dict):
            return 400, json.dumps({"error": "invalid_request"})

        if platform == "google":
            return 200, json.dumps(await self._google_reply(request))
        if platform == "alexa":
            return 200, json.dumps(await self._alexa_reply(request))
        return 400, json.dumps({"error": "unknown_platform"})

    async def _google_reply(self, request: dict[str, Any]) -> dict[str, Any]:
        # Home Assistant's handler doesn't check the exposure list for EXECUTE and QUERY, because Google
        # normally only sends IDs from SYNC. We check here, so a crafted request can't reach other entities.
        exposed = exposed_entity_ids(self._entry, "google")
        inputs = request.get("inputs") or [{}]
        intent = inputs[0].get("intent")
        payload = dict(inputs[0].get("payload") or {})
        refused_commands: list[dict[str, Any]] = []
        refused_devices: dict[str, dict[str, Any]] = {}

        if intent == GOOGLE_EXECUTE:
            commands = []
            for command in payload.get("commands") or []:
                allowed = []
                for device in command.get("devices") or []:
                    entity_id = str(device.get("id", ""))
                    if entity_id in exposed:
                        allowed.append(device)
                    else:
                        refused_commands.append({"ids": [entity_id], "status": "ERROR", "errorCode": "deviceNotFound"})
                if allowed:
                    commands.append({**command, "devices": allowed})
            payload["commands"] = commands
        elif intent == GOOGLE_QUERY:
            devices = []
            for device in payload.get("devices") or []:
                entity_id = str(device.get("id", ""))
                if entity_id in exposed:
                    devices.append(device)
                else:
                    refused_devices[entity_id] = {"online": False, "status": "ERROR", "errorCode": "deviceNotFound"}
            payload["devices"] = devices

        if intent in (GOOGLE_EXECUTE, GOOGLE_QUERY):
            request = {**request, "inputs": [{**inputs[0], "payload": payload}, *inputs[1:]]}

        reply = await google_smart_home.async_handle_message(
            self._hass, self._google, self._subdomain, None, request, SOURCE
        )
        # Google's handler returns nothing for some intents, such as DISCONNECT. Google still expects a body.
        reply = reply or {"requestId": request.get("requestId"), "payload": {}}
        body = reply.setdefault("payload", {})
        if refused_commands:
            body["commands"] = [*body.get("commands", []), *refused_commands]
        if refused_devices:
            body["devices"] = {**body.get("devices", {}), **refused_devices}
        return reply

    async def _alexa_reply(self, request: dict[str, Any]) -> dict[str, Any]:
        directive = request.get("directive") or {}
        header = directive.get("header") or {}
        kind = (header.get("namespace"), header.get("name"))
        if kind == ALEXA_UNLOCK:
            return _alexa_error(header, "Unlocking by voice is turned off. Use the MDtronix app or the lock itself.")
        if kind == ALEXA_DISARM and not _alexa_pin_matches(self._entry, directive):
            return _alexa_error(header, "Disarming by voice needs your PIN.")
        return await alexa_smart_home.async_handle_message(self._hass, self._alexa, request, Context())


def _alexa_pin_matches(entry: ConfigEntry, directive: dict[str, Any]) -> bool:
    """True when the disarm carries the owner's PIN. With no PIN set, disarm by voice is always refused."""
    expected = voice_pin(entry)
    authorization = (directive.get("payload") or {}).get("authorization") or {}
    return bool(expected) and authorization.get("type") == "FOUR_DIGIT_PIN" and authorization.get("value") == expected


def _alexa_error(request_header: dict[str, Any], message: str) -> dict[str, Any]:
    header: dict[str, Any] = {
        "namespace": "Alexa",
        "name": "ErrorResponse",
        "payloadVersion": "3",
        "messageId": str(uuid.uuid4()),
    }
    if token := request_header.get("correlationToken"):
        header["correlationToken"] = token
    return {"event": {"header": header, "payload": {"type": "INVALID_DIRECTIVE", "message": message}}}
