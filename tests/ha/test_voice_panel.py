"""The MDtronix Voice page's backend, against a real Home Assistant.

Needs pytest-homeassistant-custom-component, so it lives apart from the plain unittest files in tests/.
Run from the repo root, in the test venv:  python -m pytest tests/ha
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.mdtronix_cloud.const import (
    CONF_EXPOSED_ALEXA,
    CONF_EXPOSED_ENTITIES,
    CONF_EXPOSED_GOOGLE,
    CONF_REMOTE_URL,
    CONF_SUBDOMAIN,
    CONF_TUNNEL_TOKEN,
    CONF_TUNNEL_URL,
    CONF_VOICE_PIN,
    DOMAIN,
)
from custom_components.mdtronix_cloud.voice_config import exposed_entity_ids
from custom_components.mdtronix_cloud.voice_panel import (
    PANEL_FILE,
    PANEL_URL_PATH,
    STATIC_URL,
    device_rows,
)
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

DATA = {
    CONF_TUNNEL_TOKEN: "token",
    CONF_TUNNEL_URL: "wss://relay.invalid/v1/tunnel",
    CONF_SUBDOMAIN: "home",
    CONF_REMOTE_URL: "https://home.invalid",
}
CAMERA_STREAM_FEATURE = 2


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    return


@pytest.fixture
def google_sync():
    """Stands in for the request that tells Google the device list changed."""
    with patch(
        "custom_components.mdtronix_cloud.voice_config.GoogleVoiceConfig.async_schedule_google_sync_all"
    ) as sync:
        yield sync


async def _set_up(hass, options=None) -> MockConfigEntry:
    """Loads the integration with no tunnel and no calls to Amazon."""
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=options or {})
    entry.add_to_hass(hass)
    tunnel = MagicMock(run=AsyncMock(), close=AsyncMock())
    with (
        patch("custom_components.mdtronix_cloud.TunnelClient", return_value=tunnel),
        patch("custom_components.mdtronix_cloud.voice_config.AlexaVoiceConfig.set_authorized", AsyncMock()),
        patch(
            "custom_components.mdtronix_cloud.voice_config.AlexaVoiceConfig.async_disable_proactive_mode",
            AsyncMock(),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _add_devices(hass) -> None:
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})
    hass.states.async_set("switch.pump", "off", {"friendly_name": "Pump"})
    # A plain number sensor: neither platform has anything to do with it.
    hass.states.async_set("sensor.power", "5", {"friendly_name": "Power"})
    # Both can be controlled by the platforms, but we never offer them.
    hass.states.async_set("camera.door", "idle", {"supported_features": CAMERA_STREAM_FEATURE})
    hass.states.async_set("group.everything", "on", {"entity_id": ["light.kitchen"]})


async def _call(client, **message):
    await client.send_json_auto_id(message)
    return await client.receive_json()


async def _devices(client) -> dict:
    response = await _call(client, type=f"{DOMAIN}/voice/devices")
    assert response["success"], response
    return {device["entity_id"]: device for device in response["result"]["devices"]}


async def test_lists_only_what_the_owner_can_choose(hass, hass_ws_client, entity_registry, area_registry, google_sync):
    _add_devices(hass)
    settings = entity_registry.async_get_or_create(
        "switch", "test", "identify", suggested_object_id="identify", entity_category=EntityCategory.CONFIG
    )
    hass.states.async_set(settings.entity_id, "off")
    await _set_up(hass)

    devices = await _devices(await hass_ws_client(hass))

    assert set(devices) == {"light.kitchen", "switch.pump"}
    assert devices["light.kitchen"]["name"] == "Kitchen"
    assert devices["light.kitchen"]["google_supported"] and devices["light.kitchen"]["alexa_supported"]
    assert not devices["light.kitchen"]["google"] and not devices["light.kitchen"]["alexa"]


async def test_shows_area_and_keeps_the_default_name_slot_in_aliases(hass, hass_ws_client, entity_registry, google_sync):
    registered = entity_registry.async_get_or_create("light", "test", "lamp", suggested_object_id="lamp")
    area = ar.async_get(hass).async_create("Bedroom 2")
    entity_registry.async_update_entity(
        registered.entity_id, area_id=area.id, aliases=[er.COMPUTED_NAME, "Big light"]
    )
    hass.states.async_set(registered.entity_id, "on", {"friendly_name": "Lamp"})
    await _set_up(hass)

    device = (await _devices(await hass_ws_client(hass)))[registered.entity_id]

    assert device["area"] == "Bedroom 2"
    assert device["aliases"] == [None, "Big light"]


async def test_a_device_without_a_registry_entry_cannot_have_aliases(hass, hass_ws_client, google_sync):
    _add_devices(hass)
    await _set_up(hass)

    assert (await _devices(await hass_ws_client(hass)))["light.kitchen"]["aliases"] is None


async def test_a_chosen_device_stays_listed_after_it_goes_away_so_it_can_be_turned_off(
    hass, hass_ws_client, google_sync
):
    await _set_up(hass, {CONF_EXPOSED_GOOGLE: ["light.removed"]})

    device = (await _devices(await hass_ws_client(hass)))["light.removed"]

    assert device["google"] is True
    assert device["google_supported"] is False
    assert device["name"] == "light.removed"


async def test_a_settings_entity_chosen_earlier_stays_listed(hass, hass_ws_client, entity_registry, google_sync):
    settings = entity_registry.async_get_or_create(
        "switch", "test", "identify", suggested_object_id="identify", entity_category=EntityCategory.CONFIG
    )
    hass.states.async_set(settings.entity_id, "off")
    await _set_up(hass, {CONF_EXPOSED_ALEXA: [settings.entity_id]})

    assert (await _devices(await hass_ws_client(hass)))[settings.entity_id]["alexa"] is True


async def test_a_device_only_one_platform_supports_says_so(hass, google_sync):
    _add_devices(hass)
    entry = await _set_up(hass)

    rows = device_rows(hass, entry, {"google": {"light.kitchen"}, "alexa": set()})

    assert rows[0]["google_supported"] is True
    assert rows[0]["alexa_supported"] is False


async def test_exposing_changes_one_platform_and_tells_google(hass, hass_ws_client, google_sync):
    _add_devices(hass)
    entry = await _set_up(hass, {CONF_EXPOSED_ALEXA: ["switch.pump"], CONF_VOICE_PIN: "1234"})
    client = await hass_ws_client(hass)

    response = await _call(
        client, type=f"{DOMAIN}/voice/expose", platform="google", entity_ids=["light.kitchen"], expose=True
    )
    await hass.async_block_till_done()

    assert response["success"], response
    assert exposed_entity_ids(entry, "google") == {"light.kitchen"}
    assert exposed_entity_ids(entry, "alexa") == {"switch.pump"}
    assert entry.options[CONF_VOICE_PIN] == "1234"
    google_sync.assert_called_once()


async def test_removing_takes_the_device_away_from_the_voice_handlers(hass, hass_ws_client, google_sync):
    _add_devices(hass)
    entry = await _set_up(hass, {CONF_EXPOSED_GOOGLE: ["light.kitchen", "switch.pump"]})
    voice = hass.data[DOMAIN][entry.entry_id][2]
    assert voice._google.should_expose("light.kitchen")

    await _call(
        await hass_ws_client(hass),
        type=f"{DOMAIN}/voice/expose",
        platform="google",
        entity_ids=["light.kitchen"],
        expose=False,
    )

    assert not voice._google.should_expose("light.kitchen")
    assert voice._google.should_expose("switch.pump")


async def test_a_home_with_the_old_single_list_keeps_it_for_the_other_platform(hass, hass_ws_client, google_sync):
    _add_devices(hass)
    entry = await _set_up(hass, {CONF_EXPOSED_ENTITIES: ["light.kitchen", "switch.pump"]})

    await _call(
        await hass_ws_client(hass),
        type=f"{DOMAIN}/voice/expose",
        platform="google",
        entity_ids=["switch.pump"],
        expose=False,
    )

    assert exposed_entity_ids(entry, "google") == {"light.kitchen"}
    assert exposed_entity_ids(entry, "alexa") == {"light.kitchen", "switch.pump"}


async def test_a_camera_cannot_be_exposed_even_if_asked_directly(hass, hass_ws_client, google_sync):
    _add_devices(hass)
    entry = await _set_up(hass)

    await _call(
        await hass_ws_client(hass),
        type=f"{DOMAIN}/voice/expose",
        platform="google",
        entity_ids=["camera.door"],
        expose=True,
    )

    assert not exposed_entity_ids(entry, "google")


async def test_only_admins_can_use_the_page(hass, hass_ws_client, hass_read_only_access_token, google_sync):
    _add_devices(hass)
    entry = await _set_up(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    listed = await _call(client, type=f"{DOMAIN}/voice/devices")
    exposed = await _call(
        client, type=f"{DOMAIN}/voice/expose", platform="google", entity_ids=["light.kitchen"], expose=True
    )

    assert listed["error"]["code"] == "unauthorized"
    assert exposed["error"]["code"] == "unauthorized"
    assert not exposed_entity_ids(entry, "google")
    assert hass.data[DATA_PANELS][PANEL_URL_PATH].require_admin is True


async def test_page_goes_with_the_entry_and_comes_back_on_reload(hass, hass_ws_client, hass_client, google_sync):
    entry = await _set_up(hass)
    assert PANEL_URL_PATH in hass.data[DATA_PANELS]

    page = await (await hass_client()).get(f"{STATIC_URL}/{PANEL_FILE}")
    assert page.status == 200
    assert "customElements.define('mdtronix-voice-panel'" in await page.text()

    with patch(
        "custom_components.mdtronix_cloud.voice_config.AlexaVoiceConfig.async_disable_proactive_mode", AsyncMock()
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]

    response = await _call(await hass_ws_client(hass), type=f"{DOMAIN}/voice/devices")
    assert response["error"]["code"] == "not_loaded"

    tunnel = MagicMock(run=AsyncMock(), close=AsyncMock())
    with (
        patch("custom_components.mdtronix_cloud.TunnelClient", return_value=tunnel),
        patch("custom_components.mdtronix_cloud.voice_config.AlexaVoiceConfig.set_authorized", AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert PANEL_URL_PATH in hass.data[DATA_PANELS]


async def test_remote_access_survives_the_page_failing_to_load(hass, google_sync):
    with patch(
        "custom_components.mdtronix_cloud.async_register_voice_panel",
        AsyncMock(side_effect=RuntimeError("the page broke")),
    ):
        entry = await _set_up(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]
    client, _session, _voice, tunnel_task = hass.data[DOMAIN][entry.entry_id]
    assert not tunnel_task.cancelled()
    client.run.assert_called_once()

    with patch(
        "custom_components.mdtronix_cloud.voice_config.AlexaVoiceConfig.async_disable_proactive_mode", AsyncMock()
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_saving_the_pin_keeps_the_device_lists(hass, google_sync):
    options = {CONF_EXPOSED_GOOGLE: ["light.kitchen"], CONF_EXPOSED_ALEXA: ["switch.pump"]}
    entry = await _set_up(hass, options)

    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert set(form["data_schema"].schema) == {CONF_VOICE_PIN}
    await hass.config_entries.options.async_configure(form["flow_id"], {CONF_VOICE_PIN: "1234"})
    await hass.async_block_till_done()

    assert entry.options == {**options, CONF_VOICE_PIN: "1234"}
