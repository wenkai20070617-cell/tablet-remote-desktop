import asyncio
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
from pathlib import Path

from aiohttp import web
from PIL import Image

try:
    import mss
    import pyautogui
    import pyperclip
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "Missing desktop control dependencies. Run start.ps1 to install requirements."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "app" / "static"
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 18765,
    "fps": 1,
    "image_format": "jpeg",
    "jpeg_quality": 58,
    "max_width": 2560,
    "active_fps": 15,
    "active_jpeg_quality": 40,
    "active_max_width": 1280,
    "active_seconds": 1.2,
    "settle_fps": 8,
    "settle_jpeg_quality": 48,
    "settle_max_width": 1600,
    "settle_seconds": 3.5,
    "preview_fps": 3,
    "preview_jpeg_quality": 52,
    "preview_max_width": 1920,
    "preview_seconds": 8.0,
    "change_detection_enabled": True,
    "change_thumbnail_width": 96,
    "change_thumbnail_height": 60,
    "unchanged_preview_interval": 2.0,
    "unchanged_full_interval": 6.0,
    "frame_send_timeout_seconds": 2.5,
    "session_ttl_seconds": 3600,
    "audio_enabled": True,
    "audio_sample_rate": 24000,
    "audio_frames": 1024,
}

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 250_000
    )
    return digest.hex()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        return cfg

    password = secrets.token_urlsafe(14)
    salt = secrets.token_hex(16)
    cfg.update(
        {
            "password_salt": salt,
            "password_hash": _hash_password(password, salt),
        }
    )
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print("")
    print("Local remote desktop password:")
    print(password)
    print("")
    print("This password is shown only on first config creation.")
    print("")
    return cfg


CONFIG = load_config()
SESSIONS: dict[str, float] = {}
CONNECTION_STATES: dict[str, dict] = {}
GLOBAL_INTERACTION_STATE = {"last_interaction_at": 0.0}
_CAPTURE_LOCAL = threading.local()


def verify_password(password: str) -> bool:
    expected = CONFIG["password_hash"]
    actual = _hash_password(password, CONFIG["password_salt"])
    return hmac.compare_digest(expected, actual)


def make_session() -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + int(CONFIG["session_ttl_seconds"])
    return token


def get_session(request: web.Request) -> str | None:
    token = request.cookies.get("rd_session")
    if not token:
        return None
    expires = SESSIONS.get(token)
    if not expires or expires < time.time():
        SESSIONS.pop(token, None)
        CONNECTION_STATES.pop(token, None)
        return None
    SESSIONS[token] = time.time() + int(CONFIG["session_ttl_seconds"])
    return token


def require_session(request: web.Request) -> str:
    token = get_session(request)
    if not token:
        raise web.HTTPUnauthorized()
    return token


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
    data = await request.json()
    if not verify_password(str(data.get("password", ""))):
        await asyncio.sleep(0.3)
        raise web.HTTPUnauthorized(text="bad password")

    token = make_session()
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        "rd_session",
        token,
        httponly=True,
        samesite="Strict",
        max_age=int(CONFIG["session_ttl_seconds"]),
    )
    return resp


async def logout(request: web.Request) -> web.Response:
    token = request.cookies.get("rd_session")
    if token:
        SESSIONS.pop(token, None)
        CONNECTION_STATES.pop(token, None)
    resp = web.json_response({"ok": True})
    resp.del_cookie("rd_session")
    return resp


async def status(request: web.Request) -> web.Response:
    logged_in = bool(get_session(request))
    return web.json_response(
        {
            "authenticated": logged_in,
            "audio": bool(CONFIG.get("audio_enabled")),
            "clipboard": True,
            "fileTransfer": False,
            "multiMonitor": False,
        }
    )


async def clipboard_get(request: web.Request) -> web.Response:
    require_session(request)
    try:
        text = await asyncio.to_thread(pyperclip.paste)
    except Exception as exc:
        raise web.HTTPInternalServerError(text=str(exc)) from exc
    return web.json_response({"text": text})


async def clipboard_set(request: web.Request) -> web.Response:
    require_session(request)
    data = await request.json()
    text = str(data.get("text", ""))
    await asyncio.to_thread(pyperclip.copy, text)
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
    with mss.mss() as sct:
        monitor = sct.monitors[1]
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
                await ws.send_json({"type": "error", "message": f"screen: {exc}"})
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
    if os.name != "nt" or not contacts:
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
        await ws.send_json({"type": "audio_status", "ok": False, "message": str(exc)})
        return

    try:
        microphones = sc.all_microphones(include_loopback=True)
        loopbacks = [
            mic
            for mic in microphones
            if "loopback" in repr(mic).lower() or "loopback" in mic.name.lower()
        ]
        mic = loopbacks[0] if loopbacks else microphones[0]
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
                await ws.send_bytes(
                    pack_binary_message(
                        {"type": "audio", "sampleRate": sample_rate, "channels": 2},
                        pcm.tobytes(),
                    )
                )
    except Exception as exc:
        await ws.send_json({"type": "audio_status", "ok": False, "message": str(exc)})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    token = require_session(request)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)

    state = session_state(token)
    tasks = [
        asyncio.create_task(screen_loop(ws, state)),
        asyncio.create_task(text_focus_loop(ws)),
    ]
    await ws.send_json({"type": "hello", "message": "connected"})

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "client_info":
                state["viewport_width"] = max(640, min(10000, int(event.get("viewportWidth", 0))))
            elif event.get("type") == "input":
                await asyncio.to_thread(handle_input_event, state, event)
            elif event.get("type") == "clipboard_set":
                await asyncio.to_thread(pyperclip.copy, str(event.get("text", "")))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return ws


async def input_websocket(request: web.Request) -> web.WebSocketResponse:
    token = require_session(request)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=512 * 1024)
    await ws.prepare(request)

    state = session_state(token)
    await ws.send_json({"type": "hello", "message": "input connected"})

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "input":
                await asyncio.to_thread(handle_input_event, state, event)
                if event.get("ack"):
                    await ws.send_json({"type": "ack", "profile": frame_profile(state)["name"]})
            elif event.get("type") == "clipboard_set":
                await asyncio.to_thread(pyperclip.copy, str(event.get("text", "")))
    finally:
        await asyncio.to_thread(release_input_state, state)
    return ws


async def audio_websocket(request: web.Request) -> web.WebSocketResponse:
    require_session(request)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    task = asyncio.create_task(audio_loop(ws))
    try:
        async for _ in ws:
            pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return ws


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/clipboard", clipboard_get)
    app.router.add_post("/api/clipboard", clipboard_set)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/ws/input", input_websocket)
    app.router.add_get("/ws/audio", audio_websocket)
    app.router.add_static("/static", STATIC_DIR)
    return app


if __name__ == "__main__":
    bind = str(os.environ.get("TRD_BIND", CONFIG["bind"]))
    port = int(os.environ.get("TRD_PORT", CONFIG["port"]))
    print(f"Tablet Remote Desktop listening on http://{bind}:{port}")
    print("Close this window to stop remote control.")
    web.run_app(build_app(), host=bind, port=port)
