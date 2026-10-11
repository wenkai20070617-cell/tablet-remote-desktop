"""Audio capture and wire codecs.

WebRTC carries audio as Opus.  The WebSocket fallback used to ship raw 16-bit
stereo PCM, which is roughly 768 kbit/s and unusable on a weak link, so the
fallback now carries 4-bit IMA ADPCM (4:1) by default.  The codec is a pure
Python implementation so no extra dependency and no paid service is needed.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator


class AudioUnavailable(RuntimeError):
    pass


# --- IMA ADPCM tables -------------------------------------------------------
_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
    2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
    18500, 20350, 22385, 24623, 27086, 29794, 32767,
)
_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)

# The encoder and the browser decoder both work on unsigned nibbles packed
# low-first, so the wire format stays byte-identical on every platform.


def encode_channel(samples, predictor: int = 0, index: int = 0, update: list | None = None) -> bytes:
    """Encode signed 16-bit samples for one channel into packed nibbles.

    When ``update`` is given, the final ``(predictor, index)`` is written into
    it so the stream can continue across frames.
    """
    out = bytearray()
    pending: int | None = None
    for sample in samples:
        step = _STEP_TABLE[index]
        diff = sample - predictor
        code = 8 if diff < 0 else 0
        if diff < 0:
            diff = -diff
        delta = step >> 3
        if diff >= step:
            code |= 4
            diff -= step
            delta += step
        if diff >= step >> 1:
            code |= 2
            diff -= step >> 1
            delta += step >> 1
        if diff >= step >> 2:
            code |= 1
            delta += step >> 2
        if code & 8:
            predictor -= delta
        else:
            predictor += delta
        predictor = max(-32768, min(32767, predictor))
        index += _INDEX_TABLE[code & 0x0F]
        index = max(0, min(88, index))
        if pending is None:
            pending = code
        else:
            out.append((code << 4) | pending)
            pending = None
    if pending is not None:
        out.append(pending)
    if update is not None:
        # ``state`` holds mutable ``[predictor, index]`` pairs; a tuple entry
        # cannot carry the state forward.
        try:
            update[0], update[1] = predictor, index
        except TypeError:
            pass
    return bytes(out)


def encode_interleaved(pcm: bytes, channels: int, state: list[tuple[int, int]]) -> bytes:
    """Encode interleaved 16-bit PCM, one nibble stream per channel.

    ``state`` is updated in place with the encoder state per channel so the
    stream survives being split across frames.
    """
    if channels <= 0:
        return b""
    total = len(pcm) // 2
    frames = total // channels
    if frames == 0:
        return b""
    try:
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2", count=frames * channels)
        samples = samples.reshape(frames, channels)
        planes = [samples[:, index].tolist() for index in range(channels)]
    except Exception:
        values = memoryview(pcm).cast("h")
        planes = [list(values[index::channels]) for index in range(channels)]
    payload = bytearray()
    for plane in range(channels):
        pred, idx = _state_pair(state[plane])
        entry = state[plane]
        if not isinstance(entry, list):
            # Normalise so the carried state always survives the frame boundary.
            entry = [pred, idx]
            state[plane] = entry
        payload += encode_channel(planes[plane], pred, idx, update=entry)
    return bytes(payload)


def _state_pair(entry) -> tuple[int, int]:
    """Accept either a ``(predictor, index)`` tuple or a mutable pair."""
    predictor, index = entry[0], entry[1]
    return int(predictor), int(index)


def decode_channel(payload: bytes, sample_count: int, predictor: int, index: int) -> list[int]:
    """Decode packed nibbles back into signed 16-bit samples."""
    samples: list[int] = []
    for byte in payload:
        for code in (byte & 0x0F, (byte >> 4) & 0x0F):
            step = _STEP_TABLE[index]
            delta = step >> 3
            if code & 4:
                delta += step
            if code & 2:
                delta += step >> 1
            if code & 1:
                delta += step >> 2
            if code & 8:
                predictor -= delta
            else:
                predictor += delta
            predictor = max(-32768, min(32767, predictor))
            index += _INDEX_TABLE[code]
            index = max(0, min(88, index))
            samples.append(predictor)
    return samples


def nibble_bytes(frame_count: int) -> int:
    """Packed size of one channel with ``frame_count`` samples."""
    return (frame_count + 1) // 2


def frame_payload_size(frame_count: int, channels: int) -> int:
    return nibble_bytes(frame_count) * max(1, channels)


# --- device discovery -------------------------------------------------------
def loopback_devices() -> list[Any]:
    """Return the system-audio loopback devices, never a microphone."""
    import soundcard as sc

    found = []
    for microphone in sc.all_microphones(include_loopback=True):
        text = f"{microphone.name} {microphone!r}".lower()
        if "loopback" in text or "monitor" in text:
            found.append(microphone)
    return found


def select_recorder(device_name: str, sample_rate: int, channels: int = 2) -> tuple[Any, str]:
    """Open a loopback recorder, honouring an explicit device name."""
    devices = loopback_devices()
    if not devices:
        raise AudioUnavailable("no system-audio loopback device is available")
    chosen = None
    if device_name:
        wanted = device_name.strip().lower()
        for device in devices:
            if wanted in device.name.lower() or wanted == str(device.id).lower():
                chosen = device
                break
        if chosen is None:
            raise AudioUnavailable(f"configured audio device was not found: {device_name}")
    else:
        chosen = devices[0]
    recorder = chosen.recorder(samplerate=sample_rate, channels=channels)
    return recorder, chosen.name


def audio_devices_json() -> list[dict[str, str]]:
    try:
        devices = loopback_devices()
    except Exception:
        return []
    return [{"id": str(device.id), "name": str(device.name)} for device in devices]


class AudioCapture:
    """Blocking system-audio reader; runs in a worker thread."""

    def __init__(
        self,
        sample_rate: int,
        frames: int,
        device_name: str = "",
        channels: int = 2,
    ) -> None:
        self.sample_rate = max(8000, min(96000, int(sample_rate)))
        self.frames = max(128, min(8192, int(frames)))
        self.channels = 2 if channels != 1 else 1
        self.requested_device = device_name
        self.device_name = ""
        self.recorder: Any = None
        self._np: Any = None

    def open(self) -> None:
        import numpy as np  # noqa: F401  (validated here so errors surface early)

        self._np = np
        self.recorder, self.device_name = select_recorder(
            self.requested_device, self.sample_rate, self.channels
        )
        enter = getattr(self.recorder, "__enter__", None)
        if enter is not None:
            self.recorder = enter()

    def read(self) -> bytes:
        if self.recorder is None:
            raise AudioUnavailable("audio recorder is not open")
        np = self._np
        data = self.recorder.record(self.frames)
        pcm = np.clip(data, -1.0, 1.0)
        return (pcm * 32767).astype(np.int16).tobytes()

    def read_planar(self) -> Any:
        if self.recorder is None:
            raise AudioUnavailable("audio recorder is not open")
        np = self._np
        data = self.recorder.record(self.frames)
        pcm = np.clip(data, -1.0, 1.0)
        samples = (pcm * 32767).astype(np.int16)
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        return samples.T.copy()

    def close(self) -> None:
        recorder = self.recorder
        self.recorder = None
        if recorder is None:
            return
        exit_method = getattr(recorder, "__exit__", None)
        if exit_method is not None:
            try:
                exit_method(None, None, None)
            except Exception:
                pass


async def iterate_audio(capture: AudioCapture) -> Iterator[bytes]:
    while True:
        yield await asyncio.to_thread(capture.read)
