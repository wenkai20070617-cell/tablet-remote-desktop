from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web
from PIL import Image

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.capture import (
    AdaptiveController,
    CaptureEngine,
    FramePacer,
    SharedScreen,
    cursor_reader,
    cursor_visible,
    frame_signature,
    monitors_json,
)
from app.config import (
    CONFIG_PATH,
    client_ice_servers,
    hash_password,
    load_config,
    turn_credentials,
)
from app.file_transfer import (
    CHUNK_READ,
    FileTransferError,
    TransferManager,
    directory_listing,
    safe_name,
    safe_relative,
    unique_path,
)
from app.input_state import (
    BUTTON_NAMES,
    input_event_is_reliable,
    key_token,
    new_input_state,
    reset_input_state,
    _send_windows_key,
    _windows_vk,
)
from app.platform_support import runtime_capabilities
from app.security import (
    LoginLimiter,
    SessionCapacityError,
    SessionStore,
    effective_scheme,
    forwarded_client_ip,
    host_allowed,
    is_loopback_address,
    origin_matches,
)
from app.validation import (
    ValidationError,
    validate_client_message,
    validate_clipboard_payload,
    validate_password_payload,
    validate_sha256,
    validate_upload_finish,
    validate_upload_start,
    validate_upload_status,
    validate_webrtc_offer,
)
from app.webrtc import (
    WEBRTC_AVAILABLE,
    accept_renegotiation,
    apply_sender_bitrate,
    collect_peer_stats,
    create_peer_answer,
    parse_control_message,
    peer_connection_expired,
    restart_ice,
)
from app.wayland_portal import WaylandPortalError, get_wayland_portal, is_wayland_session

try:
    import mss
    import pyperclip
    pyautogui = None
    if not is_wayland_session():
        import pyautogui
