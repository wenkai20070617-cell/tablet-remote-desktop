from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import threading
import uuid
from typing import Any, Awaitable, Callable

from PIL import Image

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
REMOTE_INTERFACE = "org.freedesktop.portal.RemoteDesktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"


def is_wayland_session() -> bool:
    return os.name != "nt" and (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


class WaylandPortalError(RuntimeError):
    pass


class JpegPipeWireReader:
    def __init__(self, fd: int, node_id: int) -> None:
        executable = shutil.which("gst-launch-1.0")
        if not executable:
            raise WaylandPortalError("GStreamer with its PipeWire plugin is required")
        self.fd = os.dup(fd)
        try:
            self.process = subprocess.Popen(
                [executable, "-q", "pipewiresrc", f"fd={self.fd}", f"path={node_id}",
                 "do-timestamp=true", "!", "videoconvert", "!", "video/x-raw,format=RGB",
                 "!", "jpegenc", "quality=80", "!", "fdsink", "fd=1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(self.fd,),
            )
        except Exception:
            os.close(self.fd)
            raise
        self._condition = threading.Condition()
        self._latest: bytes | None = None
        self._error: str | None = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        buffer = bytearray()
        try:
            while chunk := self.process.stdout.read(65536):
                buffer.extend(chunk)
                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start < 0:
                        if len(buffer) > 1: del buffer[:-1]
                        break
                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start: del buffer[:start]
                        break
                    frame = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]
                    with self._condition:
                        self._latest = frame
                        self._condition.notify_all()
        finally:
            stderr = self.process.stderr.read(4096) if self.process.stderr else b""
            with self._condition:
                self._error = stderr.decode(errors="replace").strip() or "PipeWire stream ended"
                self._condition.notify_all()

    def image(self, timeout: float = 5.0) -> Image.Image:
        with self._condition:
            if self._latest is None and self._error is None:
                self._condition.wait(timeout)
            if self._latest is None:
                raise WaylandPortalError(self._error or "Timed out waiting for a PipeWire frame")
            payload = self._latest
        with Image.open(io.BytesIO(payload)) as image:
            return image.convert("RGB")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired: self.process.kill()
        try: os.close(self.fd)
        except OSError: pass


class WaylandPortalSession:
    def __init__(self) -> None:
        self.bus: Any = None
        self.remote: Any = None
        self.screencast: Any = None
        self.session_handle: str | None = None
        self.stream_node: int | None = None
        self.logical_size = (1920, 1080)
        self.reader: JpegPipeWireReader | None = None
        self.active = False
        self._start_lock = asyncio.Lock()
        self._touch_slots: dict[int, int] = {}
        self._buttons_down: set[int] = set()

    def _request_path(self, token: str) -> str:
        sender = str(self.bus.unique_name).lstrip(":").replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    async def _portal_request(self, token: str, call: Callable[[], Awaitable[str]]) -> dict[str, Any]:
        from dbus_next.constants import MessageType
        expected = self._request_path(token)
        future = asyncio.get_running_loop().create_future()

        def handler(message: Any) -> bool:
            if (message.message_type == MessageType.SIGNAL and message.interface == REQUEST_INTERFACE
                    and message.member == "Response" and message.path == expected and not future.done()):
                future.set_result(message.body)
                return True
            return False

        self.bus.add_message_handler(handler)
        try:
            if str(await call()) != expected:
                raise WaylandPortalError("Portal returned an unexpected request handle")
            response, results = await asyncio.wait_for(future, timeout=300)
        finally:
            self.bus.remove_message_handler(handler)
        if int(response) != 0:
            raise WaylandPortalError("The local user denied or cancelled desktop permission")
        return {key: value.value for key, value in results.items()}

    async def start(self) -> None:
        if self.active: return
        async with self._start_lock:
            if self.active: return
            if not is_wayland_session():
                raise WaylandPortalError("This host is not running a Wayland session")
            from dbus_next import Variant
            from dbus_next.aio import MessageBus

            raw_fd: int | None = None
            try:
                self.bus = await MessageBus(negotiate_unix_fd=True).connect()
                introspection = await self.bus.introspect(PORTAL_NAME, PORTAL_PATH)
                proxy = self.bus.get_proxy_object(PORTAL_NAME, PORTAL_PATH, introspection)
                self.remote = proxy.get_interface(REMOTE_INTERFACE)
                self.screencast = proxy.get_interface(SCREENCAST_INTERFACE)

                session_token = "trd_session_" + uuid.uuid4().hex
                token = "trd_create_" + uuid.uuid4().hex
                result = await self._portal_request(token, lambda: self.remote.call_create_session({
                    "handle_token": Variant("s", token), "session_handle_token": Variant("s", session_token)}))
                self.session_handle = str(result["session_handle"])

                token = "trd_sources_" + uuid.uuid4().hex
                await self._portal_request(token, lambda: self.screencast.call_select_sources(
                    self.session_handle, {"handle_token": Variant("s", token), "types": Variant("u", 1),
                                          "multiple": Variant("b", False), "cursor_mode": Variant("u", 2)}))
                token = "trd_devices_" + uuid.uuid4().hex
                await self._portal_request(token, lambda: self.remote.call_select_devices(
                    self.session_handle, {"handle_token": Variant("s", token), "types": Variant("u", 7),
                                          "persist_mode": Variant("u", 1)}))
                token = "trd_start_" + uuid.uuid4().hex
                result = await self._portal_request(token, lambda: self.remote.call_start(
                    self.session_handle, "", {"handle_token": Variant("s", token)}))
                streams = result.get("streams") or []
                if not streams: raise WaylandPortalError("The portal did not provide a screen stream")
                node_id, properties = streams[0]
                self.stream_node = int(node_id)
                props = {key: value.value for key, value in properties.items()}
                size = props.get("logical_size") or props.get("size")
                if size and len(size) == 2: self.logical_size = (int(size[0]), int(size[1]))
                fd = await self.screencast.call_open_pipe_wire_remote(self.session_handle, {})
                if isinstance(fd, (list, tuple)): fd = fd[0]
                raw_fd = int(fd)
                self.reader = JpegPipeWireReader(raw_fd, self.stream_node)
                self.active = True
            except Exception:
                try:
                    await self.close()
                except Exception:
                    pass
                raise
            finally:
                if raw_fd is not None:
                    try: os.close(raw_fd)
                    except OSError: pass
    def capture_image(self) -> Image.Image:
        if not self.active or not self.reader: raise WaylandPortalError("Wayland permission is not active")
        return self.reader.image()

    def monitor(self) -> dict[str, int]:
        return {"left": 0, "top": 0, "width": self.logical_size[0], "height": self.logical_size[1]}

    def _point(self, event: dict[str, Any]) -> tuple[float, float]:
        return float(event["x"]) * self.logical_size[0], float(event["y"]) * self.logical_size[1]

    async def _button(self, button: int, down: bool) -> None:
        code = {0: 272, 1: 274, 2: 273}[button]
        await self.remote.call_notify_pointer_button(self.session_handle, {}, code, 1 if down else 0)
        self._buttons_down.add(code) if down else self._buttons_down.discard(code)

    @staticmethod
    def _keysym(key: str) -> int:
        names = {"Enter":0xFF0D,"Backspace":0xFF08,"Tab":0xFF09,"Escape":0xFF1B,"Delete":0xFFFF,
                 "Insert":0xFF63,"Home":0xFF50,"End":0xFF57,"PageUp":0xFF55,"PageDown":0xFF56,
                 "ArrowLeft":0xFF51,"ArrowUp":0xFF52,"ArrowRight":0xFF53,"ArrowDown":0xFF54,
                 **{f"F{i}":0xFFBD+i for i in range(1,13)}}
        if key in names: return names[key]
        if len(key) == 1:
            value = ord(key)
            return value if value <= 0xFF else 0x01000000 | value
        raise WaylandPortalError("Unsupported key")

    async def _key(self, keysym: int, down: bool) -> None:
        await self.remote.call_notify_keyboard_keysym(self.session_handle, {}, keysym, 1 if down else 0)

    async def handle_input(self, event: dict[str, Any]) -> None:
        if not self.active: raise WaylandPortalError("Wayland permission is not active")
        kind = event["kind"]
        if kind in {"move","down","up","click","double"}:
            x, y = self._point(event)
            await self.remote.call_notify_pointer_motion_absolute(self.session_handle, {}, self.stream_node, x, y)
            if kind == "down": await self._button(event["button"], True)
            elif kind == "up": await self._button(event["button"], False)
            elif kind in {"click","double"}:
                for _ in range(2 if kind == "double" else 1):
                    await self._button(event["button"], True); await self._button(event["button"], False)
        elif kind == "mouse_delta":
            await self.remote.call_notify_pointer_motion(self.session_handle, {}, float(event["dx"]), float(event["dy"]))
        elif kind == "mouse_button":
            if event["action"] == "click": await self._button(event["button"], True); await self._button(event["button"], False)
            else: await self._button(event["button"], event["action"] == "down")
        elif kind == "wheel":
            if event.get("delta"): await self.remote.call_notify_pointer_axis_discrete(self.session_handle, {}, 0, int(event["delta"]))
            if event.get("deltaX"): await self.remote.call_notify_pointer_axis_discrete(self.session_handle, {}, 1, int(event["deltaX"]))
        elif kind == "key":
            modifiers = ([0xFFE3] if event.get("ctrl") else []) + ([0xFFE1] if event.get("shift") else []) + ([0xFFE9] if event.get("alt") else []) + ([0xFFEB] if event.get("meta") else [])
            for item in modifiers: await self._key(item, True)
            symbol = self._keysym(event["key"]); await self._key(symbol, True); await self._key(symbol, False)
            for item in reversed(modifiers): await self._key(item, False)
        elif kind == "text":
            for character in event["text"]:
                symbol = self._keysym(character); await self._key(symbol, True); await self._key(symbol, False)
        elif kind == "touch":
            for contact in event["contacts"]:
                raw_id = int(contact["id"])
                state = contact["state"]
                slot = self._touch_slots.get(raw_id)
                if slot is None:
                    if state != "down":
                        continue
                    used = set(self._touch_slots.values())
                    slot = next((candidate for candidate in range(10) if candidate not in used), None)
                    if slot is None:
                        raise WaylandPortalError("No free touch slot is available")
                    self._touch_slots[raw_id] = slot
                x, y = self._point(contact)
                if state == "down": await self.remote.call_notify_touch_down(self.session_handle, {}, self.stream_node, slot, x, y)
                elif state == "update": await self.remote.call_notify_touch_motion(self.session_handle, {}, self.stream_node, slot, x, y)
                else: await self.remote.call_notify_touch_up(self.session_handle, {}, slot); self._touch_slots.pop(raw_id, None)

    async def release_input(self) -> None:
        if not self.active: return
        for code in tuple(self._buttons_down):
            try: await self.remote.call_notify_pointer_button(self.session_handle, {}, code, 0)
            except Exception: pass
        self._buttons_down.clear()
        for slot in tuple(self._touch_slots.values()):
            try: await self.remote.call_notify_touch_up(self.session_handle, {}, slot)
            except Exception: pass
        self._touch_slots.clear()

    async def close(self) -> None:
        await self.release_input()
        if self.reader: self.reader.close(); self.reader = None
        if self.session_handle and self.bus:
            try:
                info = await self.bus.introspect(PORTAL_NAME, self.session_handle)
                obj = self.bus.get_proxy_object(PORTAL_NAME, self.session_handle, info)
                await obj.get_interface("org.freedesktop.portal.Session").call_close()
            except Exception: pass
        if self.bus: self.bus.disconnect()
        self.bus = None
        self.remote = None
        self.screencast = None
        self.session_handle = None
        self.stream_node = None
        self.active = False


_PORTAL = WaylandPortalSession() if is_wayland_session() else None


def get_wayland_portal() -> WaylandPortalSession | None:
    return _PORTAL