from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from aiohttp import web
from PIL import Image

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import CONFIG_PATH, hash_password, load_config
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
)
from app.webrtc import WEBRTC_AVAILABLE, create_peer_answer, parse_control_message
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
PEER_CONNECTIONS: dict[str, dict[str, Any]] = {}


def forget_session_state(token: str) -> None:
    CONNECTION_STATES.pop(token, None)


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
_CAPTURE_LOCAL = threading.local()


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


def connection_input_state(display_state: dict) -> dict:
    return {
        "last_interaction_at": 0.0,
        "viewport_width": int(display_state.get("viewport_width", 0) or 0),
        "touch_id_map": {},
        "touch_points": {},
        "buttons_down": set(),
    }


def session_state(token: str) -> dict:
    return CONNECTION_STATES.setdefault(
        token,
        {
            "last_interaction_at": 0.0,
            "viewport_width": 0,
            "touch_id_map": {},
            "touch_points": {},
            "buttons_down": set(),
        },
    )


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


def frame_width(state: dict, configured_width: int) -> int:
    configured_width = max(640, int(configured_width))
    viewport_width = int(state.get("viewport_width", 0) or 0)
    if viewport_width <= 0:
        return configured_width
    return max(640, min(configured_width, viewport_width))


def interactive_active(state: dict) -> bool:
    age = interaction_age(state)
    if age is None:
        return False
    return age < float(CONFIG.get("preview_seconds", CONFIG.get("interactive_seconds", 0)))


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
        raise web.HTTPTooManyRequests(text="password verification queue is full", headers={"Retry-After": "1"})
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
        raise web.HTTPTooManyRequests(text="all session slots are active", headers={"Retry-After": "15"}) from exc
    max_age = int(CONFIG["session_idle_ttl_seconds"])
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
        websocket=True,
        clipboard=True,
        fileTransfer=False,
        multiMonitor=False,
    )
    return web.json_response(capabilities)


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


def pack_binary_message(meta: dict, payload: bytes) -> bytes:
    header = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return len(header).to_bytes(4, "big") + header + payload


def screen_capture() -> mss.mss:
    sct = getattr(_CAPTURE_LOCAL, "sct", None)
    if sct is None:
        sct = mss.mss()
        _CAPTURE_LOCAL.sct = sct
    return sct


def capture_webrtc_image(state: dict) -> Image.Image:
    if WAYLAND_PORTAL is not None:
        image = WAYLAND_PORTAL.capture_image()
        monitor = WAYLAND_PORTAL.monitor()
    else:
        with mss.mss() as capture:
            monitor = dict(capture.monitors[1])
            raw = capture.grab(monitor)
        image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    state["monitor"] = monitor
    target_width = frame_width(state, int(CONFIG.get("active_max_width", 1280)))
    if image.width > target_width:
        target_height = max(1, int(image.height * (target_width / image.width)))
        image = image.resize((target_width, target_height), Image.Resampling.BILINEAR)
    return image


def frame_profile(state: dict) -> dict:
    age = interaction_age(state)
    if age is not None and age < float(CONFIG.get("active_seconds", 0)):
        return {
            "name": "active",
            "fps": int(CONFIG.get("active_fps", CONFIG.get("interactive_fps", CONFIG["fps"]))),
            "quality": int(
                CONFIG.get(
                    "active_jpeg_quality",
                    CONFIG.get("interactive_jpeg_quality", CONFIG["jpeg_quality"]),
                )
            ),
                "max_width": frame_width(
                    state,
                    CONFIG.get(
                        "active_max_width",
                        CONFIG.get("interactive_max_width", CONFIG["max_width"]),
                )
            ),
        }
    if age is not None and age < float(CONFIG.get("settle_seconds", 0)):
        return {
            "name": "settle",
            "fps": int(CONFIG.get("settle_fps", CONFIG.get("interactive_fps", CONFIG["fps"]))),
            "quality": int(
                CONFIG.get(
                    "settle_jpeg_quality",
                    CONFIG.get("interactive_jpeg_quality", CONFIG["jpeg_quality"]),
                )
            ),
                "max_width": frame_width(
                    state,
                    CONFIG.get(
                        "settle_max_width",
                        CONFIG.get("interactive_max_width", CONFIG["max_width"]),
                )
            ),
        }
    if age is not None and age < float(CONFIG.get("preview_seconds", 0)):
        return {
            "name": "preview",
            "fps": int(CONFIG.get("preview_fps", CONFIG.get("interactive_fps", CONFIG["fps"]))),
            "quality": int(
                CONFIG.get(
                    "preview_jpeg_quality",
                    CONFIG.get("interactive_jpeg_quality", CONFIG["jpeg_quality"]),
                )
            ),
                "max_width": frame_width(
                    state,
                    CONFIG.get(
                        "preview_max_width",
                        CONFIG.get("interactive_max_width", CONFIG["max_width"]),
                )
            ),
        }
    return {
        "name": "full",
        "fps": int(CONFIG["fps"]),
        "quality": int(CONFIG["jpeg_quality"]),
        "max_width": frame_width(state, CONFIG["max_width"]),
    }


