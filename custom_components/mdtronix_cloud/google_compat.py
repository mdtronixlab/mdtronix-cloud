"""Where Home Assistant's Google answers differ from what Google's certification checks, and the fixes.

Home Assistant's Google handler answers the way its own cloud always has. Google's Test Suite is stricter
in two places:

- PIN. Google requires the owner's PIN to open or close a door, garage, gate or window. Home Assistant
  asks only when opening a door, garage or gate, and never for a window.
- Light color. Home Assistant reports color as HSV, and the Test Suite rejects every HSV color it reads
  back. It also requires a color while the light is off, when Home Assistant reports none. We answer in
  RGB, and remember each light's last color for when it is off.

The color fix has to cover every answer Google reads (SYNC, QUERY, EXECUTE and Report State), or the
Test Suite finds them disagreeing.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components import light
from homeassistant.components.google_assistant.const import (
    CHALLENGE_FAILED_PIN_NEEDED,
    CHALLENGE_PIN_NEEDED,
    ERR_CHALLENGE_NOT_SETUP,
)
from homeassistant.components.google_assistant.error import ChallengeNeeded, SmartHomeError
from homeassistant.components.google_assistant.trait import (
    COMMAND_OPEN_CLOSE,
    COMMAND_OPEN_CLOSE_RELATIVE,
    ColorSettingTrait,
)
from homeassistant.core import State
from homeassistant.util import color as color_util

PIN_COVER_CLASSES = frozenset({"door", "garage", "gate", "window"})
OPEN_CLOSE_COMMANDS = frozenset({COMMAND_OPEN_CLOSE, COMMAND_OPEN_CLOSE_RELATIVE})
# Google names the RGB state differently in QUERY answers and in Report State.
QUERY_RGB = "spectrumRgb"
REPORT_RGB = "spectrumRGB"
WHITE_RGB = 0xFFFFFF


def needs_cover_pin(state: State) -> bool:
    return state.domain == "cover" and state.attributes.get("device_class") in PIN_COVER_CLASSES


def cover_pin_error(state: State | None, executions: list[dict[str, Any]], pin: str | None) -> dict[str, Any] | None:
    """Google's error for opening or closing a door, garage, gate or window without the owner's PIN.

    None when the command may go ahead. With no PIN set, these covers can't be opened or closed by voice,
    the same as Home Assistant does for locks.
    """
    if state is None or not needs_cover_pin(state):
        return None
    for execution in executions:
        if execution.get("command") not in OPEN_CLOSE_COMMANDS:
            continue
        given = (execution.get("challenge") or {}).get("pin")
        if not pin:
            return SmartHomeError(ERR_CHALLENGE_NOT_SETUP, "Challenge is not set up").to_response()
        if not given:
            return ChallengeNeeded(CHALLENGE_PIN_NEEDED).to_response()
        if given != pin:
            return ChallengeNeeded(CHALLENGE_FAILED_PIN_NEEDED).to_response()
    return None


class LightColors:
    """Rewrites light colors into Google's RGB form, and remembers each light's last color."""

    def __init__(self) -> None:
        # Each light's last color, in QUERY form.
        self._last: dict[str, dict[str, Any]] = {}

    @staticmethod
    def fix_sync(devices: list[dict[str, Any]]) -> None:
        for device in devices:
            attributes = device.get("attributes") or {}
            if attributes.get("colorModel") == "hsv":
                attributes["colorModel"] = "rgb"

    def fix_states(self, state: State | None, states: dict[str, Any], rgb_key: str) -> None:
        """Rewrites one light's states in place. rgb_key is QUERY_RGB or REPORT_RGB."""
        if state is None or not states.get("online", True):
            return
        if not ColorSettingTrait.supported(state.domain, 0, None, state.attributes):
            return

        color = states.get("color")
        if color is None:
            color = dict(self._last.get(state.entity_id) or _default_color(state))
        else:
            if (hsv := color.pop("spectrumHsv", None)) is not None:
                # Brightness is its own trait, so the RGB is the color at full brightness.
                red, green, blue = color_util.color_hs_to_RGB(hsv["hue"], hsv["saturation"] * 100)
                color[QUERY_RGB] = (red << 16) | (green << 8) | blue
            color.pop(REPORT_RGB, None)
            self._last[state.entity_id] = dict(color)

        if QUERY_RGB in color:
            color[rgb_key] = color.pop(QUERY_RGB)
        states["color"] = color


def _default_color(state: State) -> dict[str, Any]:
    """A color for a light that has been off since Home Assistant started: white."""
    if light.color_supported(state.attributes.get("supported_color_modes")):
        return {QUERY_RGB: WHITE_RGB}
    low = state.attributes["min_color_temp_kelvin"]
    high = state.attributes["max_color_temp_kelvin"]
    return {"temperatureK": (low + high) // 2}
