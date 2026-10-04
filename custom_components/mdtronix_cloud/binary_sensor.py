"""Binary sensor: whether this home's remote-access tunnel is connected."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import state_signal
from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([RemoteAccessConnectedSensor(entry)])


class RemoteAccessConnectedSensor(BinarySensorEntity):
    """On while the tunnel to MDtronix is connected."""

    _attr_has_entity_name = True
    _attr_translation_key = "remote_access"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_remote_access"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="MDtronix",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, state_signal(self._entry.entry_id), self._handle_state)
        )

    @callback
    def _handle_state(self, connected: bool) -> None:
        self._attr_is_on = connected
        self.async_write_ha_state()
