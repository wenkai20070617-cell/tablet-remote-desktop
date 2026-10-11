from __future__ import annotations

import math
import re
from typing import Any

MAX_PASSWORD_LENGTH = 1024
MAX_CLIPBOARD_LENGTH = 1_000_000
MAX_TEXT_LENGTH = 16_384
MAX_JSON_FIELDS = 32
MAX_SHA256_LENGTH = 64
MAX_KEY_LENGTH = 32
MAX_SDP_LENGTH = 200_000

KEY_NAMES = {
    "Enter", "Backspace", "Tab", "Escape", "Delete", "Insert", "Home", "End",
    "PageUp", "PageDown", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", " ",
    "Control", "Shift", "Alt", "Meta", "CapsLock", "NumLock", "ScrollLock",
    "PrintScreen", "Pause", "ContextMenu", "Clear",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
}
_HEX = re.compile(r"^[0-9a-fA-F]*$")


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


def _flag(value: Any) -> bool:
    return value is True


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


def validate_transfer_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValidationError("invalid transfer id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValidationError("invalid transfer id")
    return value


def validate_relative_path(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 1024:
        raise ValidationError("invalid path")
    if "\x00" in value:
        raise ValidationError("invalid path")
    return value


def validate_sha256(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) != MAX_SHA256_LENGTH or not _HEX.match(value):
        raise ValidationError("invalid checksum")
    return value.lower()


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
        if not isinstance(key, str) or len(key) > MAX_KEY_LENGTH:
            raise ValidationError("invalid key")
        if not (len(key) == 1 or key in KEY_NAMES or key.isprintable()):
            raise ValidationError("invalid key")
        action = event.get("action", "press")
        if action not in {"down", "up", "press"}:
            raise ValidationError("invalid key action")
        token = event.get("token")
        if token is not None and (not isinstance(token, str) or len(token) > MAX_KEY_LENGTH):
            raise ValidationError("invalid key token")
        result.update(
            key=key,
            action=action,
            ctrl=_flag(event.get("ctrl")),
            meta=_flag(event.get("meta")),
            alt=_flag(event.get("alt")),
            shift=_flag(event.get("shift")),
            repeat=_flag(event.get("repeat")),
        )
        if token:
            result["token"] = token
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
        result = {
            "type": "client_info",
            "viewportWidth": _integer(message.get("viewportWidth", 0), 0, 10000, "viewportWidth"),
            "viewportHeight": _integer(message.get("viewportHeight", 0), 0, 10000, "viewportHeight"),
            "displayWidth": _integer(message.get("displayWidth", 0), 0, 10000, "displayWidth"),
            "displayHeight": _integer(message.get("displayHeight", 0), 0, 10000, "displayHeight"),
        }
        if "monitor" in message:
            result["monitor"] = _integer(message.get("monitor"), 0, 32, "monitor")
        return result
    if message_type == "input":
        return {"type": "input", **validate_input_event(message), "ack": _flag(message.get("ack"))}
    if message_type == "clipboard_set":
        return {"type": "clipboard_set", "text": validate_clipboard_payload(message)}
    if message_type == "clipboard_poll":
        return {"type": "clipboard_poll", "since": _integer(message.get("since", 0), 0, 2**53, "since")}
    if message_type == "transfer_progress":
        return {
            "type": "transfer_progress",
            "transferId": validate_transfer_id(message.get("transferId")),
            "received": _integer(message.get("received", 0), 0, 2**53, "received"),
            "total": _integer(message.get("total", 0), 0, 2**53, "total"),
            "direction": "download" if message.get("direction") == "download" else "upload",
            "name": str(message.get("name", ""))[:200],
        }
    if message_type == "transfer_cancel":
        return {
            "type": "transfer_cancel",
            "transferId": validate_transfer_id(message.get("transferId")),
            "removePartial": _flag(message.get("removePartial")),
        }
    if message_type == "monitor_select":
        return {"type": "monitor_select", "monitor": _integer(message.get("monitor"), 1, 32, "monitor")}
    if message_type == "stats":
        return {
            "type": "stats",
            "clientFps": _number(message.get("clientFps", 0), 0, 1000, "clientFps"),
            "clientRtt": _number(message.get("clientRtt", 0), 0, 600_000, "clientRtt"),
            "clientLoss": _number(message.get("clientLoss", 0), 0, 1, "clientLoss"),
            "dropped": _integer(message.get("dropped", 0), 0, 2**31, "dropped"),
            "mode": str(message.get("mode", ""))[:32],
        }
    if message_type == "ping":
        return {"type": "ping", "at": _number(message.get("at", 0), 0, 2**53, "at")}
    raise ValidationError("unknown message type")


def validate_webrtc_offer(value: Any) -> dict[str, Any]:
    data = _object(value)
    sdp = data.get("sdp")
    offer_type = data.get("type")
    if not isinstance(sdp, str) or not 1 <= len(sdp) <= MAX_SDP_LENGTH or offer_type != "offer":
        raise ValidationError("invalid WebRTC offer")
    viewport_width = _integer(data.get("viewportWidth", 1280), 640, 10000, "viewportWidth")
    viewport_height = _integer(data.get("viewportHeight", 720), 360, 10000, "viewportHeight")
    monitor = _integer(data.get("monitor", 0), 0, 32, "monitor")
    peer_id = data.get("peerId")
    if peer_id is not None and (not isinstance(peer_id, str) or len(peer_id) > 64):
        raise ValidationError("invalid peer id")
    return {
        "sdp": sdp,
        "type": offer_type,
        "viewportWidth": viewport_width,
        "viewportHeight": viewport_height,
        "monitor": monitor,
        "peerId": peer_id or "",
    }


def validate_upload_start(value: Any) -> dict[str, Any]:
    data = _object(value)
    name = data.get("name")
    if not isinstance(name, str) or not name or len(name) > 512:
        raise ValidationError("invalid file name")
    total = _integer(data.get("size", 0), 0, 1_099_511_627_776, "size")
    return {
        "name": name,
        "size": total,
        "relative": validate_relative_path(data.get("relative")),
        "overwrite": _flag(data.get("overwrite")),
        "sha256": validate_sha256(data.get("sha256")),
    }


def validate_upload_finish(value: Any) -> dict[str, Any]:
    data = _object(value)
    return {
        "transferId": validate_transfer_id(data.get("transferId")),
        "sha256": validate_sha256(data.get("sha256")),
    }


def validate_upload_status(value: Any) -> dict[str, Any]:
    data = _object(value)
    name = data.get("name")
    if not isinstance(name, str) or not name or len(name) > 512:
        raise ValidationError("invalid file name")
    return {
        "name": name,
        "size": _integer(data.get("size", 0), 0, 1_099_511_627_776, "size"),
        "relative": validate_relative_path(data.get("relative")),
    }
