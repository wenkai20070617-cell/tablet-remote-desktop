"""Screen capture and frame encoding.

Two performance rules drive this module:

* The capture object is created once and reused.  Re-creating ``mss`` per frame
  reallocates a device context and a bitmap on the Windows backend, which costs
  far more than the capture itself.
* Blocking desktop APIs and image encoding are invoked off the asyncio event
  loop, so login, input and heartbeat processing remain responsive.

:class:`AdaptiveController` turns observed cost - capture time, encode time,
send-queue depth, encoded size - into a frame rate, a quality and a capture
width.  It only backs off on evidence, and it keeps a floor frame rate instead
of collapsing while the client is merely idle.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import queue
import threading
import time
from typing import Any, Callable

from PIL import Image

try:
    import mss
except Exception:  # pragma: no cover - depends on the host desktop
    mss = None

# --- cursor -----------------------------------------------------------------
_CURSOR_READER: Callable[[], tuple[int, int] | None] | None = None
_CURSOR_INITIALISED = False


def _build_cursor_reader() -> Callable[[], tuple[int, int] | None] | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Point(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetCursorPos.argtypes = (ctypes.POINTER(Point),)
        user32.GetCursorPos.restype = wintypes.BOOL
        point = Point()

        def read() -> tuple[int, int] | None:
            if not user32.GetCursorPos(ctypes.byref(point)):
                return None
            return int(point.x), int(point.y)

        return read
    except Exception:  # pragma: no cover - restricted host
        return None


def cursor_reader() -> Callable[[], tuple[int, int] | None] | None:
    global _CURSOR_READER, _CURSOR_INITIALISED
    if not _CURSOR_INITIALISED:
        _CURSOR_INITIALISED = True
        _CURSOR_READER = _build_cursor_reader()
    return _CURSOR_READER


def cursor_visible() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        class CursorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", ctypes.c_int32 * 2),
            ]

        info = CursorInfo()
        info.cbSize = ctypes.sizeof(CursorInfo)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.GetCursorInfo(ctypes.byref(info)):
            return True
        return bool(info.flags & 0x00000001)
    except Exception:  # pragma: no cover - defensive
        return True


class MonitorInfo:
    __slots__ = ("index", "left", "top", "width", "height", "primary")

    def __init__(self, index: int, region: dict[str, int], primary: bool) -> None:
        self.index = index
        self.left = int(region["left"])
        self.top = int(region["top"])
        self.width = int(region["width"])
        self.height = int(region["height"])
        self.primary = primary

    def to_region(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "primary": self.primary,
        }


class Frame:
    """A captured frame plus the payload encoded from it."""

    __slots__ = ("meta", "payload", "signature", "capture_seconds", "encode_seconds", "image")

    def __init__(
        self,
        meta: dict[str, Any],
        payload: bytes,
        signature: str | None,
        capture_seconds: float,
        encode_seconds: float,
        image: Image.Image | None = None,
    ) -> None:
        self.meta = meta
        self.payload = payload
        self.signature = signature
        self.capture_seconds = capture_seconds
        self.encode_seconds = encode_seconds
        self.image = image


class CaptureEngine:
    """One long-lived capture thread owning one long-lived capture object."""

    def __init__(self, with_cursor: bool = False, name: str = "trd-capture") -> None:
        self._requests: queue.Queue = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None
        self._with_cursor = bool(with_cursor) and os.name != "nt"
        self._name = name
        self._closed = False
        self._monitors: list[MonitorInfo] = []
        self._monitors_at = 0.0
        self._ready = False
        self.last_error = ""

    @property
    def ready(self) -> bool:
        """True once at least one frame has been captured successfully."""
        return self._ready

    # -- worker --------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        session: Any = None
        failure: str | None = None
        if mss is None:
            failure = "the screen capture backend is unavailable"
        else:
            try:
                session = mss.mss(with_cursor=True) if self._with_cursor else mss.mss()
            except Exception as exc:  # pragma: no cover - backend failure
                failure = f"screen capture could not start: {exc}"
        try:
            while True:
                request = self._requests.get()
                if request is None:
                    break
                kind, payload, reply = request
                if failure is not None:
                    reply.put((None, RuntimeError(failure)))
                    continue
                try:
                    if kind == "monitors":
                        reply.put((list_monitors(session), None))
                        continue
                    raw = session.grab(payload)
                    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    del raw
                    self._ready = True
                    self.last_error = ""
                    reply.put((image, None))
                except Exception as exc:  # keep the worker alive; report per frame
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    reply.put((None, exc))
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _dispatch(self, region: dict[str, int], timeout: float) -> Image.Image:
        self._ensure_thread()
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put(("grab", region, reply), timeout=max(0.05, timeout))
        image, error = reply.get(timeout=max(0.05, timeout))
        if error is not None:
            raise error
        return image

    def request_monitors(self, timeout: float = 3.0) -> list[MonitorInfo]:
        self._ensure_thread()
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put(("monitors", None, reply), timeout=max(0.05, timeout))
        listing, error = reply.get(timeout=max(0.05, timeout))
        if error is not None:
            raise error
        return list(listing or [])

    def close(self) -> None:
        self._closed = True
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            try:
                self._requests.get_nowait()
                self._requests.put_nowait(None)
            except queue.Empty:  # pragma: no cover - defensive
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    # -- public API ----------------------------------------------------
    def monitors(self, max_age: float = 30.0) -> list[MonitorInfo]:
        now = time.monotonic()
        if self._monitors and now - self._monitors_at < max_age:
            return list(self._monitors)
        try:
            listing = self.request_monitors()
        except Exception:
            listing = []
        if listing:
            self._monitors = listing
            self._monitors_at = now
        return list(self._monitors)

    def monitor(self, selection: int) -> MonitorInfo:
        available = self.monitors()
        for entry in available:
            if entry.index == int(selection):
                return entry
        if available:
            return available[0]
        raise RuntimeError("no display is available for capture")

    def grab(self, region: dict[str, int], timeout: float = 2.0) -> Image.Image:
        if self._closed:
            raise RuntimeError("capture engine is closed")
        return self._dispatch(region, timeout)

    def grab_timed(self, region: dict[str, int], timeout: float = 2.0) -> tuple[Image.Image, float]:
        started = time.perf_counter()
        image = self.grab(region, timeout)
        return image, time.perf_counter() - started

    def capture_encoded(
        self,
        region: dict[str, int],
        quality: int,
        max_width: int,
        image_format: str,
        config: Callable[[], dict[str, Any]] | None = None,
    ) -> Frame:
        image, capture_seconds = self.grab_timed(region)
        return self.encode(image, quality, max_width, image_format, capture_seconds, config)

    def encode(
        self,
        image: Image.Image,
        quality: int,
        max_width: int,
        image_format: str,
        capture_seconds: float = 0.0,
        config: Callable[[], dict[str, Any]] | None = None,
    ) -> Frame:
        started = time.perf_counter()
        if max_width > 0 and image.width > max_width:
            height = max(1, int(image.height * (max_width / image.width)))
            image = image.resize((max_width, height), Image.Resampling.BILINEAR)
        signature = frame_signature(image, config)
        payload, payload_format = encode_image(image, quality, image_format)
        meta = {
            "width": image.width,
            "height": image.height,
            "format": payload_format,
        }
        if signature:
            meta["signature"] = signature
        return Frame(
            meta=meta,
            payload=payload,
            signature=signature,
            capture_seconds=capture_seconds,
            encode_seconds=time.perf_counter() - started,
            image=image,
        )


class SharedScreen:
    """Latest-frame fan-out on top of one :class:`CaptureEngine`.

    A single paced loop grabs the desktop for every consumer (WebRTC encoder,
    JPEG fallback, WebM writer).  Consumers read the most recent raw frame and
    do their own resizing and encoding, so a slow consumer never slows the
    capture down and the desktop is only grabbed once per tick.
    """

    def __init__(
        self,
        engine: CaptureEngine,
        fps: float = 30.0,
        min_fps: float = 8.0,
        name: str = "trd-shared-capture",
    ) -> None:
        self.engine = engine
        self.fps = max(1.0, float(fps))
        self.min_fps = max(1.0, min(self.fps, float(min_fps)))
        self.requested_fps = self.fps
        self._image: Image.Image | None = None
        self._at = 0.0
        self._sequence = 0
        self._region: dict[str, int] | None = None
        self._task: asyncio.Task | None = None
        self._name = name
        self._failures = 0

    def set_fps(self, fps: float) -> None:
        self.fps = max(self.min_fps, min(60.0, float(fps)))

    @property
    def region(self) -> dict[str, int] | None:
        return dict(self._region) if self._region else None

    @property
    def sequence(self) -> int:
        return self._sequence

    def read(self) -> tuple[Image.Image, float, int] | None:
        """Return the newest frame as ``(image, age_seconds, sequence)``."""
        image = self._image
        if image is None:
            return None
        return image, time.monotonic() - self._at, self._sequence

    def set_region(self, region: dict[str, int]) -> None:
        if self._region != region:
            self._region = dict(region)
            self._image = None

    async def run(self) -> None:
        pacer = FramePacer(self.fps)
        while True:
            region = self._region
            if region is None:
                await asyncio.sleep(0.05)
                continue
            pacer.set_fps(self.fps)
            try:
                image = await asyncio.to_thread(self.engine.grab, region, 2.0)
                self._image = image
                self._at = time.monotonic()
                self._sequence += 1
                self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures += 1
                await asyncio.sleep(min(2.0, 0.1 * self._failures))
                pacer.reset()
                continue
            await pacer.wait()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=self._name)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._image = None


_MONITOR_ENGINE: CaptureEngine | None = None
_MONITOR_LOCK = threading.Lock()


def list_monitors(session: Any | None = None) -> list[MonitorInfo]:
    """Enumerate displays, using the worker session when one is available."""
    raw = None
    if session is not None:
        try:
            raw = session.monitors
        except Exception:
            raw = None
    if raw is None:
        global _MONITOR_ENGINE
        with _MONITOR_LOCK:
            if _MONITOR_ENGINE is None:
                _MONITOR_ENGINE = CaptureEngine(with_cursor=True, name="trd-monitor")
            try:
                return _MONITOR_ENGINE.request_monitors()
            except Exception:
                return []
    result: list[MonitorInfo] = []
    for index in range(1, len(raw)):
        result.append(MonitorInfo(index, raw[index], index == 1))
    if not result and len(raw) > 0:
        result.append(MonitorInfo(1, raw[0], True))
    return result


def monitors_json(engine: CaptureEngine | None = None) -> list[dict[str, Any]]:
    try:
        listing = engine.monitors() if engine is not None else list_monitors()
    except Exception:
        return []
    return [entry.to_json() for entry in listing]


def frame_signature(
    image: Image.Image,
    config: Callable[[], dict[str, Any]] | None,
) -> str | None:
    settings = config() if config is not None else {}
    if not settings.get("change_detection_enabled", True):
        return None
    width = max(16, int(settings.get("change_thumbnail_width", 96)))
    height = max(10, int(settings.get("change_thumbnail_height", 60)))
    thumb = image.resize((width, height), Image.Resampling.BILINEAR).convert("L")
    return hashlib.blake2b(thumb.tobytes(), digest_size=8).hexdigest()


def encode_image(
    image: Image.Image,
    quality: int,
    image_format: str = "jpeg",
) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if image_format == "webp":
        image.save(buffer, format="WEBP", quality=int(quality), method=0)
        return buffer.getvalue(), "webp"
    image.save(buffer, format="JPEG", quality=int(quality))
    return buffer.getvalue(), "jpeg"


class AdaptiveController:
    """Turns observed cost into frame rate, quality and capture width."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.configure(settings or {})

    def configure(self, settings: dict[str, Any]) -> None:
        self.floor_fps = max(1.0, float(settings.get("min_fps", 8)))
        self.ceiling_fps = max(self.floor_fps, float(settings.get("max_fps", 30)))
        self.min_quality = max(5, int(settings.get("min_quality", 28)))
        self.max_quality = max(self.min_quality, int(settings.get("max_quality", 82)))
        self.base_quality = min(
            self.max_quality, max(self.min_quality, int(settings.get("quality", 55)))
        )
        self.min_width = max(320, int(settings.get("min_width", 960)))
        self.max_width = max(self.min_width, int(settings.get("max_width", 2560)))
        self.width_step = max(64, int(settings.get("width_step", 160)))
        self.quality_step = max(1, int(settings.get("quality_step", 6)))
        self.cpu_budget = max(0.1, float(settings.get("cpu_budget", 0.75)))
        self.bytes_budget = max(16_384, int(settings.get("bytes_budget", 1_200_000)))
        self.decay = max(0.0, min(0.95, float(settings.get("decay", 0.6))))
        self.fps = self.ceiling_fps
        self.quality = self.base_quality
        self.width = self.max_width
        self._idle_frames = 0
        self._busy_frames = 0
        self._dirty = 0.0
        self.last_utilisation = 0.0
        self.last_bytes = 0
        self.last_changed = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "fps": round(self.fps, 2),
            "quality": int(self.quality),
            "width": int(self.width),
            "load": round(self.last_utilisation, 3),
        }

    def observe(
        self,
        *,
        capture_seconds: float,
        encode_seconds: float,
        payload_bytes: int,
        interval: float,
        changed: bool,
        queue_ratio: float,
        frame_bytes: int,
    ) -> None:
        work = max(0.0, capture_seconds) + max(0.0, encode_seconds)
        interval = max(1e-3, interval)
        utilisation = work / interval
        self.last_utilisation = utilisation
        self.last_bytes = int(payload_bytes)
        self.last_changed = bool(changed)

        self._dirty = self._dirty * self.decay + (6.0 if changed else 0.0)
        self._dirty = min(60.0, self._dirty)

        congested = queue_ratio > 0.6 or payload_bytes > self.bytes_budget
        starving = queue_ratio < 0.15

        if congested or utilisation > self.cpu_budget:
            self._busy_frames += 1
            self._idle_frames = 0
            self._relieve(congested, utilisation)
        elif utilisation < self.cpu_budget * 0.55 and starving:
            self._idle_frames += 1
            self._busy_frames = 0
            if self._idle_frames >= 3:
                self._idle_frames = 0
                self._recover()
        else:
            self._idle_frames = 0
            self._busy_frames = 0

        if self._dirty > 3.0:
            target = self.ceiling_fps
        elif self._dirty > 0.5:
            target = max(self.floor_fps, self.ceiling_fps * 0.6)
        else:
            # Idle screen: keep a usable refresh so the client still sees the
            # clock, notifications and other small local updates.
            target = max(self.floor_fps, self.ceiling_fps * 0.35)
        target = min(target, self.ceiling_fps)
        if target > self.fps:
            self.fps = min(target, self.fps * 1.35 + 1.0)
        else:
            self.fps = max(target, self.fps * 0.8)
        self.fps = max(self.floor_fps, min(self.ceiling_fps, self.fps))

    def _relieve(self, congested: bool, utilisation: float) -> None:
        if utilisation > self.cpu_budget * 1.5:
            # The machine simply cannot encode this much: cut pixels first.
            self.fps = max(self.floor_fps, self.fps * 0.7)
            if self._busy_frames > 4:
                self.width = max(self.min_width, self.width - self.width_step)
                self._busy_frames = 0
            return
        if congested:
            self.quality = max(self.min_quality, self.quality - self.quality_step * 2)
            if self.quality <= self.min_quality:
                self.width = max(self.min_width, self.width - self.width_step)
            self.fps = max(self.floor_fps, self.fps * 0.85)
            return
        if self.quality > self.min_quality:
            self.quality = max(self.min_quality, self.quality - self.quality_step)
        elif self.width > self.min_width:
            self.width = max(self.min_width, self.width - self.width_step)
        else:
            self.fps = max(self.floor_fps, self.fps * 0.85)

    def _recover(self) -> None:
        if self.fps < self.ceiling_fps and self._dirty > 1.0:
            self.fps = min(self.ceiling_fps, self.fps * 1.15 + 0.5)
        elif self.quality < self.max_quality:
            self.quality = min(self.max_quality, self.quality + self.quality_step)
        elif self.width < self.max_width:
            self.width = min(self.max_width, self.width + self.width_step)


class FramePacer:
    """Fixed-rate scheduler that never adds work time on top of the sleep."""

    def __init__(self, fps: float = 15.0) -> None:
        self.period = 1.0 / max(1e-3, float(fps))
        self.next_at = time.monotonic()

    def set_fps(self, fps: float) -> None:
        self.period = 1.0 / max(1e-3, float(fps))

    def reset(self) -> None:
        self.next_at = time.monotonic()

    def sleep(self) -> None:
        now = time.monotonic()
        delay = self.next_at - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        # Re-anchor on the absolute schedule.  When the loop overruns, resync
        # instead of folding the whole processing time into every later frame.
        self.next_at += self.period
        if self.next_at < now - self.period:
            self.next_at = now + self.period

    async def wait(self) -> None:
        """Pace an asyncio loop without blocking the event-loop thread."""
        now = time.monotonic()
        delay = self.next_at - now
        if delay > 0:
            await asyncio.sleep(delay)
            now = time.monotonic()
        self.next_at += self.period
        if self.next_at < now - self.period:
            self.next_at = now + self.period
