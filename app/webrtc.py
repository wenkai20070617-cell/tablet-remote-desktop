"""WebRTC media layer built on aiortc.

The video track drives the capture callback, which returns a frame already
sized by the adaptive controller.  ``recv()`` is blocked on an event, so a
capture only happens when the encoder actually asks for a frame - the pacing
loop lives in exactly one place.

Audio is always Opus.  aiortc negotiates Opus through the SDP; video bitrate is
adapted through the sender API when available and the codec target otherwise.
"""

from __future__ import annotations

import asyncio
import json
import time
from fractions import Fraction
from typing import Any, Callable

try:
    import numpy as np
    from av import AudioFrame, VideoFrame
    from aiortc import (
        AudioStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
        VideoStreamTrack,
    )
    from aiortc.rtcrtpparameters import RTCRtpEncodingParameters
except Exception:  # Optional at import time so the WebSocket fallback can still start.
    np = None
    AudioFrame = VideoFrame = None
    AudioStreamTrack = VideoStreamTrack = object
    RTCConfiguration = RTCIceServer = RTCPeerConnection = RTCSessionDescription = None
    RTCRtpEncodingParameters = None


WEBRTC_AVAILABLE = RTCPeerConnection is not None

VIDEO_CLOCK_RATE = 90_000
DEFAULT_AUDIO_SAMPLE_RATE = 48_000
DEFAULT_AUDIO_FRAMES = 960


def peer_connection_expired(
    state: str,
    *,
    now: float,
    unhealthy_since: float | None,
    last_seen: float,
    grace_seconds: float,
    recover_seconds: float,
    idle_seconds: float,
) -> bool:
    """Decide when a peer can no longer consume a session connection slot."""
    if state == "connected":
        return False
    if state in {"failed", "closed"}:
        return unhealthy_since is not None and now - unhealthy_since >= grace_seconds
    if state == "disconnected":
        return unhealthy_since is not None and now - unhealthy_since >= recover_seconds
    return now - last_seen >= idle_seconds