def frame_signature(img: Image.Image) -> str | None:
    if not CONFIG.get("change_detection_enabled", True):
        return None
    width = max(16, int(CONFIG.get("change_thumbnail_width", 96)))
    height = max(10, int(CONFIG.get("change_thumbnail_height", 60)))
    thumb = img.resize((width, height), Image.Resampling.BILINEAR).convert("L")
    return hashlib.blake2b(thumb.tobytes(), digest_size=8).hexdigest()


def unchanged_frame_interval(profile_name: str) -> float:
    if profile_name == "preview":
        return float(CONFIG.get("unchanged_preview_interval", 0))
    if profile_name == "full":
        return float(CONFIG.get("unchanged_full_interval", 0))
    return 0.0


def encode_frame(monitor: dict, profile: dict) -> tuple[dict, bytes]:
    if WAYLAND_PORTAL is not None:
        img = WAYLAND_PORTAL.capture_image()
        monitor = WAYLAND_PORTAL.monitor()
    else:
        raw = screen_capture().grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    max_width = int(profile["max_width"])
    if img.width > max_width:
        height = int(img.height * (max_width / img.width))
        img = img.resize((max_width, height), Image.Resampling.BILINEAR)
    signature = frame_signature(img)
    buf = io.BytesIO()
    image_format = str(CONFIG.get("image_format", "jpeg")).lower()
    if image_format == "webp":
        img.save(buf, format="WEBP", quality=int(profile["quality"]), method=0)
        payload_format = "webp"
    else:
        img.save(buf, format="JPEG", quality=int(profile["quality"]))
        payload_format = "jpeg"
    meta = {
        "type": "frame",
        "width": img.width,
        "height": img.height,
        "screenWidth": monitor["width"],
        "screenHeight": monitor["height"],
        "left": monitor["left"],
        "top": monitor["top"],
        "format": payload_format,
        "profile": profile["name"],
    }
    if signature:
        meta["signature"] = signature
    return meta, buf.getvalue()


async def screen_loop(ws: web.WebSocketResponse, state: dict) -> None:
    if WAYLAND_PORTAL is not None:
        if not WAYLAND_PORTAL.active:
            await ws.send_json({"type": "error", "message": "Wayland permission is required"})
            return
        monitor = WAYLAND_PORTAL.monitor()
    else:
        with mss.mss() as sct:
            monitor = dict(sct.monitors[1])
    state["monitor"] = monitor
    last_signature = None
    last_sent_at = 0.0
    while not ws.closed:
        profile = frame_profile(state)
        interval = 1 / max(1, int(profile["fps"]))
        try:
            meta, payload = await asyncio.to_thread(encode_frame, monitor, profile)
            if profile["name"] == "full" and interactive_active(state):
                await asyncio.sleep(interval)
                continue
            signature = meta.get("signature")
            unchanged_interval = unchanged_frame_interval(profile["name"])
            now = time.monotonic()
            if (
                signature
                and signature == last_signature
                and unchanged_interval > 0
                and now - last_sent_at < unchanged_interval
            ):
                await asyncio.sleep(interval)
                continue
            await asyncio.wait_for(
                ws.send_bytes(pack_binary_message(meta, payload)),
                timeout=float(CONFIG.get("frame_send_timeout_seconds", 2.5)),
            )
            if signature:
                last_signature = signature
            last_sent_at = now
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
                await ws.send_json({"type": "error", "message": "screen capture unavailable"})
            except ConnectionError:
                break
            await asyncio.sleep(1)
            continue
        await asyncio.sleep(interval)


def to_screen_point(state: dict, nx: float, ny: float) -> tuple[int, int]:
    monitor = state.get("monitor")
    if not monitor:
        with mss.mss() as sct:
            monitor = sct.monitors[1]
    x = int(monitor["left"] + max(0, min(1, nx)) * monitor["width"])
    y = int(monitor["top"] + max(0, min(1, ny)) * monitor["height"])
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

    buttons_down = state.get("buttons_down", set())
    for button in tuple(buttons_down):
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            pass
    buttons_down.clear()


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