except Exception as exc:  # pragma: no cover - depends on the host desktop
    raise RuntimeError(
        "Missing desktop dependencies. Run the start script for this operating system."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "app" / "static"
CONFIG, GENERATED_PASSWORD = load_config()

if pyautogui is not None:
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0

CONNECTION_STATES: dict[str, dict[str, Any]] = {}
SESSION_WEBSOCKETS: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
PEER_SESSIONS: dict[str, "PeerSession"] = {}
# Input state is per session, not per connection, so a reconnect can still
# release anything the previous connection left pressed.
SESSION_INPUT: dict[str, dict[str, Any]] = {}
KEYBOARD_DEVICES: dict[str, Any] = {}
MULTIMEDIA_DEVICES: dict[str, Any] = {}

CAPTURE = CaptureEngine(
    with_cursor=bool(CONFIG.get("cursor_enabled")) and os.name != "nt",
    name="trd-capture",
)
SHARED_SCREEN = SharedScreen(
    CAPTURE,
    fps=float(CONFIG["webrtc_fps"]),
    min_fps=float(CONFIG["webrtc_min_fps"]),
)
_CONFIG_SNAPSHOT: dict[str, Any] = dict(CONFIG)
STARTED_AT = time.monotonic()


def current_config() -> dict[str, Any]:
    return _CONFIG_SNAPSHOT


def _transfer_root(value: Any, fallback: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return fallback
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


TRANSFERS = TransferManager(
    upload_root=_transfer_root(CONFIG.get("file_upload_root"), Path.home() / "Downloads"),
    download_root=_transfer_root(CONFIG.get("file_download_root"), Path.home()),
    max_size=int(CONFIG["file_max_size"]),
    reserve_free_bytes=int(CONFIG["file_reserve_free_bytes"]),
)


def forget_session_state(token: str) -> None:
    CONNECTION_STATES.pop(token, None)
    state = SESSION_INPUT.pop(token, None)
    if state is not None:
        reset_input_state(state)


SESSION_STORE = SessionStore(
    idle_ttl=int(CONFIG["session_idle_ttl_seconds"]),
    absolute_ttl=int(CONFIG["session_absolute_ttl_seconds"]),
    max_sessions=int(CONFIG["max_sessions"]),
    max_connections=int(CONFIG["max_connections_per_session"]),
    on_remove=forget_session_state,
)
LOGIN_LIMITER = LoginLimiter(
    attempts=max(1, int(CONFIG["login_attempts"])),
    window=max(1, int(CONFIG["login_window_seconds"])),
    lockout=max(1, int(CONFIG["login_lockout_seconds"])),
)
PASSWORD_VERIFY_SEMAPHORE = asyncio.Semaphore(2)
PASSWORD_VERIFY_PENDING = 0
MAX_PASSWORD_VERIFY_PENDING = 4
INPUT_LOCK = asyncio.Lock()
WAYLAND_PORTAL = get_wayland_portal()
GLOBAL_INTERACTION_STATE = {"last_interaction_at": 0.0}


def verify_password(password: str) -> bool:
    expected = str(CONFIG["password_hash"])
    actual = hash_password(password, str(CONFIG["password_salt"]))
    return hmac.compare_digest(expected, actual)


def make_session() -> str:
    return SESSION_STORE.create()


def get_session(request: web.Request) -> str | None:
    token = request.cookies.get("rd_session")
    record = SESSION_STORE.get(token)
    if record is None:
        if token:
            CONNECTION_STATES.pop(token, None)
        return None
    return record.token


def require_session(request: web.Request) -> str:
    token = get_session(request)
    if not token:
        raise web.HTTPUnauthorized(text="authentication required")
    return token


def transport_peer(request: web.Request) -> str:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"


def request_peer(request: web.Request) -> str:
    return forwarded_client_ip(
        transport_peer(request),
        request.headers.get("CF-Connecting-IP"),
        request.headers.get("X-Forwarded-For"),
    )


def request_scheme(request: web.Request) -> str:
    return effective_scheme(
        request.scheme,
        request.headers.get("X-Forwarded-Proto"),
        transport_peer(request),
    )


# --------------------------------------------------------------------------
# interaction bookkeeping
# --------------------------------------------------------------------------
def session_state(token: str) -> dict:
    return CONNECTION_STATES.setdefault(
        token,
        {
            "last_interaction_at": 0.0,
            "viewport_width": 0,
            "viewport_height": 0,
            "monitor": int(CONFIG.get("monitor", 1)),
        },
    )


def input_state(token: str) -> dict:
    return SESSION_INPUT.setdefault(token, new_input_state())


def mark_interaction(state: dict) -> None:
    now = time.monotonic()
    state["last_interaction_at"] = now
    GLOBAL_INTERACTION_STATE["last_interaction_at"] = now


def interaction_age(state: dict) -> float | None:
    last_at = max(
        float(state.get("last_interaction_at", 0)),
        float(GLOBAL_INTERACTION_STATE.get("last_interaction_at", 0)),
    )
    if last_at <= 0:
        return None
    return time.monotonic() - last_at


def monitor_region(state: dict) -> dict[str, int]:
    if WAYLAND_PORTAL is not None:
        return WAYLAND_PORTAL.monitor()
    selection = int(state.get("monitor", 1) or 1)
    return CAPTURE.monitor(selection).to_region()


def monitor_screen_size(state: dict) -> tuple[int, int]:
    region = monitor_region(state)
    return int(region["width"]), int(region["height"])


def capture_region(state: dict, _target_width: int) -> dict[str, int]:
    """Return the complete selected monitor.

    ``mss.grab`` crops a rectangle; it never scales one.  Resolution reduction
    therefore belongs to :meth:`CaptureEngine.encode`, after the whole monitor
    has been captured, otherwise the fallback channel only shows the top-left
    corner of the desktop.
    """
    full = state.get("monitor_region") or monitor_region(state)
    state["monitor_region"] = full
    return dict(full)


# --------------------------------------------------------------------------
# input injection
# --------------------------------------------------------------------------
def to_screen_point(state: dict, nx: float, ny: float) -> tuple[int, int]:
    region = state.get("monitor_region")
    if not region:
        region = monitor_region(state)
        state["monitor_region"] = region
    x = int(region["left"] + max(0.0, min(1.0, nx)) * region["width"])
    y = int(region["top"] + max(0.0, min(1.0, ny)) * region["height"])
    return x, y


_TOUCH_DEVICE = None


def inject_touch_contacts(state: dict, contacts: list[dict]) -> None:
    if not contacts:
        return
    if os.name != "nt":
        contact = contacts[0]
        x, y = to_screen_point(state, float(contact["x"]), float(contact["y"]))
        contact_state = str(contact["state"])
        pyautogui.moveTo(x, y)
        if contact_state == "down" and not state.get("generic_touch_down"):
            pyautogui.mouseDown(button="left")
            state["generic_touch_down"] = True
            state.setdefault("buttons_down", set()).add("left")
        elif contact_state in {"up", "cancel"} and state.get("generic_touch_down"):
            pyautogui.mouseUp(button="left")
            state["generic_touch_down"] = False
            state.setdefault("buttons_down", set()).discard("left")
        return

    import ctypes
    from ctypes import wintypes

    global _TOUCH_DEVICE

    touch_id_map: dict[int, int] = state.setdefault("touch_id_map", {})
    touch_points: dict[int, tuple[float, float]] = state.setdefault("touch_points", {})

    pointer_flag_inrange = 0x00000002
    pointer_flag_incontact = 0x00000004
    pointer_flag_primary = 0x00002000
    pointer_flag_confidence = 0x00004000
    pointer_flag_canceled = 0x00008000
    pointer_flag_down = 0x00010000
    pointer_flag_update = 0x00020000
    pointer_flag_up = 0x00040000
    pointer_change_none = 0
    pt_touch = 2
    pointer_feedback_default = 0x00000001
    touch_mask_contactarea = 0x00000001
    touch_mask_orientation = 0x00000002
    touch_mask_pressure = 0x00000004

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class PointerInfo(ctypes.Structure):
        _fields_ = [
            ("pointerType", wintypes.DWORD),
            ("pointerId", wintypes.UINT),
            ("frameId", wintypes.UINT),
            ("pointerFlags", wintypes.DWORD),
            ("sourceDevice", wintypes.HANDLE),
            ("hwndTarget", wintypes.HWND),
            ("ptPixelLocation", Point),
            ("ptHimetricLocation", Point),
            ("ptPixelLocationRaw", Point),
            ("ptHimetricLocationRaw", Point),
            ("dwTime", wintypes.DWORD),
            ("historyCount", wintypes.UINT),
            ("inputData", ctypes.c_int),
            ("dwKeyStates", wintypes.DWORD),
            ("performanceCount", ctypes.c_ulonglong),
            ("buttonChangeType", ctypes.c_int),
        ]

    class PointerTouchInfo(ctypes.Structure):
        _fields_ = [
            ("pointerInfo", PointerInfo),
            ("touchFlags", wintypes.DWORD),
            ("touchMask", wintypes.DWORD),
            ("rcContact", Rect),
            ("rcContactRaw", Rect),
            ("orientation", wintypes.UINT),
            ("pressure", wintypes.UINT),
        ]

    class PointerTypeUnion(ctypes.Union):
        _fields_ = [("touchInfo", PointerTouchInfo)]

    class PointerTypeInfo(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", PointerTypeUnion)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        create_device = user32.CreateSyntheticPointerDevice
        inject_pointer = user32.InjectSyntheticPointerInput
    except AttributeError as exc:
        raise RuntimeError("Windows synthetic touch API is unavailable") from exc

    create_device.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.DWORD)
    create_device.restype = wintypes.HANDLE
    inject_pointer.argtypes = (wintypes.HANDLE, ctypes.POINTER(PointerTypeInfo), wintypes.UINT)
    inject_pointer.restype = wintypes.BOOL

    if not _TOUCH_DEVICE:
        _TOUCH_DEVICE = create_device(pt_touch, 10, pointer_feedback_default)
        if not _TOUCH_DEVICE:
            raise OSError(ctypes.get_last_error(), "CreateSyntheticPointerDevice failed")

    safe_contacts = contacts[:10]
    frame = (PointerTypeInfo * len(safe_contacts))()
    release_ids: list[int] = []

    def mapped_pointer_id(raw_id: int, contact_state: str) -> int:
        raw_id = int(raw_id)
        if raw_id not in touch_id_map:
            used = set(touch_id_map.values())
            for candidate in range(10):
                if candidate not in used:
                    touch_id_map[raw_id] = candidate
                    break
            else:
                touch_id_map[raw_id] = raw_id % 10
        if contact_state in {"up", "cancel"}:
            release_ids.append(raw_id)
        return touch_id_map[raw_id]

    for index, contact in enumerate(safe_contacts):
        x, y = to_screen_point(state, float(contact.get("x", 0)), float(contact.get("y", 0)))
        contact_state = str(contact.get("state", "update"))
        raw_id = int(contact.get("id", index))
        touch_points[raw_id] = (float(contact.get("x", 0)), float(contact.get("y", 0)))
        pointer_id = mapped_pointer_id(raw_id, contact_state)
        flags = pointer_flag_inrange | pointer_flag_confidence
        if index == 0:
            flags |= pointer_flag_primary
        if contact_state == "down":
            flags |= pointer_flag_down | pointer_flag_incontact
        elif contact_state == "up":
            flags |= pointer_flag_up
        elif contact_state == "cancel":
            flags |= pointer_flag_up | pointer_flag_canceled
        else:
            flags |= pointer_flag_update | pointer_flag_incontact

        box = 6
        frame[index].type = pt_touch
        info = frame[index].union.touchInfo
        info.pointerInfo.pointerType = pt_touch
        info.pointerInfo.pointerId = pointer_id
        info.pointerInfo.pointerFlags = flags
        info.pointerInfo.ptPixelLocation = Point(x, y)
        info.pointerInfo.ptPixelLocationRaw = Point(x, y)
        info.pointerInfo.historyCount = 1
        info.pointerInfo.buttonChangeType = pointer_change_none
        info.touchMask = touch_mask_contactarea | touch_mask_orientation | touch_mask_pressure
        info.rcContact = Rect(x - box, y - box, x + box, y + box)
        info.rcContactRaw = Rect(x - box, y - box, x + box, y + box)
        info.orientation = 90
        info.pressure = 512

    if not inject_pointer(_TOUCH_DEVICE, frame, len(safe_contacts)):
        raise OSError(ctypes.get_last_error(), "InjectSyntheticPointerInput failed")

    for raw_id in release_ids:
        touch_id_map.pop(raw_id, None)
        touch_points.pop(raw_id, None)


def release_input_state(state: dict) -> None:
    touch_id_map = state.get("touch_id_map", {})
    touch_points = state.get("touch_points", {})
    if touch_id_map:
        contacts = [
            {"id": raw_id, "x": point[0], "y": point[1], "state": "cancel"}
            for raw_id, point in touch_points.items()
            if raw_id in touch_id_map
        ]
        if contacts:
            try:
                inject_touch_contacts(state, contacts)
            except Exception:
                pass
    state["touch_id_map"] = {}
    state["touch_points"] = {}

    for button in tuple(state.get("buttons_down", set())):
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            pass
    state.get("buttons_down", set()).clear()

    for token in tuple(state.get("keys_down", set())):
        try:
            release_key(token)
        except Exception:
            pass
    state.get("keys_down", set()).clear()
    state["generic_touch_down"] = False


def release_key(token: str) -> None:
    """Release one held key regardless of platform."""
    if WAYLAND_PORTAL is not None:
        return
    if os.name == "nt":
        vk = _windows_vk(token)
        if vk is None:
            return
        _send_windows_key(vk, True)
        return
    pyautogui.keyUp(token)


def press_key(token: str) -> None:
    if WAYLAND_PORTAL is not None:
        return
    if os.name == "nt":
        vk = _windows_vk(token)
        if vk is None:
            return
        _send_windows_key(vk, False)
        return
    pyautogui.keyDown(token)


def send_unicode_text(text: str) -> None:
    if not text:
        return
    if os.name != "nt":
        try:
            previous = pyperclip.paste()
            pyperclip.copy(text)
            pyautogui.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
            pyperclip.copy(previous)
        except Exception:
            pyautogui.write(text)
        return

    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class KeyBdInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeyBdInput), ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", InputUnion)]

    input_keyboard = 1
    keyeventf_keyup = 0x0002
    keyeventf_unicode = 0x0004
    send_input = ctypes.windll.user32.SendInput
    send_input.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
    send_input.restype = wintypes.UINT

    def send_unit(unit: int) -> None:
        down = Input(
            type=input_keyboard,
            union=InputUnion(ki=KeyBdInput(0, unit, keyeventf_unicode, 0, 0)),
        )
        up = Input(
            type=input_keyboard,
            union=InputUnion(ki=KeyBdInput(0, unit, keyeventf_unicode | keyeventf_keyup, 0, 0)),
        )
        inputs = (Input * 2)(down, up)
        sent = send_input(2, inputs, ctypes.sizeof(Input))
        if sent != 2:
            raise OSError(ctypes.get_last_error(), "SendInput failed")

    for char in text:
        if char == "\r":
            continue
        if char == "\n":
            pyautogui.press("enter")
            continue
        encoded = char.encode("utf-16-le")
        for index in range(0, len(encoded), 2):
            send_unit(int.from_bytes(encoded[index : index + 2], "little"))


def handle_input_event(state: dict, event: dict) -> None:
    mark_interaction(state)
    kind = event.get("kind")
    if kind == "touch":
        inject_touch_contacts(state, list(event.get("contacts", [])))
    elif kind in {"move", "down", "up", "click", "double"}:
        x, y = to_screen_point(state, float(event.get("x", 0)), float(event.get("y", 0)))
        state["mouse_x"], state["mouse_y"] = x, y
        pyautogui.moveTo(x, y)
        button = BUTTON_NAMES.get(int(event.get("button", 0) or 0), "left")
        if kind == "down":
            pyautogui.mouseDown(button=button)
            state.setdefault("buttons_down", set()).add(button)
        elif kind == "up":
            pyautogui.mouseUp(button=button)
            state.setdefault("buttons_down", set()).discard(button)
        elif kind == "click":
            pyautogui.click(button=button)
        elif kind == "double":
            pyautogui.doubleClick(button=button)
    elif kind == "wheel":
        if "x" in event and "y" in event:
            x, y = to_screen_point(state, float(event.get("x", 0)), float(event.get("y", 0)))
            state["mouse_x"], state["mouse_y"] = x, y
            pyautogui.moveTo(x, y)
        amount = int(event.get("delta", 0))
        if amount:
            pyautogui.scroll(-amount)
        horizontal = int(event.get("deltaX", 0))
        if horizontal and hasattr(pyautogui, "hscroll"):
            pyautogui.hscroll(-horizontal)
    elif kind == "mouse_delta":
        dx = int(float(event.get("dx", 0)))
        dy = int(float(event.get("dy", 0)))
        if dx or dy:
            pyautogui.moveRel(dx, dy, duration=0)
            state["mouse_x"] = int(state.get("mouse_x", 0)) + dx
            state["mouse_y"] = int(state.get("mouse_y", 0)) + dy
    elif kind == "mouse_button":
        button = BUTTON_NAMES.get(int(event.get("button", 0) or 0), "left")
        action = str(event.get("action", "click"))
        if action == "down":
            pyautogui.mouseDown(button=button)
            state.setdefault("buttons_down", set()).add(button)
        elif action == "up":
            pyautogui.mouseUp(button=button)
            state.setdefault("buttons_down", set()).discard(button)
        else:
            pyautogui.click(button=button)
    elif kind == "key":
        handle_key_event(state, event)
    elif kind == "text":
        send_unicode_text(str(event.get("text", "")))


def handle_key_event(state: dict, event: dict) -> None:
    token = str(event.get("token") or "") or key_token(event)
    if not token:
        return
    action = str(event.get("action", "press"))
    held: set = state.setdefault("keys_down", set())
    modifiers = []
    if event.get("ctrl"):
        modifiers.append("ctrl")
    if event.get("meta"):
        modifiers.append("command" if sys.platform == "darwin" else "win")
    if event.get("alt"):
        modifiers.append("alt")
    if event.get("shift"):
        modifiers.append("shift")
    if action == "down":
        if token in held:
            return
        if len(held) > 24:
            return
        press_key(token)
        held.add(token)
        return
    if action == "up":
        if token in held:
            release_key(token)
            held.discard(token)
        return
    # Tap semantics: keep the legacy combination behaviour for single presses.
    if len(token) == 1 and not modifiers:
        pyautogui.write(token)
        return
    if modifiers:
        pyautogui.hotkey(*modifiers, token)
        return
    pyautogui.press(token)


# --------------------------------------------------------------------------
# cursor overlay
# --------------------------------------------------------------------------
_CURSOR_READ = cursor_reader()


def cursor_payload(state: dict, image_size: tuple[int, int] | None = None) -> dict[str, Any] | None:
    if not CONFIG.get("cursor_enabled", True) or _CURSOR_READ is None:
        return None
    position = _CURSOR_READ()
    if position is None:
        return None
    region = state.get("monitor_region") or monitor_region(state)
    state["monitor_region"] = region
    x, y = position
    width = max(1, int(region["width"]))
    height = max(1, int(region["height"]))
    if not (
        int(region["left"]) <= x <= int(region["left"]) + width
        and int(region["top"]) <= y <= int(region["top"]) + height
    ):
        return {"x": 0.0, "y": 0.0, "visible": False}
    return {
        "x": round((x - int(region["left"])) / width, 5),
        "y": round((y - int(region["top"])) / height, 5),
        "visible": bool(cursor_visible()),
    }


# --------------------------------------------------------------------------
# protocol helpers
# --------------------------------------------------------------------------
def pack_binary_message(meta: dict, payload: bytes) -> bytes:
    header = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return len(header).to_bytes(4, "big") + header + payload


_ERROR_STATUS = {
    400: web.HTTPBadRequest,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
    413: web.HTTPRequestEntityTooLarge,
    422: web.HTTPUnprocessableEntity,
    503: web.HTTPServiceUnavailable,
}


def http_error(status: int, message: str) -> web.HTTPException:
    """Map a service-level status onto a concrete aiohttp exception."""
    factory = _ERROR_STATUS.get(int(status))
    if factory is None:
        return web.HTTPBadRequest(text=message, headers={"X-Error-Status": str(status)})
    return factory(text=message)


def consume_event_budget(state: dict, rate: float | None = None, burst: float | None = None) -> bool:
    rate = float(rate if rate is not None else CONFIG.get("input_message_rate", 600.0))
    burst = float(burst if burst is not None else CONFIG.get("input_message_burst", 1200.0))
    now = time.monotonic()
    bucket = state.setdefault("event_budget", {"tokens": burst, "at": now})
    elapsed = max(0.0, now - float(bucket["at"]))
    bucket["tokens"] = min(burst, float(bucket["tokens"]) + elapsed * rate)
    bucket["at"] = now
    if bucket["tokens"] < 1:
        return False
    bucket["tokens"] -= 1
    return True


def frame_profile(state: dict) -> dict[str, Any]:
    age = interaction_age(state)
    if age is not None and age < float(CONFIG.get("active_seconds", 0)):
        return {
            "name": "active",
            "quality": int(CONFIG.get("interactive_jpeg_quality", CONFIG["jpeg_quality"])),
            "max_width": frame_width(state, int(CONFIG.get("interactive_max_width", CONFIG["max_width"]))),
        }
    if age is not None and age < float(CONFIG.get("settle_seconds", 0)):
        return {
            "name": "settle",
            "quality": int(CONFIG.get("settle_jpeg_quality", CONFIG.get("interactive_jpeg_quality", CONFIG["jpeg_quality"]))),
            "max_width": frame_width(state, int(CONFIG.get("settle_max_width", CONFIG["max_width"]))),
        }
    if age is not None and age < float(CONFIG.get("preview_seconds", 0)):
        return {
            "name": "preview",
            "quality": int(CONFIG.get("preview_jpeg_quality", CONFIG["jpeg_quality"])),
            "max_width": frame_width(state, int(CONFIG.get("preview_max_width", CONFIG["max_width"]))),
        }
    return {
        "name": "full",
        "quality": int(CONFIG["jpeg_quality"]),
        "max_width": frame_width(state, int(CONFIG["max_width"])),
    }


def frame_width(state: dict, configured_width: int) -> int:
    configured_width = max(640, int(configured_width))
    viewport_width = int(state.get("viewport_width", 0) or 0)
    if viewport_width <= 0:
        return configured_width
    return max(640, min(configured_width, viewport_width))


def unchanged_frame_interval(profile_name: str) -> float:
    if profile_name == "preview":
        return float(CONFIG.get("unchanged_preview_interval", 0))
    if profile_name == "full":
        return float(CONFIG.get("unchanged_full_interval", 0))
    return 0.0


def encode_frame(monitor: dict, profile: dict, image_format: str) -> tuple[dict, bytes, str | None]:
    if WAYLAND_PORTAL is not None:
        image = WAYLAND_PORTAL.capture_image()
        monitor = WAYLAND_PORTAL.monitor()
    else:
        image = CAPTURE.grab(monitor)
    frame = CAPTURE.encode(
        image,
        int(profile["quality"]),
        int(profile["max_width"]),
        image_format,
        config=current_config,
    )
    meta = {
        "type": "frame",
        "width": frame.meta["width"],
        "height": frame.meta["height"],
        "screenWidth": monitor["width"],
        "screenHeight": monitor["height"],
        "left": monitor["left"],
        "top": monitor["top"],
        "format": frame.meta["format"],
        "profile": profile["name"],
    }
    if frame.signature:
        meta["signature"] = frame.signature
    return meta, frame.payload, frame.signature


def pending_write_bytes(ws: web.WebSocketResponse) -> int:
    """Bytes queued on the socket, for backpressure decisions.

    The attribute holding the transport moved between aiohttp releases, so it
    is probed rather than assumed.
    """
    for attribute in ("_writer", "_response", "_req"):
        owner = getattr(ws, attribute, None)
        transport = getattr(owner, "transport", None) if owner is not None else None
        if transport is None:
            continue
        try:
            return int(transport.get_write_buffer_size())
        except Exception:
            continue
    return 0


async def screen_loop(ws: web.WebSocketResponse, state: dict) -> None:
    """Adaptive JPEG/WebP fallback channel with real backpressure."""
    if WAYLAND_PORTAL is not None:
        if not WAYLAND_PORTAL.active:
            await ws.send_json({"type": "error", "message": "Wayland permission is required"})
            return
        monitor = WAYLAND_PORTAL.monitor()
    else:
        try:
            monitor = monitor_region(state)
        except Exception:
            await ws.send_json({"type": "error", "message": "screen capture unavailable"})
            return
    state["monitor_region"] = monitor
    controller = AdaptiveController(
        {
            "min_fps": CONFIG["min_fps"],
            "max_fps": CONFIG["fps"],
            "quality": CONFIG["jpeg_quality"],
            "min_quality": CONFIG["min_jpeg_quality"],
            "max_quality": min(90, CONFIG["jpeg_quality"] + 25),
            "min_width": CONFIG["min_width"],
            "max_width": CONFIG["max_width"],
            "cpu_budget": CONFIG["cpu_budget"],
            "bytes_budget": CONFIG["target_bytes_per_frame"],
        }
    )
    pacer = FramePacer(controller.fps)
    image_format = str(CONFIG.get("image_format", "jpeg"))
    last_signature: str | None = None
    last_sent_at = 0.0
    last_interval = 1.0 / max(1.0, controller.fps)
    paused = False
    queued_bytes = 0
    high_water = int(CONFIG["send_queue_high_water"])
    low_water = int(CONFIG["send_queue_low_water"])
    last_metrics_at = 0.0
    metrics_frames = 0
    metrics_bytes = 0

    while not ws.closed:
        interval = 1.0 / max(1.0, controller.fps)
        last_interval = interval
        profile = frame_profile(state)
        capture = capture_region(state, min(int(profile["max_width"]), int(controller.width)))
        try:
            frame = await asyncio.to_thread(
                CAPTURE.capture_encoded,
                capture,
                int(profile["quality"]),
                min(int(profile["max_width"]), int(controller.width)),
                image_format,
                current_config,
            )
            meta = {
                "type": "frame",
                "width": frame.meta["width"],
                "height": frame.meta["height"],
                "screenWidth": monitor["width"],
                "screenHeight": monitor["height"],
                "captureWidth": capture["width"],
                "captureHeight": capture["height"],
                "left": monitor["left"],
                "top": monitor["top"],
                "format": frame.meta["format"],
                "profile": profile["name"],
                "fps": round(controller.fps, 2),
            }
            if frame.signature:
                meta["signature"] = frame.signature
            payload = frame.payload
            signature = frame.signature
            changed = signature is None or signature != last_signature
            now = time.monotonic()
            unchanged_interval = unchanged_frame_interval(profile["name"])
            skip = (
                signature is not None
                and not changed
                and unchanged_interval > 0
                and now - last_sent_at < unchanged_interval
            )
            queue_ratio = 0.0
            pending = pending_write_bytes(ws)
            if pending:
                limit = max(1, len(payload) * high_water)
                queue_ratio = min(1.0, pending / limit)
                if paused:
                    if pending <= low_water * max(1, len(payload)):
                        paused = False
                    else:
                        skip = True
                elif pending >= limit:
                    paused = True
                    skip = True
            queued_bytes = pending
            if not skip:
                await asyncio.wait_for(
                    ws.send_bytes(pack_binary_message(meta, payload)),
                    timeout=float(CONFIG.get("frame_send_timeout_seconds", 2.5)),
                )
                last_sent_at = now
                metrics_frames += 1
                metrics_bytes += len(payload)
                if signature:
                    last_signature = signature
            if now - last_metrics_at > 1.0:
                meta_metrics = {
                    "type": "metrics",
                    "mode": "websocket",
                    "fps": round(metrics_frames / max(0.25, now - last_metrics_at), 2),
                    "bitrate": int(metrics_bytes * 8 / max(0.25, now - last_metrics_at)),
                    "encodeMs": round((frame.capture_seconds + frame.encode_seconds) * 1000, 1),
                    "quality": int(profile["quality"]),
                    "width": int(frame.meta["width"]),
                    "serverFpsTarget": round(controller.fps, 2),
                    "queued": int(queued_bytes),
                    "paused": paused,
                }
                last_metrics_at = now
                metrics_frames = 0
                metrics_bytes = 0
                try:
                    await ws.send_json(meta_metrics)
                except ConnectionError:
                    break
            controller.observe(
                capture_seconds=frame.capture_seconds,
                encode_seconds=frame.encode_seconds,
                payload_bytes=len(payload),
                interval=last_interval,
                changed=changed,
                queue_ratio=queue_ratio,
                frame_bytes=frame.meta["width"] * frame.meta["height"] * 3,
            )
            pacer.set_fps(controller.fps)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            break
        except ConnectionError:
            break
        except Exception as exc:
            if ws.closed:
                break
            try:
                await ws.send_json({"type": "error", "message": f"screen capture unavailable: {exc}"})
            except ConnectionError:
                break
            await asyncio.sleep(1)
            pacer.reset()
            continue
        await pacer.wait()


async def cursor_loop(ws: web.WebSocketResponse, state: dict) -> None:
    if not CONFIG.get("cursor_enabled", True) or _CURSOR_READ is None:
        return
    last_sent = ""
    while not ws.closed:
        await asyncio.sleep(float(CONFIG.get("cursor_interval_seconds", 0.1)))
        payload = await asyncio.to_thread(cursor_payload, state, None)
        if payload is None:
            return
        text = json.dumps(payload, separators=(",", ":"))
        if text == last_sent:
            continue
        last_sent = text
        try:
            await ws.send_json({"type": "cursor", **payload})
        except (ConnectionError, RuntimeError):
            return


# --------------------------------------------------------------------------
# HTTP routes
# --------------------------------------------------------------------------
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def login(request: web.Request) -> web.Response:
    global PASSWORD_VERIFY_PENDING
    identity = request_peer(request)
    retry_after = LOGIN_LIMITER.retry_after(identity)
    if retry_after:
        raise web.HTTPTooManyRequests(
            text="too many login attempts", headers={"Retry-After": str(retry_after)}
        )
    try:
        data = await request.json()
        password = validate_password_payload(data)
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid login request") from exc

    if PASSWORD_VERIFY_PENDING >= MAX_PASSWORD_VERIFY_PENDING:
        raise web.HTTPTooManyRequests(
            text="password verification queue is full", headers={"Retry-After": "1"}
        )
    PASSWORD_VERIFY_PENDING += 1
    try:
        async with PASSWORD_VERIFY_SEMAPHORE:
            retry_after = LOGIN_LIMITER.retry_after(identity)
            if retry_after:
                raise web.HTTPTooManyRequests(
                    text="too many login attempts", headers={"Retry-After": str(retry_after)}
                )
            valid = await asyncio.to_thread(verify_password, password)
    finally:
        PASSWORD_VERIFY_PENDING -= 1
    if not valid:
        retry_after = LOGIN_LIMITER.failure(identity)
        await asyncio.sleep(0.25)
        if retry_after:
            raise web.HTTPTooManyRequests(
                text="too many login attempts", headers={"Retry-After": str(retry_after)}
            )
        raise web.HTTPUnauthorized(text="bad password")

    LOGIN_LIMITER.success(identity)
    try:
        token = make_session()
    except SessionCapacityError as exc:
        raise web.HTTPTooManyRequests(
            text="all session slots are active", headers={"Retry-After": "15"}
        ) from exc
    max_age = int(CONFIG["session_absolute_ttl_seconds"])
    response = web.json_response({"ok": True})
    response.set_cookie(
        "rd_session",
        token,
        httponly=True,
        secure=request_scheme(request) == "https",
        samesite="Strict",
        max_age=max_age,
        path="/",
    )
    return response


async def logout(request: web.Request) -> web.Response:
    token = require_session(request)
    SESSION_STORE.revoke(token)
    CONNECTION_STATES.pop(token, None)
    await close_session_connections(token)
    response = web.json_response({"ok": True})
    response.del_cookie("rd_session", path="/")
    return response


async def health(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe for the supervisor and systemd.

    It reports only whether the process is serving and whether the local
    desktop is usable; it never exposes configuration or session data.
    """
    payload: dict[str, Any] = {
        "status": "ok",
        "uptime": round(time.monotonic() - STARTED_AT, 1),
        "sessions": len(CONNECTION_STATES),
        "peers": len(PEER_SESSIONS),
        "capture": "unavailable",
    }
    if WAYLAND_PORTAL is not None:
        payload["capture"] = "portal" if WAYLAND_PORTAL.active else "awaiting-permission"
    elif CAPTURE.last_error:
        payload["capture"] = "error"
        payload["status"] = "degraded"
        payload["detail"] = CAPTURE.last_error
    else:
        payload["capture"] = "ready" if CAPTURE.ready else "idle"
    return web.json_response(payload)


async def status(request: web.Request) -> web.Response:
    authenticated = bool(get_session(request))
    if not authenticated:
        return web.json_response({"authenticated": False})
    capabilities = runtime_capabilities()
    if WAYLAND_PORTAL is not None:
        capabilities["waylandAuthorized"] = WAYLAND_PORTAL.active
        if WAYLAND_PORTAL.active:
            capabilities.update(screen=True, pointer=True, keyboard=True, nativeTouch=True)
            capabilities.setdefault("permission", {})["remoteDesktopPortal"] = True
    capabilities.update(
        authenticated=True,
        audio=bool(CONFIG.get("audio_enabled")),
        webrtc=bool(CONFIG.get("webrtc_enabled")) and WEBRTC_AVAILABLE,
        stunUrls=[str(url) for url in CONFIG.get("stun_urls", []) if str(url).strip()],
        iceServers=client_ice_servers(CONFIG),
        turnConfigured=bool(CONFIG.get("turn_urls")),
        iceTransportPolicy=str(CONFIG.get("ice_transport_policy", "all")),
        websocket=True,
        clipboard=True,
        clipboardSync=bool(CONFIG.get("clipboard_sync_enabled", True)),
        fileTransfer=bool(CONFIG.get("file_transfer_enabled", True)),
        fileChunkSize=int(CONFIG["file_chunk_size"]),
        fileMaxSize=int(CONFIG["file_max_size"]),
        uploadRoot=str(TRANSFERS.upload_root),
        downloadRoot=str(TRANSFERS.download_root),
        multiMonitor=WAYLAND_PORTAL is None,
        monitors=monitors_json(None if WAYLAND_PORTAL is not None else CAPTURE),
        monitor=int(CONFIG.get("monitor", 1)),
        audioDevices=_audio_devices(),
        audioCodec=str(CONFIG.get("audio_codec", "adpcm")),
        audioJitterSeconds=float(CONFIG.get("audio_jitter_seconds", 0.18)),
        cursor=bool(CONFIG.get("cursor_enabled", True)) and _CURSOR_READ is not None,
        protocolVersion=3,
    )
    return web.json_response(capabilities)


def _audio_devices() -> list[dict[str, str]]:
    if not CONFIG.get("audio_enabled"):
        return []
    try:
        from app.audio import audio_devices_json

        return audio_devices_json()
    except Exception:
        return []


async def clipboard_get(request: web.Request) -> web.Response:
    require_session(request)
    try:
        value = await asyncio.to_thread(pyperclip.paste)
    except Exception as exc:
        raise web.HTTPServiceUnavailable(text="clipboard unavailable") from exc
    return web.json_response({"text": str(value)[:1_000_000]})


async def clipboard_set(request: web.Request) -> web.Response:
    require_session(request)
    try:
        text = validate_clipboard_payload(await request.json())
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid clipboard request") from exc
    try:
        await asyncio.to_thread(pyperclip.copy, text)
    except Exception as exc:
        raise web.HTTPServiceUnavailable(text="clipboard unavailable") from exc
    return web.json_response({"ok": True})


async def authorize_platform(request: web.Request) -> web.Response:
    require_session(request)
    if WAYLAND_PORTAL is None:
        return web.json_response({"required": False, "authorized": True})
    try:
        await WAYLAND_PORTAL.start()
    except WaylandPortalError as exc:
        raise web.HTTPServiceUnavailable(text="Wayland permission was not established") from exc
    return web.json_response(
        {"required": True, "authorized": True, "monitor": WAYLAND_PORTAL.monitor()}
    )


# --------------------------------------------------------------------------
# client message dispatch
# --------------------------------------------------------------------------
CLIPBOARD_STATE = {"text": "", "revision": 0, "at": 0.0}


async def clipboard_snapshot(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if force or now - float(CLIPBOARD_STATE["at"]) > float(CONFIG.get("clipboard_poll_seconds", 1.5)):
        try:
            text = await asyncio.to_thread(pyperclip.paste)
        except Exception:
            text = None
        if text is not None and text != CLIPBOARD_STATE["text"]:
            CLIPBOARD_STATE["text"] = str(text)[:1_000_000]
            CLIPBOARD_STATE["revision"] = int(CLIPBOARD_STATE["revision"]) + 1
        CLIPBOARD_STATE["at"] = now
    return {
        "text": CLIPBOARD_STATE["text"],
        "revision": int(CLIPBOARD_STATE["revision"]),
    }


async def apply_client_message(
    token: str,
    state: dict,
    event: dict,
    sink: Any = None,
    *,
    allow_clipboard: bool = True,
) -> dict[str, Any] | None:
    """Apply one validated client message; return an immediate reply if any."""
    kind = event["type"]
    if kind == "client_info":
        if event.get("viewportWidth"):
            state["viewport_width"] = int(event["viewportWidth"])
        if event.get("viewportHeight"):
            state["viewport_height"] = int(event["viewportHeight"])
        if event.get("monitor"):
            state["monitor"] = max(1, int(event["monitor"]))
            state.pop("monitor_region", None)
        return None
    if kind == "monitor_select":
        state["monitor"] = max(1, int(event["monitor"]))
        state.pop("monitor_region", None)
        region = monitor_region(state) if WAYLAND_PORTAL is None else WAYLAND_PORTAL.monitor()
        state["monitor_region"] = region
        return {
            "type": "monitor",
            "monitor": state["monitor"],
            "width": int(region["width"]),
            "height": int(region["height"]),
        }
    if kind == "input":
        reliable = input_event_is_reliable(event)
        if not consume_event_budget(
            state,
            rate=float(CONFIG["input_message_rate"]) if reliable else float(CONFIG["input_message_rate"]) * 1.5,
            burst=float(CONFIG["input_message_burst"]) if reliable else float(CONFIG["input_message_burst"]),
        ):
            if reliable:
                raise ValidationError("input rate exceeded")
            return None
        if WAYLAND_PORTAL is not None:
            if not WAYLAND_PORTAL.active:
                return None
            mark_interaction(state)
            async with INPUT_LOCK:
                await WAYLAND_PORTAL.handle_input(event)
        else:
            try:
                async with INPUT_LOCK:
                    await asyncio.to_thread(handle_input_event, state, event)
            except OSError as exc:
                # Windows refuses synthetic input while a higher-integrity
                # window (UAC, secure desktop, an elevated app) has focus.
                # Report it without tearing the control channel down.
                return {"type": "input_error", "message": f"host refused input: {exc}"}
        return {"type": "ack", "profile": frame_profile(state)["name"]} if event.get("ack") else None
    if kind == "clipboard_set":
        if not allow_clipboard:
            return None
        await asyncio.to_thread(pyperclip.copy, event["text"])
        CLIPBOARD_STATE["text"] = event["text"]
        CLIPBOARD_STATE["revision"] = int(CLIPBOARD_STATE["revision"]) + 1
        CLIPBOARD_STATE["at"] = time.monotonic()
        return None
    if kind == "clipboard_poll":
        if not allow_clipboard or not CONFIG.get("clipboard_sync_enabled", True):
            return None
        snapshot = await clipboard_snapshot()
        if snapshot["revision"] == int(event["since"]):
            return None
        return {"type": "clipboard", **snapshot, "sync": True}
    if kind == "transfer_progress":
        if sink is not None:
            await sink(event)
        return None
    if kind == "transfer_cancel":
        result = await TRANSFERS.cancel(event["transferId"], event["removePartial"])
        return {"type": "transfer_cancel_ack", **result, "transferId": event["transferId"]}
    if kind == "stats":
        state["client_stats"] = {
            "fps": event["clientFps"],
            "rtt": event["clientRtt"],
            "loss": event["clientLoss"],
            "dropped": event["dropped"],
            "mode": event["mode"],
            "at": time.monotonic(),
        }
        return None
    if kind == "ping":
        return {"type": "pong", "at": event["at"], "serverTime": time.time()}
    return None


async def send_json_safe(ws: web.WebSocketResponse, payload: dict) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except (ConnectionError, RuntimeError):
        return False


async def session_watchdog(token: str, ws: web.WebSocketResponse) -> None:
    while not ws.closed:
        await asyncio.sleep(15)
        if SESSION_STORE.get(token, touch=False) is None:
            await ws.close(code=1008, message=b"session expired")
            return


def unregister_websocket(token: str, ws: web.WebSocketResponse) -> None:
    sockets = SESSION_WEBSOCKETS.get(token)
    if sockets is None:
        return
    sockets.discard(ws)
    if not sockets:
        SESSION_WEBSOCKETS.pop(token, None)


async def close_session_connections(token: str) -> None:
    sockets = list(SESSION_WEBSOCKETS.pop(token, set()))
    if sockets:
        await asyncio.gather(
            *(ws.close(code=1008, message=b"session revoked") for ws in sockets),
            return_exceptions=True,
        )
    sessions = [
        (peer_id, entry) for peer_id, entry in list(PEER_SESSIONS.items())
        if entry.token == token
    ]
    for peer_id, entry in sessions:
        PEER_SESSIONS.pop(peer_id, None)
        await entry.shutdown()


async def release_connection_input(state: dict) -> None:
    if WAYLAND_PORTAL is not None:
        try:
            await WAYLAND_PORTAL.release_input()
        except Exception:
            pass
        return
    await asyncio.to_thread(release_input_state, state)


async def websocket(request: web.Request) -> web.WebSocketResponse:
    token = require_session(request)
    if not SESSION_STORE.acquire_connection(token):
        raise web.HTTPTooManyRequests(text="session connection limit reached")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1_100_000, compress=False)
    try:
        await ws.prepare(request)
        SESSION_WEBSOCKETS[token].add(ws)
        state = session_state(token)
        inp = input_state(token)
        if WAYLAND_PORTAL is not None and not WAYLAND_PORTAL.active:
            await send_json_safe(ws, {"type": "error", "message": "Wayland permission is required"})
            await ws.close(code=1008, message=b"permission required")
            return ws
        await send_json_safe(
            ws,
            {
                "type": "hello",
                "message": "connected",
                "protocolVersion": 3,
                "mediaTransports": ["webrtc", "websocket"] if WEBRTC_AVAILABLE else ["websocket"],
                "audioCodec": str(CONFIG.get("audio_codec", "adpcm")),
                "monitors": monitors_json(None if WAYLAND_PORTAL is not None else CAPTURE),
            },
        )
        tasks = [
            asyncio.create_task(text_focus_loop(ws)),
            asyncio.create_task(session_watchdog(token, ws)),
            asyncio.create_task(clipboard_loop(ws)),
            asyncio.create_task(cursor_loop(ws, state)),
        ]
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    event = validate_client_message(json.loads(message.data))
                    if SESSION_STORE.get(token) is None:
                        await ws.close(code=1008, message=b"session expired")
                        break
                    reply = await apply_client_message(token, inp, event)
                    if reply:
                        await send_json_safe(ws, reply)
                    # A client that announces itself as an input-only channel
                    # must not receive screen frames on the same socket.
                    if "screen" not in tasks and not event.get("inputOnly"):
                        tasks.append(asyncio.create_task(screen_loop(ws, state)))
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                    await ws.close(code=1008, message=b"invalid control message")
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return ws
    finally:
        unregister_websocket(token, ws)
        SESSION_STORE.release_connection(token)


async def input_websocket(request: web.Request) -> web.WebSocketResponse:
    """Control-only channel: never carries screen frames, so input never waits
    behind a frame on the wire."""
    token = require_session(request)
    if not SESSION_STORE.acquire_connection(token):
        raise web.HTTPTooManyRequests(text="session connection limit reached")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1_100_000, compress=False)
    try:
        await ws.prepare(request)
        SESSION_WEBSOCKETS[token].add(ws)
        state = session_state(token)
        inp = input_state(token)
        tasks = [
            asyncio.create_task(session_watchdog(token, ws)),
            asyncio.create_task(text_focus_loop(ws)),
            asyncio.create_task(cursor_loop(ws, state)),
            asyncio.create_task(metrics_loop(ws, state, "websocket")),
        ]
        await send_json_safe(ws, {"type": "hello", "message": "input connected"})
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    event = validate_client_message(json.loads(message.data))
                    if SESSION_STORE.get(token) is None:
                        await ws.close(code=1008, message=b"session expired")
                        break
                    reply = await apply_client_message(token, inp, event)
                    if reply:
                        await send_json_safe(ws, reply)
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                    await ws.close(code=1008, message=b"invalid control message")
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return ws
    finally:
        unregister_websocket(token, ws)
        SESSION_STORE.release_connection(token)


async def audio_websocket(request: web.Request) -> web.WebSocketResponse:
    token = require_session(request)
    if not SESSION_STORE.acquire_connection(token):
        raise web.HTTPTooManyRequests(text="session connection limit reached")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096, compress=False)
    try:
        await ws.prepare(request)
        SESSION_WEBSOCKETS[token].add(ws)
        task = asyncio.create_task(audio_loop(ws))
        watchdog = asyncio.create_task(session_watchdog(token, ws))
        try:
            async for _ in ws:
                if SESSION_STORE.get(token) is None:
                    await ws.close(code=1008, message=b"session expired")
                    break
        finally:
            task.cancel()
            watchdog.cancel()
            await asyncio.gather(task, watchdog, return_exceptions=True)
        return ws
    finally:
        unregister_websocket(token, ws)
        SESSION_STORE.release_connection(token)


# --------------------------------------------------------------------------
# clipboard, focus and metrics loops
# --------------------------------------------------------------------------
async def clipboard_loop(ws: web.WebSocketResponse) -> None:
    if not CONFIG.get("clipboard_sync_enabled", True):
        return
    last_revision = int(CLIPBOARD_STATE["revision"])
    while not ws.closed:
        await asyncio.sleep(float(CONFIG.get("clipboard_poll_seconds", 1.5)))
        try:
            snapshot = await clipboard_snapshot(force=True)
        except Exception:
            continue
        if snapshot["revision"] == last_revision:
            continue
        last_revision = int(snapshot["revision"])
        if not await send_json_safe(ws, {"type": "clipboard", **snapshot, "sync": True}):
            return


def text_caret_active() -> bool:
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class GuiThreadInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", Rect),
        ]

    info = GuiThreadInfo()
    info.cbSize = ctypes.sizeof(GuiThreadInfo)
    if not ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return False
    return bool(info.hwndCaret)


async def text_focus_loop(ws: web.WebSocketResponse) -> None:
    last = None
    while not ws.closed:
        try:
            active = await asyncio.to_thread(text_caret_active)
            if active != last:
                last = active
                if not await send_json_safe(ws, {"type": "text_focus", "active": active}):
                    return
        except Exception:
            pass
        await asyncio.sleep(0.12)


async def metrics_loop(ws: web.WebSocketResponse, state: dict, mode: str) -> None:
    while not ws.closed:
        await asyncio.sleep(1.0)
        client = state.get("client_stats") or {}
        payload = {
            "type": "server_metrics",
            "mode": mode,
            "fingerprint": _fingerprint(),
            "client": client,
        }
        if not await send_json_safe(ws, payload):
            return


def _fingerprint() -> dict[str, Any]:
    return {
        "version": "3",
        "cursor": bool(CONFIG.get("cursor_enabled", True)) and _CURSOR_READ is not None,
        "clipboardSync": bool(CONFIG.get("clipboard_sync_enabled", True)),
        "audioCodec": str(CONFIG.get("audio_codec", "adpcm")),
    }


# --------------------------------------------------------------------------
# audio (WebSocket fallback, ADPCM compressed)
# --------------------------------------------------------------------------
async def audio_loop(ws: web.WebSocketResponse) -> None:
    if not CONFIG.get("audio_enabled"):
        return
    try:
        from app.audio import AudioCapture, encode_interleaved, frame_payload_size
    except Exception as exc:
        await send_json_safe(
            ws, {"type": "audio_status", "ok": False, "message": f"system audio unavailable: {exc}"}
        )
        return

    codec = str(CONFIG.get("audio_codec", "adpcm"))
    sample_rate = int(CONFIG["audio_sample_rate"])
    frames = int(CONFIG["audio_frames"])
    channels = 2
    capture = AudioCapture(sample_rate, frames, str(CONFIG.get("audio_device", "")), channels)
    try:
        await asyncio.to_thread(capture.open)
    except Exception as exc:
        await send_json_safe(
            ws, {"type": "audio_status", "ok": False, "message": str(exc) or "system audio unavailable"}
        )
        return
    await send_json_safe(
        ws,
        {
            "type": "audio_status",
            "ok": True,
            "codec": codec,
            "sampleRate": sample_rate,
            "channels": channels,
            "frames": frames,
            "device": capture.device_name,
        },
    )
    state: list[tuple[int, int]] = [(0, 0) for _ in range(channels)]
    sequence = 0
    try:
        while not ws.closed:
            pcm = await asyncio.to_thread(capture.read)
            if codec == "adpcm":
                payload = encode_interleaved(pcm, channels, state)
            else:
                payload = pcm
            meta = {
                "type": "audio",
                "codec": codec if codec == "adpcm" else "pcm",
                "sampleRate": sample_rate,
                "channels": channels,
                "frames": frames,
                "seq": sequence,
                "sentAt": round(time.time(), 4),
                "bytes": len(payload),
            }
            sequence = (sequence + 1) % 1_000_000
            try:
                await asyncio.wait_for(
                    ws.send_bytes(pack_binary_message(meta, payload)),
                    timeout=float(CONFIG.get("frame_send_timeout_seconds", 2.5)),
                )
            except asyncio.TimeoutError:
                break
            except ConnectionError:
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await send_json_safe(
            ws, {"type": "audio_status", "ok": False, "message": f"system audio stopped: {exc}"}
        )
    finally:
        await asyncio.to_thread(capture.close)


# --------------------------------------------------------------------------
# file transfer routes
# --------------------------------------------------------------------------
def require_file_transfer() -> None:
    if not CONFIG.get("file_transfer_enabled", True):
        raise web.HTTPServiceUnavailable(text="file transfer is disabled")


async def upload_begin(request: web.Request) -> web.Response:
    require_session(request)
    require_file_transfer()
    try:
        payload = validate_upload_start(await request.json())
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid upload request") from exc
    try:
        transfer = await TRANSFERS.begin(
            payload["name"],
            payload["size"],
            payload["relative"],
            payload["overwrite"],
        )
    except FileTransferError as exc:
        raise http_error(exc.status, str(exc)) from exc
    return web.json_response(
        {
            "transferId": transfer.transfer_id,
            "offset": transfer.received,
            "total": transfer.total,
            "name": transfer.name,
            "path": str(transfer.path),
            "chunkSize": int(CONFIG["file_chunk_size"]),
            "resumeBlockSize": CHUNK_READ,
            "resumeHashes": list(transfer.resume_hashes),
        }
    )


async def upload_status(request: web.Request) -> web.Response:
    require_session(request)
    require_file_transfer()
    try:
        payload = validate_upload_status(await request.json())
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid status request") from exc
    try:
        result = await TRANSFERS.status(payload["name"], payload["size"], payload["relative"])
    except FileTransferError as exc:
        raise http_error(exc.status, str(exc)) from exc
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"invalid path: {exc}") from exc
    return web.json_response(result)


async def upload_chunk(request: web.Request) -> web.Response:
    require_session(request)
    require_file_transfer()
    transfer_id = request.match_info.get("transfer_id", "")
    try:
        offset = int(request.headers.get("X-Transfer-Offset", "-1"))
    except (TypeError, ValueError):
        offset = -1
    if offset < 0:
        raise web.HTTPBadRequest(text="missing transfer offset")
    chunk = await request.read()
    if not chunk:
        raise web.HTTPBadRequest(text="empty chunk")
    if len(chunk) > int(CONFIG["file_chunk_size"]):
        raise web.HTTPRequestEntityTooLarge(
            max_size=int(CONFIG["file_chunk_size"]), actual_size=len(chunk)
        )
    try:
        chunk_sha256 = validate_sha256(request.headers.get("X-Chunk-SHA256"))
    except ValidationError as exc:
        raise web.HTTPBadRequest(text="invalid chunk checksum") from exc
    if not chunk_sha256:
        raise web.HTTPBadRequest(text="missing chunk checksum")
    try:
        result = await TRANSFERS.write_chunk(transfer_id, offset, chunk, chunk_sha256)
    except FileTransferError as exc:
        raise http_error(exc.status, str(exc)) from exc
    return web.json_response(result)


async def upload_finish(request: web.Request) -> web.Response:
    require_session(request)
    require_file_transfer()
    try:
        payload = validate_upload_finish(await request.json())
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid finish request") from exc
    try:
        result = await TRANSFERS.finish(payload["transferId"], payload["sha256"])
    except FileTransferError as exc:
        raise http_error(exc.status, str(exc)) from exc
    return web.json_response(result)


async def upload_cancel(request: web.Request) -> web.Response:
    require_session(request)
    transfer_id = request.match_info.get("transfer_id", "")
    remove = request.query.get("remove") == "1"
    result = await TRANSFERS.cancel(transfer_id, remove)
    return web.json_response(result)


async def files_list(request: web.Request) -> web.Response:
    require_session(request)
    require_file_transfer()
    relative = request.query.get("path", "")
    root_kind = request.query.get("root", "upload")
    root = TRANSFERS.download_root if root_kind == "download" else TRANSFERS.upload_root
    try:
        listing = await asyncio.to_thread(directory_listing, root, relative)
    except FileTransferError as exc:
        raise http_error(exc.status, str(exc)) from exc
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"invalid path: {exc}") from exc
    return web.json_response(
        {
            "root": root_kind,
            "rootPath": str(root),
            **listing,
        }
    )


async def files_download(request: web.Request) -> web.Response:
    require_session(request)
    require_file_transfer()
    relative = request.query.get("path", "")
    try:
        target = await asyncio.to_thread(TRANSFERS.resolve_download, relative)
    except FileTransferError as exc:
        raise http_error(exc.status, str(exc)) from exc
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"invalid path: {exc}") from exc
    response = web.FileResponse(
        target,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(target.name)}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        },
    )
    return response


async def files_delete(request: web.Request) -> web.Response:
    require_session(request)
    require_file_transfer()
    relative = request.query.get("path", "")
    root_kind = request.query.get("root", "upload")
    try:
        result = await asyncio.to_thread(TRANSFERS.delete_file, relative, root_kind)
    except FileTransferError as exc:
        raise http_error(exc.status, str(exc)) from exc
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"invalid path: {exc}") from exc
    return web.json_response(result)


# --------------------------------------------------------------------------
# WebRTC peer sessions
# --------------------------------------------------------------------------
class PeerSession:
    """One browser peer, kept alive across a transient network interruption.

    ``failed``/``disconnected`` no longer tears the session down immediately:
    the peer is held for ``webrtc_grace_seconds`` so an ICE restart can revive
    it, and only then is the slot released for a fresh negotiation.
    """

    def __init__(self, peer_id: str, token: str, state: dict, inp: dict, monitor: int) -> None:
        self.peer_id = peer_id
        self.token = token
        self.state = state
        self.input_state = inp
        self.monitor = monitor
        self.pc: Any = None
        self.video_track: Any = None
        self.audio_track: Any = None
        self.audio_device = ""
        self.control_channel: Any = None
        self.motion_channel: Any = None
        self.control_queue: asyncio.Queue[tuple[str, str]] | None = None
        self.motion_queue: asyncio.Queue[tuple[str, str]] | None = None
        self.queue_ready = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.supervisor: asyncio.Task | None = None
        self.closed = False
        self.connection_released = False
        self.input_released = False
        self.created_at = time.monotonic()
        self.last_seen_at = time.monotonic()
        self.last_frame_at = 0.0
        self.last_encoded_bytes = 0
        self.last_encoded_at = 0.0
        self.recoveries = 0
        self.ice_restarts = 0
        self.connection_state = "new"
        self.unhealthy_since: float | None = None
        self.ever_connected = False
        self.controller = AdaptiveController(
            {
                "min_fps": CONFIG["webrtc_min_fps"],
                "max_fps": CONFIG["webrtc_fps"],
                "quality": 72,
                # WebRTC quality is controlled by the codec bitrate.  Keep the
                # JPEG-only quality dimension fixed so congestion immediately
                # reduces real work (resolution / frame rate).
                "min_quality": 72,
                "max_quality": 72,
                "min_width": CONFIG["min_width"],
                "max_width": CONFIG["webrtc_max_width"],
                "width_step": 192,
                "cpu_budget": CONFIG["cpu_budget"],
                "bytes_budget": max(65_536, CONFIG["webrtc_bitrate_start"] // 8 // 30 * 2),
            }
        )
        self.controller.fps = min(
            float(CONFIG["webrtc_fps"]), max(float(CONFIG["webrtc_min_fps"]), 24.0)
        )
        self.controller.width = min(int(CONFIG["webrtc_max_width"]), 1920)
        self.target_bitrate = int(CONFIG["webrtc_bitrate_start"])
        self.applied_bitrate = 0
        self._last_sequence = -1
        self._latest_image: Any = None
        self._latest_at = 0.0
        self._last_encode_seconds = 0.0
        self._last_signature: str | None = None
        self._last_changed = True
        self._last_stats_bytes = 0
        self._last_stats_frames = 0
        self._frames = 0
        self._bytes = 0
        self._send_failures = 0
        self._stats_at = 0.0
        self._pipeline: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------
    async def start(self, offer_sdp: str, offer_type: str) -> dict[str, Any]:
        stun_urls = [str(item) for item in CONFIG.get("stun_urls", [])]
        pc, answer = await create_peer_answer(
            offer_sdp=offer_sdp,
            offer_type=offer_type,
            frame_source=self.next_frame,
            on_control_message=lambda _raw: None,
            on_state_change=self.on_state_change,
            stun_urls=stun_urls,
            fps=int(CONFIG["webrtc_fps"]),
            min_fps=int(CONFIG["webrtc_min_fps"]),
            audio_enabled=bool(CONFIG.get("audio_enabled")),
            audio_sample_rate=int(CONFIG["audio_sample_rate"]),
            audio_frames=int(CONFIG["audio_frames"]),
            ice_servers=CONFIG.get("ice_servers_normalized", []),
            audio_device=str(CONFIG.get("audio_device", "")),
            on_datachannel=self.on_datachannel,
            ice_transport_policy=str(CONFIG.get("ice_transport_policy", "all")),
            video_bitrate=int(CONFIG["webrtc_bitrate_start"]),
        )
        self.pc = pc
        self.video_track = answer.get("videoTrack")
        self.audio_track = answer.get("audioTrack")
        self.audio_device = str(answer.get("audioDevice") or "")
        self.control_queue = asyncio.Queue(maxsize=max(2, int(CONFIG["input_reliable_queue"])))
        self.motion_queue = asyncio.Queue(maxsize=max(1, int(CONFIG["input_motion_queue"])))
        self.worker = asyncio.create_task(self.control_worker())
        self.supervisor = asyncio.create_task(self.supervise())
        self._pipeline = asyncio.create_task(self.video_pipeline())
        SHARED_SCREEN.set_fps(float(CONFIG["webrtc_fps"]))
        SHARED_SCREEN.set_region(monitor_region(self.state))
        SHARED_SCREEN.start()
        return answer

    async def renegotiate(self, offer_sdp: str, offer_type: str) -> dict[str, Any]:
        """Answer a browser-initiated ICE restart without a new peer slot."""
        if self.pc is None:
            raise RuntimeError("peer is not connected")
        answer = await accept_renegotiation(self.pc, offer_sdp, offer_type)
        self.ice_restarts += 1
        self.last_seen_at = time.monotonic()
        return answer

    async def restart_ice(self) -> dict[str, Any]:
        if self.pc is None:
            raise RuntimeError("peer is not connected")
        answer = await restart_ice(self.pc)
        self.ice_restarts += 1
        return answer

    def release_connection(self) -> None:
        if not self.connection_released:
            self.connection_released = True
            SESSION_STORE.release_connection(self.token)

    async def release_input(self) -> None:
        if self.input_released:
            return
        self.input_released = True
        await release_connection_input(self.input_state)

    async def shutdown(self) -> None:
        if self.closed and self.pc is None:
            return
        self.closed = True
        current = asyncio.current_task()
        pending = [
            task
            for task in (self.worker, self.supervisor, self._pipeline)
            if task is not None and task is not current
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.worker = self.supervisor = self._pipeline = None
        self.control_queue = None
        self.motion_queue = None
        pc = self.pc
        self.pc = None
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass
        await self.release_input()
        self.release_connection()

    # -- channels ------------------------------------------------------
    def on_datachannel(self, channel: Any) -> None:
        label = getattr(channel, "label", "")
        if label not in {"control", "motion"}:
            try:
                channel.close()
            except Exception:
                pass
            return
        if label == "control":
            self.control_channel = channel
        else:
            self.motion_channel = channel

        @channel.on("message")
        def on_message(message: Any) -> None:
            if isinstance(message, str):
                self.on_channel_message(label, message)

    def on_channel_message(self, label: str, raw: str) -> None:
        if len(raw) > 65_536:
            return
        self.last_seen_at = time.monotonic()
        control_queue = self.control_queue
        motion_queue = self.motion_queue
        if control_queue is None or motion_queue is None:
            return
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "input":
            reliable = input_event_is_reliable(event)
        else:
            # Client information, clipboard and monitor commands always retain
            # ordering even if a broken client labels them as motion.
            reliable = True
        if not reliable:
            try:
                motion_queue.put_nowait(("motion", raw))
            except asyncio.QueueFull:
                # Only the motion queue is touched: no state-changing control
                # event can be discarded to make room for a pointer sample.
                try:
                    motion_queue.get_nowait()
                    motion_queue.put_nowait(("motion", raw))
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
            self.queue_ready.set()
            return
        try:
            control_queue.put_nowait(("control", raw))
        except asyncio.QueueFull:
            # A saturated reliable queue cannot safely choose an event to drop.
            # Put an ordered reset before the newest command so all host-side
            # latched state is released before input resumes.
            while True:
                try:
                    control_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            control_queue.put_nowait(("reset", ""))
            control_queue.put_nowait(("control", raw))
        self.queue_ready.set()

    async def control_worker(self) -> None:
        control_queue = self.control_queue
        motion_queue = self.motion_queue
        if control_queue is None or motion_queue is None:
            return
        while not self.closed:
            item: tuple[str, str] | None = None
            try:
                item = control_queue.get_nowait()
            except asyncio.QueueEmpty:
                try:
                    item = motion_queue.get_nowait()
                except asyncio.QueueEmpty:
                    self.queue_ready.clear()
                    if not control_queue.empty() or not motion_queue.empty():
                        self.queue_ready.set()
                        continue
                    await self.queue_ready.wait()
                    continue
            priority, raw = item
            if priority == "reset":
                await release_connection_input(self.input_state)
                continue
            if SESSION_STORE.get(self.token) is None:
                return
            try:
                event = validate_client_message(parse_control_message(raw))
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                continue
            try:
                reply = await apply_client_message(self.token, self.input_state, event)
            except ValidationError:
                continue
            except Exception:
                continue
            if reply and self.control_channel is not None:
                try:
                    self.control_channel.send(json.dumps(reply, separators=(",", ":")))
                except Exception:
                    pass

    def on_state_change(self, connection_state: str) -> None:
        now = time.monotonic()
        self.last_seen_at = now
        self.connection_state = str(connection_state)
        if connection_state == "connected":
            if self.ever_connected:
                self.recoveries += 1
            self.ever_connected = True
            self.unhealthy_since = None
            if self.video_track is not None:
                self.video_track.request_frame()
        elif connection_state in {"disconnected", "failed", "closed"}:
            if self.unhealthy_since is None:
                self.unhealthy_since = now

    # -- media ---------------------------------------------------------
    def next_frame(self) -> tuple[Any, float] | None:
        """Return the newest encoded frame for the WebRTC encoder.

        A frame is produced by :meth:`encode_once`, which also measures the
        real capture and encode cost that drives the adaptive controller.
        """
        image = self._latest_image
        if image is None:
            return None
        return image, max(0.0, time.monotonic() - self._latest_at)

    def encode_once(self) -> bool:
        """Prepare one raw frame for aiortc; runs off the event loop.

        aiortc owns the VP8/H.264 encoder.  Pre-encoding JPEG here would produce
        bytes that are never sent and would make every frame pay for two codecs.
        """
        sample = SHARED_SCREEN.read()
        if sample is None:
            return False
        image, age, sequence = sample
        if age > 2.0 or sequence == self._last_sequence:
            return False
        self._last_sequence = sequence
        started = time.perf_counter()
        width = max(320, int(self.controller.width))
        if image.width > width:
            height = max(1, int(image.height * (width / image.width)))
            image = image.resize((width, height), Image.Resampling.BILINEAR)
        signature = frame_signature(image, current_config)
        changed = signature is None or signature != self._last_signature
        if signature is not None:
            self._last_signature = signature
        self._last_changed = changed
        self._last_encode_seconds = time.perf_counter() - started
        self.last_encoded_at = time.monotonic()
        self._frames += 1
        self.last_frame_at = self.last_encoded_at
        self._latest_image = image
        self._latest_at = self.last_encoded_at
        if self.video_track is not None:
            self.video_track.request_frame()
        return True

    async def video_pipeline(self) -> None:
        """Prepare frames off-loop and pace the aiortc encoder input."""
        pacer = FramePacer(float(CONFIG["webrtc_fps"]))
        while not self.closed:
            pacer.set_fps(max(1.0, self.controller.fps))
            try:
                produced = await asyncio.to_thread(self.encode_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                produced = False
                await asyncio.sleep(0.5)
                pacer.reset()
                continue
            if produced:
                self.controller.observe(
                    capture_seconds=0.0,
                    encode_seconds=self._last_encode_seconds,
                    payload_bytes=self.last_encoded_bytes,
                    interval=max(1e-3, 1.0 / max(1.0, self.controller.fps)),
                    changed=self._last_changed,
                    queue_ratio=self._channel_pressure,
                    frame_bytes=max(1, int(self.controller.width) * int(self.controller.width) * 9 // 16 * 3),
                )
                SHARED_SCREEN.set_fps(
                    max(float(CONFIG["webrtc_min_fps"]), self.controller.fps)
                )
                if self.video_track is not None:
                    self.video_track.set_fps(self.controller.fps)
            await pacer.wait()

    @property
    def _channel_pressure(self) -> float:
        channel = self.control_channel
        if channel is None:
            return 0.0
        try:
            buffered = float(getattr(channel, "bufferedAmount", 0) or 0)
        except Exception:
            buffered = 0.0
        return min(1.0, buffered / max(1.0, float(CONFIG["input_reliable_bytes_limit"])))

    async def supervise(self) -> None:
        """Adapt the encoder bitrate and keep the peer honest about liveness."""
        interval = 1.0
        while not self.closed:
            await asyncio.sleep(interval)
            pc = self.pc
            if pc is None:
                return
            now = time.monotonic()
            connection_state = str(getattr(pc, "connectionState", self.connection_state))
            self.connection_state = connection_state
            if connection_state in {"disconnected", "failed", "closed"}:
                if self.unhealthy_since is None:
                    self.unhealthy_since = now
            elif connection_state == "connected":
                self.unhealthy_since = None
            if peer_connection_expired(
                connection_state,
                now=now,
                unhealthy_since=self.unhealthy_since,
                last_seen=self.last_seen_at,
                grace_seconds=float(CONFIG["webrtc_grace_seconds"]),
                recover_seconds=float(CONFIG["webrtc_recover_seconds"]),
                idle_seconds=float(CONFIG["peer_idle_timeout_seconds"]),
            ):
                PEER_SESSIONS.pop(self.peer_id, None)
                await self.shutdown()
                return
            if SESSION_STORE.get(self.token, touch=False) is None:
                PEER_SESSIONS.pop(self.peer_id, None)
                await self.shutdown()
                return
            try:
                stats = await collect_peer_stats(pc)
            except Exception:
                self._send_failures += 1
                stats = {}
            if self.video_track is not None:
                self.video_track.note_outbound(stats)
            bytes_sent = int(stats.get("bytesSent", 0) or 0)
            delta_bytes = max(0, bytes_sent - self._last_stats_bytes)
            delta_frames = max(0, self._frames - self._last_stats_frames)
            if delta_frames:
                self.last_encoded_bytes = max(1, delta_bytes // delta_frames)
            self._last_stats_bytes = max(self._last_stats_bytes, bytes_sent)
            self._last_stats_frames = self._frames
            self._bytes = max(self._bytes, bytes_sent)
            available = int(stats.get("availableOutgoingBitrate", 0) or 0)
            loss = float(stats.get("fractionLost", 0.0) or 0.0)
            rtt = float(stats.get("roundTripTime", 0.0) or 0.0)
            target = int(self.target_bitrate)
            if available:
                target = min(
                    int(CONFIG["webrtc_bitrate_max"]),
                    max(int(CONFIG["webrtc_bitrate_min"]), int(available * 0.85)),
                )
            if loss > 0.08:
                target = max(int(CONFIG["webrtc_bitrate_min"]), int(target * 0.7))
            elif loss < 0.01 and rtt < 0.25:
                target = min(int(CONFIG["webrtc_bitrate_max"]), int(target * 1.12) + 50_000)
            self.target_bitrate = target
            if target != self.applied_bitrate:
                if await apply_sender_bitrate(pc, {"video": target}):
                    self.applied_bitrate = target
            pacer_fps = self.video_track.stats["fps"] if self.video_track is not None else 0
            self.state["peer_stats"] = {
                "fps": round(pacer_fps, 2),
                "quality": int(self.controller.quality),
                "width": int(self.controller.width),
                "bitrate": int(target),
                "bitrateCapActive": self.applied_bitrate == target,
                "loss": round(loss, 4),
                "rtt": round(rtt, 4),
                "encodeMs": round(self._last_encode_seconds * 1000, 1),
                "frameBytes": int(self.last_encoded_bytes),
                "iceRestarts": self.ice_restarts,
                "recoveries": self.recoveries,
                "frames": self._frames,
                "bytes": self._bytes,
                "sendFailures": self._send_failures,
                "connectionState": connection_state,
            }


async def _create_peer_session(
    token: str,
    payload: dict[str, Any],
    existing: PeerSession | None = None,
) -> web.Response:
    state = session_state(token)
    if payload["viewportWidth"]:
        state["viewport_width"] = payload["viewportWidth"]
    if payload["viewportHeight"]:
        state["viewport_height"] = payload["viewportHeight"]
    if payload["monitor"]:
        state["monitor"] = payload["monitor"]
    inp = input_state(token)
    peer_id = payload["peerId"] or secrets.token_urlsafe(18)

    if existing is not None and not existing.closed:
        try:
            answer = await existing.renegotiate(payload["sdp"], payload["type"])
            existing.recoveries += 1
            return web.json_response({**answer, "peerId": existing.peer_id, "reused": True})
        except Exception:
            await existing.shutdown()
            PEER_SESSIONS.pop(existing.peer_id, None)

    if not SESSION_STORE.acquire_connection(token):
        raise web.HTTPTooManyRequests(text="session connection limit reached")
    session = PeerSession(peer_id, token, state, inp, int(payload["monitor"] or 1))
    try:
        answer = await session.start(payload["sdp"], payload["type"])
    except Exception as exc:
        await session.shutdown()
        message = f"{type(exc).__name__}: {exc}"
        # A malformed offer is the client's problem (400); anything else is a
        # host-side media failure (503).
        if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)) or "sdp" in message.lower():
            raise web.HTTPBadRequest(text=f"the WebRTC offer was rejected: {message}") from exc
        raise web.HTTPServiceUnavailable(text=f"WebRTC negotiation failed: {message}") from exc
    PEER_SESSIONS[peer_id] = session
    return web.json_response(
        {
            "sdp": answer["sdp"],
            "type": answer["type"],
            "audio": answer["audio"],
            "audioDevice": answer["audioDevice"],
            "peerId": peer_id,
            "monitor": int(state.get("monitor", 1)),
        }
    )


async def webrtc_offer(request: web.Request) -> web.Response:
    token = require_session(request)
    if not CONFIG.get("webrtc_enabled") or not WEBRTC_AVAILABLE:
        raise web.HTTPServiceUnavailable(text="WebRTC is unavailable")
    try:
        payload = validate_webrtc_offer(await request.json())
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid WebRTC offer") from exc
    existing = PEER_SESSIONS.get(payload["peerId"]) if payload["peerId"] else None
    if existing is not None and existing.token != token:
        existing = None
    return await _create_peer_session(token, payload, existing)


async def webrtc_ice_restart(request: web.Request) -> web.Response:
    """Server-initiated ICE restart.

    aiortc cannot renew ICE credentials by itself, so when the in-place restart
    is not possible the client is told to run a full recovery negotiation
    (``409`` with ``retry: recover``) instead of being left with a dead peer.
    """
    token = require_session(request)
    try:
        data = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid restart request") from exc
    peer_id = str((data or {}).get("peerId", ""))
    session = PEER_SESSIONS.get(peer_id)
    if session is None or session.token != token:
        raise web.HTTPNotFound(text="unknown peer")
    try:
        answer = await session.restart_ice()
    except Exception as exc:
        return web.json_response(
            {
                "restart": False,
                "retry": "recover",
                "peerId": peer_id,
                "message": f"in-place ICE restart is unavailable: {exc}",
            },
            status=409,
        )
    return web.json_response({**answer, "peerId": peer_id, "restart": True})


async def webrtc_recover(request: web.Request) -> web.Response:
    """Fresh negotiation after the browser lost its peer (page reload, sleep)."""
    token = require_session(request)
    if not CONFIG.get("webrtc_enabled") or not WEBRTC_AVAILABLE:
        raise web.HTTPServiceUnavailable(text="WebRTC is unavailable")
    try:
        payload = validate_webrtc_offer(await request.json())
    except (json.JSONDecodeError, ValidationError, web.HTTPBadRequest) as exc:
        raise web.HTTPBadRequest(text="invalid WebRTC offer") from exc
    existing = PEER_SESSIONS.get(payload["peerId"]) if payload["peerId"] else None
    if existing is not None and existing.token != token:
        existing = None
    return await _create_peer_session(token, payload, existing)


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------
@web.middleware
async def security_middleware(request: web.Request, handler):
    try:
        needs_origin = request.method not in {"GET", "HEAD", "OPTIONS"} or request.path.startswith("/ws")
        try:
            host = request.host
        except ValueError as exc:
            raise web.HTTPBadRequest(text="invalid host") from exc
        if needs_origin:
            allowed = CONFIG.get("allowed_origins", [])
            if not isinstance(allowed, list):
                allowed = []
            if not origin_matches(
                request.headers.get("Origin"), request_scheme(request), host, [str(item) for item in allowed]
            ):
                raise web.HTTPForbidden(text="origin rejected")
        allowed_hosts = CONFIG.get("allowed_hosts", [])
        if not isinstance(allowed_hosts, list) or not host_allowed(host, [str(item) for item in allowed_hosts]):
            raise web.HTTPBadRequest(text="host rejected")
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "img-src 'self' blob: data:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self' ws: wss:"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def start_background(app: web.Application) -> None:
    if WAYLAND_PORTAL is None:
        try:
            await asyncio.to_thread(CAPTURE.monitors)
        except Exception:
            pass


async def close_peer_connections(app: web.Application) -> None:
    entries = list(PEER_SESSIONS.values())
    PEER_SESSIONS.clear()
    if entries:
        await asyncio.gather(*(entry.shutdown() for entry in entries), return_exceptions=True)
    await SHARED_SCREEN.stop()
    await asyncio.to_thread(CAPTURE.close)
    await TRANSFERS.close()
    if WAYLAND_PORTAL is not None:
        await WAYLAND_PORTAL.close()


def build_app() -> web.Application:
    app = web.Application(
        middlewares=[security_middleware],
        client_max_size=max(1_200_000, int(CONFIG["file_chunk_size"]) + 65_536),
    )
    app.router.add_get("/", index)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/clipboard", clipboard_get)
    app.router.add_post("/api/clipboard", clipboard_set)
    app.router.add_post("/api/platform/authorize", authorize_platform)
    app.router.add_post("/api/webrtc/offer", webrtc_offer)
    app.router.add_post("/api/webrtc/restart", webrtc_ice_restart)
    app.router.add_post("/api/webrtc/recover", webrtc_recover)
    app.router.add_post("/api/files/upload", upload_begin)
    app.router.add_post("/api/files/upload/status", upload_status)
    app.router.add_put("/api/files/upload/{transfer_id}", upload_chunk)
    app.router.add_post("/api/files/upload/{transfer_id}/finish", upload_finish)
    app.router.add_delete("/api/files/upload/{transfer_id}", upload_cancel)
    app.router.add_get("/api/files/list", files_list)
    app.router.add_get("/api/files/download", files_download)
    app.router.add_delete("/api/files/delete", files_delete)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/ws/input", input_websocket)
    app.router.add_get("/ws/audio", audio_websocket)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(start_background)
    app.on_cleanup.append(close_peer_connections)
    return app


def main() -> None:
    bind = str(os.environ.get("TRD_BIND", CONFIG["bind"])).strip()
    try:
        port = int(os.environ.get("TRD_PORT", CONFIG["port"]))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("TRD_PORT must be an integer from 1 through 65535") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("TRD_PORT must be an integer from 1 through 65535")
    if not is_loopback_address(bind) and os.environ.get("TRD_ALLOW_INSECURE_LAN") != "1":
        raise RuntimeError(
            "Plain HTTP on a non-loopback address is disabled. Use the HTTPS tunnel, or set "
            "TRD_ALLOW_INSECURE_LAN=1 only for a trusted local network."
        )
    if GENERATED_PASSWORD:
        print()
        print("Local remote desktop password (shown once):")
        print(GENERATED_PASSWORD)
        print(f"Configuration: {CONFIG_PATH}")
        print()
    print(f"Tablet Remote Desktop listening on http://{bind}:{port}")
    if CONFIG.get("turn_urls"):
        credentials = turn_credentials(CONFIG)
        print(
            "TURN relay configured: "
            + ", ".join(str(url) for url in CONFIG["turn_urls"])
            + (" (ephemeral credentials)" if credentials else " (no credentials)")
        )
    else:
        print("TURN relay not configured; STUN plus the WebSocket fallback are used.")
    if not is_loopback_address(bind):
        print("WARNING: explicitly enabled non-loopback HTTP; traffic is not encrypted.")
    print("Press Ctrl+C or close this window to stop remote control.")
    web.run_app(build_app(), host=bind, port=port)


if __name__ == "__main__":
    main()