class DesktopVideoTrack(VideoStreamTrack):
    """Desktop video source fed by a shared, already-captured frame buffer."""

    kind = "video"

    def __init__(
        self,
        frame_source: Callable[[], Any],
        fps: int = 30,
        min_fps: int = 8,
        max_frame_age: float = 1.0,
    ) -> None:
        super().__init__()
        self.frame_source = frame_source
        self.max_frame_age = max(0.05, float(max_frame_age))
        self.target_fps = max(1, min(60, int(fps)))
        self.min_fps = max(1, min(self.target_fps, int(min_fps)))
        self._fps = float(self.target_fps)
        self._next_at = time.monotonic()
        self._pts = 0
        self._time_base = Fraction(1, VIDEO_CLOCK_RATE)
        self._wake = asyncio.Event()
        self._frames = 0
        self._total_frames = 0
        self._dropped_frames = 0
        self._capture_errors = 0
        self._bytes = 0
        self._rtt = 0.0
        self._loss = 0.0
        self._jitter = 0.0
        self._last_frame_bytes = 0
        self._last_capture_seconds = 0.0
        self._last_frame_age = 0.0
        self._started_at = time.monotonic()
        self.last_frame_at = 0.0

    # -- adaptive controls ---------------------------------------------
    def request_frame(self) -> None:
        """Ask for a fresh frame as soon as the encoder is ready."""
        self._wake.set()

    def set_fps(self, fps: float) -> None:
        self._fps = max(0.5, min(60.0, float(fps)))

    @property
    def stats(self) -> dict[str, Any]:
        elapsed = max(1e-3, time.monotonic() - self._started_at)
        return {
            "frames": self._frames,
            "bytes": self._bytes,
            "averageFps": round(self._frames / elapsed, 2),
            "bitrate": int(self._bytes * 8 / elapsed),
            "frameBytes": self._last_frame_bytes,
            "captureSeconds": round(self._last_capture_seconds, 4),
            "frameAge": round(self._last_frame_age, 4),
            "captureErrors": self._capture_errors,
            "rtt": round(self._rtt, 4),
            "loss": round(self._loss, 4),
            "jitter": round(self._jitter, 5),
            "totalFrames": self._total_frames,
            "droppedFrames": self._dropped_frames,
            "fps": round(self._fps, 2),
        }

    def note_outbound(self, stats: dict[str, Any]) -> None:
        """Feed back RTCP-derived quality so the track can be tuned."""
        self._rtt = float(stats.get("roundTripTime", 0.0) or 0.0)
        self._loss = float(stats.get("fractionLost", 0.0) or 0.0)
        self._jitter = float(stats.get("jitter", 0.0) or 0.0)

    async def recv(self):
        while True:
            delay = self._next_at - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(delay, 0.5))
                except asyncio.TimeoutError:
                    pass
                if self._wake.is_set():
                    self._wake.clear()
                    break
                continue
            break
        self._next_at = max(
            self._next_at + 1.0 / max(0.5, self._fps), time.monotonic()
        )
        started = time.perf_counter()
        image = None
        try:
            result = await asyncio.to_thread(self.frame_source)
        except Exception:
            result = None
        self._last_capture_seconds = time.perf_counter() - started
        if isinstance(result, tuple):
            image = result[0]
            self._last_frame_age = float(result[1]) if len(result) > 1 else 0.0
        else:
            image = result
            self._last_frame_age = 0.0
        if image is None:
            self._capture_errors += 1
            self._dropped_frames += 1
            image = _blank_frame()
        if getattr(image, "mode", "") not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        frame = VideoFrame.from_image(image)
        self._pts += max(1, int(VIDEO_CLOCK_RATE / max(1.0, self._fps)))
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._frames += 1
        self._total_frames += 1
        self._last_frame_bytes = int(image.width * image.height / 4)
        self._bytes += self._last_frame_bytes
        self.last_frame_at = time.monotonic()
        return frame