KEY_MAP = {
    "Enter": "enter",
    "Backspace": "backspace",
    "Tab": "tab",
    "Escape": "esc",
    "Delete": "delete",
    "Home": "home",
    "End": "end",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "ArrowUp": "up",
    "ArrowDown": "down",
    " ": "space",
}


def handle_input_event(state: dict, event: dict) -> None:
    mark_interaction(state)
    kind = event.get("kind")
    if kind == "touch":
        inject_touch_contacts(state, list(event.get("contacts", [])))
    elif kind in {"move", "down", "up", "click", "double"}:
        x, y = to_screen_point(state, float(event.get("x", 0)), float(event.get("y", 0)))
        pyautogui.moveTo(x, y)
        button = {1: "middle", 2: "right"}.get(event.get("button"), "left")
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
    elif kind == "mouse_button":
        button = {1: "middle", 2: "right"}.get(event.get("button"), "left")
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
        key = str(event.get("key", ""))
        mapped = KEY_MAP.get(key, key.lower())
        modifiers = []
        if event.get("ctrl"):
            modifiers.append("ctrl")
        if event.get("meta"):
            modifiers.append("command" if sys.platform == "darwin" else "win")
        if event.get("alt"):
            modifiers.append("alt")
        if event.get("shift"):
            modifiers.append("shift")
        if len(key) == 1 and not modifiers:
            pyautogui.write(key)
        elif modifiers:
            pyautogui.hotkey(*modifiers, mapped)
        elif mapped:
            pyautogui.press(mapped)
    elif kind == "text":
        send_unicode_text(str(event.get("text", "")))


async def clipboard_poll_loop(ws: web.WebSocketResponse) -> None:
    last = None
    while not ws.closed:
        try:
            text = await asyncio.to_thread(pyperclip.paste)
            if text != last:
                last = text
                await ws.send_json({"type": "clipboard", "text": text})
        except Exception:
            pass
        await asyncio.sleep(2)


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
                await ws.send_json({"type": "text_focus", "active": active})
        except Exception:
            pass
        await asyncio.sleep(0.12)


async def audio_loop(ws: web.WebSocketResponse) -> None:
    if not CONFIG.get("audio_enabled"):
        return
    try:
        import numpy as np
        import soundcard as sc
    except Exception as exc:
        await ws.send_json({"type": "audio_status", "ok": False, "message": "system audio unavailable"})
        return

    try:
        microphones = sc.all_microphones(include_loopback=True)
        loopbacks = [
            mic
            for mic in microphones
            if "loopback" in repr(mic).lower() or "loopback" in mic.name.lower()
        ]
        if not loopbacks:
            raise RuntimeError("No system-audio loopback device is available")
        mic = loopbacks[0]
        sample_rate = int(CONFIG["audio_sample_rate"])
        frames = int(CONFIG["audio_frames"])
        await ws.send_json(
            {
                "type": "audio_status",
                "ok": True,
                "sampleRate": sample_rate,
                "channels": 2,
                "device": mic.name,
            }
        )
        with mic.recorder(samplerate=sample_rate, channels=2) as recorder:
            while not ws.closed:
                data = await asyncio.to_thread(recorder.record, frames)
                pcm = np.clip(data, -1.0, 1.0)
                pcm = (pcm * 32767).astype(np.int16)
                await asyncio.wait_for(
                    ws.send_bytes(
                        pack_binary_message(
                            {"type": "audio", "sampleRate": sample_rate, "channels": 2},
                            pcm.tobytes(),
                        )
                    ),
                    timeout=float(CONFIG.get("frame_send_timeout_seconds", 2.5)),
                )
    except Exception as exc:
        await ws.send_json({"type": "audio_status", "ok": False, "message": "system audio unavailable"})


def consume_event_budget(state: dict, rate: float = 300.0, burst: float = 600.0) -> bool:
    now = time.monotonic()
    bucket = state.setdefault("event_budget", {"tokens": burst, "at": now})
    elapsed = max(0.0, now - float(bucket["at"]))
    bucket["tokens"] = min(burst, float(bucket["tokens"]) + elapsed * rate)
    bucket["at"] = now
    if bucket["tokens"] < 1:
        return False
    bucket["tokens"] -= 1
    return True


