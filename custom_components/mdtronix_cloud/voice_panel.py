"""The MDtronix Voice page: one switch per device for Google Home and Alexa.

Home Assistant's own Voice assistants page can't offer this. Its frontend lists Google and Alexa only when
`cloud/status` reports a signed-in Nabu Casa account, so we register our own page through the custom panel API
instead. It edits the same option lists the voice handlers read, so a change applies straight away.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .voice_config import VOICE_DOMAINS, exposed_entity_ids, options_with_exposure, voice_pin

PANEL_URL_PATH = "mdtronix-voice"
PANEL_COMPONENT = "mdtronix-voice-panel"
PANEL_TITLE = "MDtronix Voice"
PANEL_ICON = "mdi:account-voice"
PANEL_FILE = "voice-panel.js"
FRONTEND_DIR = Path(__file__).parent / "frontend"
STATIC_URL = f"/{DOMAIN}_static"

PLATFORMS = ("google", "alexa")
ERROR_NOT_LOADED = "not_loaded"
# The static path and the WebSocket commands last until Home Assistant stops, unlike the panel, which goes with the entry.
_REGISTERED = f"{DOMAIN}_voice_panel_registered"


def _running_voice(hass: HomeAssistant) -> tuple[ConfigEntry, Any] | None:
    """The config entry and its voice assistant, while the integration is loaded."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if running := hass.data.get(DOMAIN, {}).get(entry.entry_id):
            _client, _session, voice, _tunnel_task = running
            return entry, voice
    return None


def _area_name(
    registered: er.RegistryEntry | None,
    devices: dr.DeviceRegistry,
    areas: ar.AreaRegistry,
) -> str | None:
    """The entity's own area, or else its device's."""
    if registered is None:
        return None
    area_id = registered.area_id
    if not area_id and registered.device_id and (device := devices.async_get(registered.device_id)):
        area_id = device.area_id
    area = areas.async_get_area(area_id) if area_id else None
    return area.name if area else None


def device_rows(hass: HomeAssistant, entry: ConfigEntry, supported: dict[str, set[str]]) -> list[dict[str, Any]]:
    """One row per device the owner can choose, plus any already chosen so they can always be turned off."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    exposed = {platform: exposed_entity_ids(entry, platform) for platform in PLATFORMS}
    already_chosen = exposed["google"] | exposed["alexa"]

    rows = []
    for entity_id in sorted(already_chosen | supported["google"] | supported["alexa"]):
        registered = entities.async_get(entity_id)
        offered = entity_id.partition(".")[0] in VOICE_DOMAINS and not (
            registered and (registered.entity_category or registered.hidden_by)
        )
        if not offered and entity_id not in already_chosen:
            continue
        state = hass.states.get(entity_id)
        rows.append(
            {
                "entity_id": entity_id,
                "name": state.name if state else entity_id,
                "area": _area_name(registered, devices, areas),
                # None marks the default name in the alias list. Without a registry entry there's nowhere to keep aliases.
                "aliases": None if registered is None else [a if isinstance(a, str) else None for a in registered.aliases],
                "google": entity_id in exposed["google"],
                "alexa": entity_id in exposed["alexa"],
                "google_supported": entity_id in supported["google"],
                "alexa_supported": entity_id in supported["alexa"],
            }
        )
    return rows


@callback
@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/voice/devices"})
def ws_devices(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Lists the devices the owner can choose for Google Home and Alexa."""
    running = _running_voice(hass)
    if running is None:
        connection.send_error(msg["id"], ERROR_NOT_LOADED, "MDtronix Cloud is not running")
        return
    entry, voice = running
    connection.send_result(
        msg["id"],
        {
            "devices": device_rows(hass, entry, voice.supported_entity_ids()),
            "pin_set": voice_pin(entry) is not None,
            # Before Home Assistant had an alias slot for the default name, a list of plain strings was all it took.
            "default_alias": hasattr(er, "COMPUTED_NAME"),
        },
    )


@callback
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/voice/expose",
        vol.Required("platform"): vol.In(PLATFORMS),
        vol.Required("entity_ids"): [cv.entity_id],
        vol.Required("expose"): bool,
    }
)
def ws_expose(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Adds devices to one platform's list, or removes them."""
    running = _running_voice(hass)
    if running is None:
        connection.send_error(msg["id"], ERROR_NOT_LOADED, "MDtronix Cloud is not running")
        return
    entry, _voice = running
    hass.config_entries.async_update_entry(
        entry, options=options_with_exposure(entry, msg["platform"], msg["entity_ids"], msg["expose"])
    )
    connection.send_result(msg["id"])


async def async_register_voice_panel(hass: HomeAssistant) -> None:
    """Adds the page to the sidebar for admins. Called last in entry setup, so it's there only when the entry is."""
    if _REGISTERED not in hass.data:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), cache_headers=False)]
        )
        websocket_api.async_register_command(hass, ws_devices)
        websocket_api.async_register_command(hass, ws_expose)
        hass.data[_REGISTERED] = True

    # In the URL so a browser picks up a new version of the page after an update.
    version = (await hass.async_add_executor_job((FRONTEND_DIR / PANEL_FILE).stat)).st_mtime_ns
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/{PANEL_FILE}?v={version}",
        require_admin=True,
    )


@callback
def async_remove_voice_panel(hass: HomeAssistant) -> None:
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