class SystemAudioTrack(AudioStreamTrack):
    """Opus-bound system audio track."""

    kind = "audio"

    def __init__(
        self,
        capture: Any,
        sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
        frames: int = DEFAULT_AUDIO_FRAMES,
        device_name: str = "",
    ) -> None:
        super().__init__()
        self.capture = capture
        self.sample_rate = int(sample_rate)
        self.frames = int(frames)
        self.device_name = device_name
        self._pts = 0
        self._closed = False
        self._failures = 0

    @classmethod
    def create(
        cls,
        sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
        frames: int = DEFAULT_AUDIO_FRAMES,
        device_name: str = "",
    ) -> "SystemAudioTrack":
        from app.audio import AudioCapture

        capture = AudioCapture(sample_rate, frames, device_name, channels=2)
        capture.open()
        return cls(capture, sample_rate, frames, capture.device_name)

    async def recv(self):
        samples = await asyncio.to_thread(self.capture.read_planar)
        frame = AudioFrame.from_ndarray(samples, format="s16p", layout="stereo")
        frame.sample_rate = self.sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self.sample_rate)
        self._pts += samples.shape[1]
        return frame

    def stop(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.capture.close()
            except Exception:
                pass
        super().stop()


def _blank_frame() -> Any:
    from PIL import Image

    return Image.new("RGB", (640, 360), (0, 0, 0))


async def wait_for_ice_gathering(pc: Any, timeout: float = 5.0) -> None:
    if pc.iceGatheringState == "complete":
        return
    ready = asyncio.Event()

    @pc.on("icegatheringstatechange")
    async def on_ice_gathering_state_change() -> None:
        if pc.iceGatheringState == "complete":
            ready.set()

    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


def build_ice_servers(entries: list[dict[str, Any]]) -> list[Any]:
    servers = []
    for entry in entries:
        urls = entry.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        urls = [str(url) for url in urls if str(url).strip()]
        if not urls:
            continue
        username = entry.get("username")
        credential = entry.get("credential")
        if username is not None and credential is not None:
            servers.append(RTCIceServer(urls=urls, username=str(username), credential=str(credential)))
        else:
            servers.append(RTCIceServer(urls=urls))
    return servers


async def create_peer_answer(
    offer_sdp: str,
    offer_type: str,
    frame_source: Callable[[], Any],
    on_control_message: Callable[[str], None],
    on_state_change: Callable[[str], None],
    stun_urls: list[str],
    fps: int,
    audio_enabled: bool,
    audio_sample_rate: int,
    audio_frames: int,
    *,
    ice_servers: list[dict[str, Any]] | None = None,
    min_fps: int | None = None,
    audio_device: str = "",
    on_datachannel: Callable[[Any], None] | None = None,
    ice_transport_policy: str = "all",
    video_bitrate: int = 0,
) -> tuple[Any, dict[str, Any]]:
    if not WEBRTC_AVAILABLE:
        raise RuntimeError("WebRTC dependencies are not installed")
    if ice_servers is None:
        ice_servers = [{"urls": stun_urls}] if stun_urls else []
    configuration = RTCConfiguration(iceServers=build_ice_servers(ice_servers))
    if ice_transport_policy in {"all", "relay"}:
        configuration.iceTransportPolicy = ice_transport_policy
    pc = RTCPeerConnection(configuration)
    video_track = DesktopVideoTrack(frame_source, fps=fps, min_fps=min_fps or max(1, fps // 3))
    pc.addTrack(video_track)
    audio_track = None
    audio_name = None
    if audio_enabled:
        try:
            audio_track = await asyncio.to_thread(
                SystemAudioTrack.create, audio_sample_rate, audio_frames, audio_device
            )
            pc.addTrack(audio_track)
            audio_name = audio_track.device_name
        except Exception:
            audio_track = None
            audio_name = None

    @pc.on("datachannel")
    def on_data_channel(channel: Any) -> None:
        if on_datachannel is not None:
            on_datachannel(channel)
            return
        if channel.label != "control":
            channel.close()
            return

        @channel.on("message")
        def on_message(message: Any) -> None:
            if isinstance(message, str):
                on_control_message(message)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange() -> None:
        on_state_change(pc.connectionState)
        if pc.connectionState == "closed":
            await pc.close()

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await wait_for_ice_gathering(pc)
        if video_bitrate:
            await apply_sender_bitrate(pc, {"video": int(video_bitrate)})
        return pc, {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "audio": audio_name is not None,
            "audioDevice": audio_name or "",
            "videoTrack": video_track,
            "audioTrack": audio_track,
        }
    except Exception:
        await pc.close()
        raise


async def apply_sender_bitrate(pc: Any, prefers: dict[str, int]) -> bool:
    """Apply a bitrate cap using the API supported by the installed aiortc.

    ``prefers`` maps a track kind ("video", "audio") to a target bitrate.
    Newer aiortc releases expose WebRTC-style sender parameters.  aiortc 1.15
    does not, but its codec encoder exposes the same target used by REMB.  The
    fallback is deliberately feature-detected and the caller retries until the
    encoder has been created by the first media frame.
    """
    applied = False
    for sender in pc.getSenders():
        if sender.track is None:
            continue
        wanted = prefers.get(sender.track.kind)
        if not wanted:
            continue
        get_parameters = getattr(sender, "getParameters", None)
        set_parameters = getattr(sender, "setParameters", None)
        if callable(get_parameters) and callable(set_parameters):
            try:
                parameters = get_parameters()
                encodings = list(parameters.encodings or [])
                if not encodings:
                    encodings = [RTCRtpEncodingParameters(maxBitrate=int(wanted))]
                for encoding in encodings:
                    encoding.maxBitrate = int(wanted)
                parameters.encodings = encodings
                await set_parameters(parameters)
                applied = True
                continue
            except Exception:
                pass
        encoder = getattr(sender, "_RTCRtpSender__encoder", None)
        if encoder is not None and hasattr(encoder, "target_bitrate"):
            try:
                encoder.target_bitrate = int(wanted)
                applied = True
            except Exception:
                pass
    return applied


async def collect_peer_stats(pc: Any) -> dict[str, Any]:
    """Read the outbound RTP stats that drive adaptive quality."""
    result: dict[str, Any] = {
        "roundTripTime": 0.0,
        "fractionLost": 0.0,
        "jitter": 0.0,
        "bytesSent": 0,
        "packetsSent": 0,
        "availableOutgoingBitrate": 0,
        "framesEncoded": 0,
        "framesPerSecond": 0.0,
        "qualityLimitationReason": "",
        "remoteInbound": 0,
    }
    try:
        report = await pc.getStats()
    except Exception:
        return result
    for item in report.values():
        if item.type == "remote-inbound-rtp":
            result["roundTripTime"] = float(getattr(item, "roundTripTime", 0.0) or 0.0)
            result["fractionLost"] = float(getattr(item, "fractionLost", 0.0) or 0.0)
            result["jitter"] = float(getattr(item, "jitter", 0.0) or 0.0)
            result["remoteInbound"] = int(getattr(item, "packetsLost", 0) or 0)
        elif item.type == "outbound-rtp" and getattr(item, "kind", "") == "video":
            result["bytesSent"] = int(getattr(item, "bytesSent", 0) or 0)
            result["packetsSent"] = int(getattr(item, "packetsSent", 0) or 0)
            result["framesEncoded"] = int(getattr(item, "framesEncoded", 0) or 0)
            result["framesPerSecond"] = float(getattr(item, "framesPerSecond", 0.0) or 0.0)
            result["qualityLimitationReason"] = str(
                getattr(item, "qualityLimitationReason", "") or ""
            )
        elif item.type == "candidate-pair" and getattr(item, "state", "") == "succeeded":
            available = int(getattr(item, "availableOutgoingBitrate", 0) or 0)
            if available:
                result["availableOutgoingBitrate"] = available
            current_rtt = float(getattr(item, "currentRoundTripTime", 0.0) or 0.0)
            if current_rtt and not result["roundTripTime"]:
                result["roundTripTime"] = current_rtt
    return result


async def restart_ice(pc: Any) -> dict[str, Any]:
    """Renegotiate an existing peer connection with a fresh ICE generation.

    aiortc implements ``restartIce()`` as a no-op placeholder: it does not
    generate new ICE credentials on its own.  When the peer cannot restart
    itself the caller must fall back to a fresh negotiation, so this raises a
    clear error instead of returning a useless answer.
    """
    restarter = getattr(pc, "restartIce", None)
    if restarter is None:
        raise RuntimeError("this peer connection cannot restart ICE")
    result = restarter()
    if asyncio.iscoroutine(result):
        await result
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await wait_for_ice_gathering(pc, timeout=4.0)
    sdp = str(pc.localDescription.sdp)
    if "ice-ufrag" in sdp and _ice_ufrag(sdp) == _ice_ufrag(pc.remoteDescription.sdp):
        raise RuntimeError("ICE credentials were not renewed")
    return {"sdp": sdp, "type": pc.localDescription.type}


def _ice_ufrag(sdp: str) -> str:
    for line in str(sdp).splitlines():
        if line.startswith("a=ice-ufrag:"):
            return line.split(":", 1)[1].strip()
    return ""


async def accept_renegotiation(pc: Any, sdp: str, description_type: str) -> dict[str, Any]:
    """Answer a browser-initiated renegotiation (ICE restart from the client)."""
    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=description_type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await wait_for_ice_gathering(pc, timeout=4.0)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


def parse_control_message(raw: str) -> Any:
    if len(raw) > 1_100_000:
        raise ValueError("control message is too large")
    return json.loads(raw)