async def _handle_validated_message(state: dict, event: dict, display_state: dict | None = None) -> None:
    if event["type"] == "client_info":
        target = display_state if display_state is not None else state
        target["viewport_width"] = max(640, int(event["viewportWidth"] or 640))
    elif event["type"] == "input":
        if not consume_event_budget(state):
            raise ValidationError("input rate exceeded")
        async with INPUT_LOCK:
            if WAYLAND_PORTAL is not None:
                mark_interaction(state)
                await WAYLAND_PORTAL.handle_input(event)
            else:
                await asyncio.to_thread(handle_input_event, state, event)
    elif event["type"] == "clipboard_set":
        await asyncio.to_thread(pyperclip.copy, event["text"])


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
    peers = [
        (peer_id, entry) for peer_id, entry in list(PEER_CONNECTIONS.items())
        if entry.get("token") == token
    ]
    for peer_id, entry in peers:
        PEER_CONNECTIONS.pop(peer_id, None)
        tasks = entry.get("tasks", [])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await release_connection_input(entry["input_state"])
        await entry["pc"].close()


async def release_connection_input(state: dict) -> None:
    if WAYLAND_PORTAL is not None:
        await WAYLAND_PORTAL.release_input()
    else:
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
        input_state = connection_input_state(state)
        tasks = [
            asyncio.create_task(screen_loop(ws, state)),
            asyncio.create_task(text_focus_loop(ws)),
            asyncio.create_task(session_watchdog(token, ws)),
        ]
        await ws.send_json(
            {
                "type": "hello",
                "message": "connected",
                "protocolVersion": 2,
                "mediaTransports": ["webrtc", "websocket"] if WEBRTC_AVAILABLE else ["websocket"],
            }
        )
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    event = validate_client_message(json.loads(message.data))
                    if SESSION_STORE.get(token) is None:
                        await ws.close(code=1008, message=b"session expired")
                        break
                    await _handle_validated_message(input_state, event, state)
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                    await ws.close(code=1008, message=b"invalid control message")
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await release_connection_input(input_state)
        return ws
    finally:
        unregister_websocket(token, ws)
        SESSION_STORE.release_connection(token)


async def input_websocket(request: web.Request) -> web.WebSocketResponse:
    token = require_session(request)
    if not SESSION_STORE.acquire_connection(token):
        raise web.HTTPTooManyRequests(text="session connection limit reached")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1_100_000, compress=False)
    try:
        await ws.prepare(request)
        SESSION_WEBSOCKETS[token].add(ws)
        state = session_state(token)
        input_state = connection_input_state(state)
        tasks = [
            asyncio.create_task(session_watchdog(token, ws)),
            asyncio.create_task(text_focus_loop(ws)),
        ]
        await ws.send_json({"type": "hello", "message": "input connected"})
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                try:
                    event = validate_client_message(json.loads(message.data))
                    if SESSION_STORE.get(token) is None:
                        await ws.close(code=1008, message=b"session expired")
                        break
                    await _handle_validated_message(input_state, event, state)
                    if event["type"] == "input" and event.get("ack"):
                        await ws.send_json({"type": "ack", "profile": frame_profile(state)["name"]})
                except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                    await ws.close(code=1008, message=b"invalid control message")
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await release_connection_input(input_state)
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


