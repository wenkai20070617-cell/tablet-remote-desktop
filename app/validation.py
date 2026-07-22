from __future__ import annotations

import math
from typing import Any

MAX_PASSWORD_LENGTH = 1024
MAX_CLIPBOARD_LENGTH = 1_000_000
MAX_TEXT_LENGTH = 16_384
MAX_JSON_FIELDS = 24

KEY_NAMES = {
    "Enter", "Backspace", "Tab", "Escape", "Delete", "Insert", "Home", "End",
    "PageUp", "PageDown", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", " ",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
}


class ValidationError(ValueError):
    pass


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > MAX_JSON_FIELDS:
        raise ValidationError("message must be a small JSON object")
    return value


def _number(value: Any, low: float, high: float, name: str) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number < low or number > high:
        raise ValidationError(f"{name} is outside the allowed range")
    return number


def _integer(value: Any, low: int, high: int, name: str) -> int:
    number = _number(value, low, high, name)
    if number != int(number):
        raise ValidationError(f"{name} must be an integer")
    return int(number)


def validate_password_payload(value: Any) -> str:
    data = _object(value)
    password = data.get("password")
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError("invalid password payload")
    return password


def validate_clipboard_payload(value: Any) -> str:
    data = _object(value)
    text = data.get("text")
    if not isinstance(text, str) or len(text) > MAX_CLIPBOARD_LENGTH:
        raise ValidationError("invalid clipboard payload")
    return text


def validate_input_event(value: Any) -> dict[str, Any]:
    event = _object(value)
    kind = event.get("kind")
    if not isinstance(kind, str):
        raise ValidationError("input kind is required")
    result: dict[str, Any] = {"kind": kind}
    if kind == "touch":
        contacts = event.get("contacts")
        if not isinstance(contacts, list) or not 1 <= len(contacts) <= 10:
            raise ValidationError("touch contacts must contain 1 to 10 items")
        normalized = []
        for raw in contacts:
            contact = _object(raw)
            state = contact.get("state", "update")
            if state not in {"down", "update", "up", "cancel"}:
                raise ValidationError("invalid touch state")
            normalized.append({
                "id": _integer(contact.get("id"), 0, 2_147_483_647, "touch id"),
                "x": _number(contact.get("x"), 0, 1, "x"),
                "y": _number(contact.get("y"), 0, 1, "y"),
                "state": state,
            })
        result["contacts"] = normalized
    elif kind in {"move", "down", "up", "click", "double"}:
        result.update(
            x=_number(event.get("x"), 0, 1, "x"),
            y=_number(event.get("y"), 0, 1, "y"),
            button=_integer(event.get("button", 0), 0, 2, "button"),
        )
    elif kind == "wheel":
        if "x" in event or "y" in event:
            result["x"] = _number(event.get("x"), 0, 1, "x")
            result["y"] = _number(event.get("y"), 0, 1, "y")
        result["delta"] = _integer(event.get("delta", 0), -100, 100, "delta")
        result["deltaX"] = _integer(event.get("deltaX", 0), -100, 100, "deltaX")
    elif kind == "mouse_delta":
        result["dx"] = _number(event.get("dx", 0), -5000, 5000, "dx")
        result["dy"] = _number(event.get("dy", 0), -5000, 5000, "dy")
    elif kind == "mouse_button":
        action = event.get("action", "click")
        if action not in {"down", "up", "click"}:
            raise ValidationError("invalid mouse action")
        result.update(
            button=_integer(event.get("button", 0), 0, 2, "button"),
            action=action,
        )
    elif kind == "key":
        key = event.get("key")
        if not isinstance(key, str) or not (len(key) == 1 or key in KEY_NAMES):
            raise ValidationError("invalid key")
        result.update(
            key=key,
            ctrl=event.get("ctrl") is True,
            meta=event.get("meta") is True,
            alt=event.get("alt") is True,
            shift=event.get("shift") is True,
        )
    elif kind == "text":
        text = event.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
            raise ValidationError("invalid text input")
        result["text"] = text
    else:
        raise ValidationError("unknown input kind")
    return result


def validate_client_message(value: Any) -> dict[str, Any]:
    message = _object(value)
    message_type = message.get("type")
    if message_type == "client_info":
        return {
            "type": "client_info",
            "viewportWidth": _integer(message.get("viewportWidth", 0), 0, 10000, "viewportWidth"),
        }
    if message_type == "input":
        return {"type": "input", **validate_input_event(message), "ack": message.get("ack") is True}
    if message_type == "clipboard_set":
        return {"type": "clipboard_set", "text": validate_clipboard_payload(message)}
    raise ValidationError("unknown message type")