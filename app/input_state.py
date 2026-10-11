"""Remote input injection and per-session input state.

Two rules matter for a weak link:

* The host must always be able to release what the client pressed, even when
  the connection that pressed it is already gone.  Input state is therefore
  keyed by session token, not by connection, and it is released explicitly
  when a session ends.
* A dropped *move* is invisible; a dropped *mouse-up* or *touch end* leaves a
  button stuck down on the host.  ``INPUT_RELIABLE_KINDS`` marks the events
  that must never be discarded.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Events that change a latched host state and therefore must be delivered.
INPUT_RELIABLE_KINDS = frozenset(
    {"down", "up", "click", "double", "mouse_button", "key", "touch", "wheel"}
)
# Events that only convey a transient position.
INPUT_MOTION_KINDS = frozenset({"move", "mouse_delta"})


def input_event_is_reliable(event: dict[str, Any]) -> bool:
    """Return whether dropping *event* could leave host input latched.

    Touch updates are position samples and may be coalesced just like mouse
    movement.  Touch down/up/cancel frames remain reliable because they change
    host state.
    """
    kind = str(event.get("kind", ""))
    if kind == "touch":
        contacts = event.get("contacts")
        if not isinstance(contacts, list) or not contacts:
            return True
        return any(
            not isinstance(contact, dict) or str(contact.get("state", "")) != "update"
            for contact in contacts
        )
    return kind in INPUT_RELIABLE_KINDS

BUTTON_NAMES = {0: "left", 1: "middle", 2: "right"}

KEY_MAP = {
    "Enter": "enter",
    "Backspace": "backspace",
    "Tab": "tab",
    "Escape": "esc",
    "Delete": "delete",
    "Insert": "insert",
    "Home": "home",
    "End": "end",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "ArrowUp": "up",
    "ArrowDown": "down",
    " ": "space",
    "Control": "ctrl",
    "ControlLeft": "ctrl",
    "ControlRight": "ctrl",
    "Shift": "shift",
    "ShiftLeft": "shift",
    "ShiftRight": "shift",
    "Alt": "alt",
    "AltLeft": "alt",
    "AltRight": "alt",
    "Meta": "win",
    "OS": "win",
    "CapsLock": "capslock",
    "NumLock": "numlock",
    "ScrollLock": "scrolllock",
    "PrintScreen": "printscreen",
    "Pause": "pause",
    "ContextMenu": "apps",
    "Clear": "clear",
}

# Windows virtual key codes for the keys that can be held down.
VK_CODES = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "win": 0x5B, "apps": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}
for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_CODES[_letter.lower()] = ord(_letter)
for _digit in "0123456789":
    VK_CODES[_digit] = ord(_digit)


def key_token(event: dict[str, Any]) -> str | None:
    """Normalise a browser key name into the token used by the host."""
    key = str(event.get("key", ""))
    if not key:
        return None
    if key in KEY_MAP:
        return KEY_MAP[key]
    if len(key) == 1:
        return key.lower()
    if key in VK_CODES:
        return key.lower()
    return key.lower()


def _windows_vk(token: str) -> int | None:
    if token in VK_CODES:
        return VK_CODES[token]
    if len(token) == 1:
        return ord(token.upper())
    return None


def _send_windows_key(vk: int, key_up: bool) -> None:
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class KeyBdInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [("ki", KeyBdInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", InputUnion)]

    keyeventf_keyup = 0x0002
    flags = keyeventf_keyup if key_up else 0
    unit = Input(type=1, union=InputUnion(ki=KeyBdInput(vk, 0, flags, 0, 0)))
    array = (Input * 1)(unit)
    sent = ctypes.windll.user32.SendInput(1, array, ctypes.sizeof(Input))
    if sent != 1:
        raise OSError(ctypes.get_last_error(), "SendInput failed")


def new_input_state(viewport_width: int = 0) -> dict[str, Any]:
    return {
        "viewport_width": int(viewport_width or 0),
        "touch_id_map": {},
        "touch_points": {},
        "buttons_down": set(),
        "keys_down": set(),
        "mouse_x": 0,
        "mouse_y": 0,
        "last_interaction_at": 0.0,
        "generic_touch_down": False,
    }


def reset_input_state(state: dict[str, Any]) -> None:
    state["touch_id_map"] = {}
    state["touch_points"] = {}
    state["buttons_down"] = set()
    state["keys_down"] = set()
    state["generic_touch_down"] = False