async def webrtc_offer(request: web.Request) -> web.Response:
    token = require_session(request)
    if not CONFIG.get("webrtc_enabled") or not WEBRTC_AVAILABLE:
        raise web.HTTPServiceUnavailable(text="WebRTC is unavailable")
    if not SESSION_STORE.acquire_connection(token):
        raise web.HTTPTooManyRequests(text="session connection limit reached")
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValidationError("offer must be an object")
        sdp = data.get("sdp")
        offer_type = data.get("type")
        viewport_width = data.get("viewportWidth", 1280)
        if not isinstance(sdp, str) or not 1 <= len(sdp) <= 200_000 or offer_type != "offer":
            raise ValidationError("invalid WebRTC offer")
        if isinstance(viewport_width, bool):
            raise ValidationError("invalid viewport width")
        viewport_width = max(640, min(10000, int(viewport_width)))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError, web.HTTPBadRequest) as exc:
        SESSION_STORE.release_connection(token)
        raise web.HTTPBadRequest(text="invalid WebRTC offer") from exc

    state = session_state(token)
    state["viewport_width"] = viewport_width
    input_state = connection_input_state(state)
    peer_id = secrets.token_urlsafe(18)
    control_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
    released = False
    input_released = False

    async def release_peer_input() -> None:
        nonlocal input_released
        if input_released:
            return
        input_released = True
        await release_connection_input(input_state)

    async def control_worker() -> None:
        while True:
            raw = await control_queue.get()
            if SESSION_STORE.get(token) is None:
                return
            try:
                event = validate_client_message(parse_control_message(raw))
                await _handle_validated_message(input_state, event, state)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
                continue

    worker = asyncio.create_task(control_worker())

    def on_control(raw: str) -> None:
        if not isinstance(raw, str) or len(raw) > 65_536:
            return
        try:
            control_queue.put_nowait(raw)
        except asyncio.QueueFull:
            return

    def on_state_change(connection_state: str) -> None:
        nonlocal released
        if connection_state in {"failed", "closed"} and not released:
            released = True
            SESSION_STORE.release_connection(token)
            entry = PEER_CONNECTIONS.pop(peer_id, None)
            worker.cancel()
            asyncio.create_task(release_peer_input())

    try:
        pc, answer = await create_peer_answer(
            offer_sdp=sdp,
            offer_type=offer_type,
            capture=lambda: capture_webrtc_image(state),
            on_control_message=on_control,
            on_state_change=on_state_change,
            stun_urls=[str(item) for item in CONFIG.get("stun_urls", [])],
            fps=int(CONFIG.get("active_fps", 15)),
            audio_enabled=bool(CONFIG.get("audio_enabled")),
            audio_sample_rate=int(CONFIG.get("audio_sample_rate", 24000)),
            audio_frames=int(CONFIG.get("audio_frames", 1024)),
        )
    except Exception as exc:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if not released:
            SESSION_STORE.release_connection(token)
        await release_peer_input()
        raise web.HTTPServiceUnavailable(text="WebRTC negotiation failed") from exc

    async def peer_watchdog() -> None:
        while pc.connectionState not in {"failed", "closed"}:
            await asyncio.sleep(15)
            if SESSION_STORE.get(token, touch=False) is None:
                await pc.close()
                return

    watchdog = asyncio.create_task(peer_watchdog())
    PEER_CONNECTIONS[peer_id] = {
        "pc": pc,
        "token": token,
        "tasks": [worker, watchdog],
        "input_state": input_state,
    }
    return web.json_response({**answer, "peerId": peer_id})


@web.middleware
async def security_middleware(request: web.Request, handler):
    try:
        needs_origin = request.method not in {"GET", "HEAD", "OPTIONS"} or request.path.startswith("/ws")
        if needs_origin:
            try:
                host = request.host
            except ValueError as exc:
                raise web.HTTPBadRequest(text="invalid host") from exc
            allowed = CONFIG.get("allowed_origins", [])
            if not isinstance(allowed, list):
                allowed = []
            if not origin_matches(
                request.headers.get("Origin"), request_scheme(request), host, [str(item) for item in allowed]
            ):
                raise web.HTTPForbidden(text="origin rejected")
        try:
            host = request.host
        except ValueError as exc:
            raise web.HTTPBadRequest(text="invalid host") from exc
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


async def close_peer_connections(app: web.Application) -> None:
    entries = list(PEER_CONNECTIONS.values())
    PEER_CONNECTIONS.clear()
    tasks = []
    for entry in entries:
        for task in entry.get("tasks", []):
            task.cancel()
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if entries:
        await asyncio.gather(
            *(release_connection_input(entry["input_state"]) for entry in entries),
            return_exceptions=True,
        )
        await asyncio.gather(*(entry["pc"].close() for entry in entries), return_exceptions=True)
    if WAYLAND_PORTAL is not None:
        await WAYLAND_PORTAL.close()


def build_app() -> web.Application:
    app = web.Application(middlewares=[security_middleware], client_max_size=1_200_000)
    app.router.add_get("/", index)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/clipboard", clipboard_get)
    app.router.add_post("/api/clipboard", clipboard_set)
    app.router.add_post("/api/platform/authorize", authorize_platform)
    app.router.add_post("/api/webrtc/offer", webrtc_offer)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/ws/input", input_websocket)
    app.router.add_get("/ws/audio", audio_websocket)
    app.router.add_static("/static", STATIC_DIR)
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
    if not is_loopback_address(bind):
        print("WARNING: explicitly enabled non-loopback HTTP; traffic is not encrypted.")
    print("Press Ctrl+C or close this window to stop remote control.")
    web.run_app(build_app(), host=bind, port=port)


if __name__ == "__main__":
    main()
