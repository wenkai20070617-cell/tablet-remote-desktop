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
except Exception:  # Optional at import time so WebSocket fallback can still start.
    np = None
    AudioFrame = VideoFrame = None
    AudioStreamTrack = VideoStreamTrack = object
    RTCConfiguration = RTCIceServer = RTCPeerConnection = RTCSessionDescription = None


WEBRTC_AVAILABLE = RTCPeerConnection is not None


class DesktopVideoTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, capture: Callable[[], Any], fps: int = 15) -> None:
        super().__init__()
        self.capture = capture
        self.fps = max(1, min(30, int(fps)))
        self._next_at = time.monotonic()
        self._pts = 0
        self._time_base = Fraction(1, 90_000)

    async def recv(self):
        interval = 1 / self.fps
        delay = self._next_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_at = max(self._next_at + interval, time.monotonic())
        image = await asyncio.to_thread(self.capture)
        frame = VideoFrame.from_image(image.convert("RGB"))
        self._pts += int(90_000 / self.fps)
        frame.pts = self._pts
        frame.time_base = self._time_base
        return frame


class SystemAudioTrack(AudioStreamTrack):
    kind = "audio"

    def __init__(self, recorder: Any, sample_rate: int, frames: int, device_name: str) -> None:
        super().__init__()
        self.recorder = recorder
        self.sample_rate = sample_rate
        self.frames = frames
        self.device_name = device_name
        self._pts = 0
        self._closed = False

    @classmethod
    def create(cls, sample_rate: int, frames: int) -> "SystemAudioTrack":
        import soundcard as sc

        microphones = sc.all_microphones(include_loopback=True)
        loopbacks = [
            mic for mic in microphones
            if "loopback" in repr(mic).lower() or "loopback" in mic.name.lower()
        ]
        if not loopbacks:
            raise RuntimeError("No system-audio loopback device is available")
        microphone = loopbacks[0]
        recorder = microphone.recorder(samplerate=sample_rate, channels=2)
        recorder.__enter__()
        return cls(recorder, sample_rate, frames, microphone.name)

    def _read(self):
        data = self.recorder.record(self.frames)
        pcm = np.clip(data, -1.0, 1.0)
        return (pcm * 32767).astype(np.int16).T.copy()

    async def recv(self):
        samples = await asyncio.to_thread(self._read)
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
                self.recorder.__exit__(None, None, None)
            except Exception:
                pass
        super().stop()


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


async def create_peer_answer(
    offer_sdp: str,
    offer_type: str,
    capture: Callable[[], Any],
    on_control_message: Callable[[str], None],
    on_state_change: Callable[[str], None],
    stun_urls: list[str],
    fps: int,
    audio_enabled: bool,
    audio_sample_rate: int,
    audio_frames: int,
) -> tuple[Any, dict[str, Any]]:
    if not WEBRTC_AVAILABLE:
        raise RuntimeError("WebRTC dependencies are not installed")
    ice_servers = [RTCIceServer(urls=stun_urls)] if stun_urls else []
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    pc.addTrack(DesktopVideoTrack(capture, fps=fps))
    audio_name = None
    if audio_enabled:
        try:
            audio_track = await asyncio.to_thread(
                SystemAudioTrack.create, audio_sample_rate, audio_frames
            )
            pc.addTrack(audio_track)
            audio_name = audio_track.device_name
        except Exception:
            audio_name = None

    @pc.on("datachannel")
    def on_datachannel(channel: Any) -> None:
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
        if pc.connectionState in {"failed", "closed"}:
            await pc.close()

    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await wait_for_ice_gathering(pc)
        return pc, {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "audio": audio_name is not None,
        }
    except Exception:
        await pc.close()
        raise


def parse_control_message(raw: str) -> Any:
    if len(raw) > 1_100_000:
        raise ValueError("control message is too large")
    return json.loads(raw)