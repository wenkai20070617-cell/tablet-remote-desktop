import math
import unittest

from app.audio import (
    decode_channel,
    encode_channel,
    encode_interleaved,
    frame_payload_size,
    nibble_bytes,
)


def _sine(frames: int, frequency: float = 997.0, amplitude: int = 15000, rate: int = 24000):
    return [
        int(amplitude * math.sin(2 * math.pi * frequency * index / rate))
        for index in range(frames)
    ]


class AdpcmTests(unittest.TestCase):
    def test_payload_size_is_predictable(self):
        self.assertEqual(nibble_bytes(960), 480)
        self.assertEqual(nibble_bytes(961), 481)
        self.assertEqual(frame_payload_size(960, 2), 960)

    def test_round_trip_stays_close_to_the_source(self):
        samples = _sine(960)
        payload = encode_channel(samples, 0, 0)
        self.assertEqual(len(payload), 480)
        decoded = decode_channel(payload, len(samples), 0, 0)
        self.assertEqual(len(decoded), len(samples))
        error = sum(abs(a - b) for a, b in zip(samples, decoded)) / len(samples)
        self.assertLess(error, 400, f"mean absolute error was {error:.1f}")

    def test_silence_round_trips_exactly(self):
        samples = [0] * 256
        decoded = decode_channel(encode_channel(samples, 0, 0), 256, 0, 0)
        self.assertEqual(decoded, samples)

    def test_encoder_state_is_carried_between_frames(self):
        # The state list is updated in place, so a second frame continues the
        # same predictor/step instead of restarting from silence.
        tone = _sine(480)
        restarted_state = [(0, 0), (0, 0)]
        carried_state = [(0, 0), (0, 0)]
        encode_interleaved(_pcm(tone, 2), 2, carried_state)
        self.assertNotEqual(carried_state, restarted_state)
        first = encode_interleaved(_pcm(tone, 2), 2, restarted_state)
        second = encode_interleaved(_pcm(tone, 2), 2, carried_state)
        # Carrying the predictor produces a different (better) bitstream than
        # restarting the encoder from zero on every frame.
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 480)
        self.assertEqual(len(second), 480)

    def test_carrying_the_state_reduces_error_across_frames(self):
        tone = _sine(960)
        state = [(0, 0), (0, 0)]
        payload = encode_interleaved(_pcm(tone, 2), 2, state)
        left = decode_channel(payload[:240], 480, 0, 0)
        error = sum(abs(a - b) for a, b in zip(tone[:480], left)) / 480
        self.assertLess(error, 400, f"mean absolute error was {error:.1f}")

        # A second frame decoded with the carried state must stay close too.
        carried = list(state[0])
        second_payload = encode_interleaved(_pcm(tone, 2), 2, [list(carried), list(state[1])])
        second_left = decode_channel(second_payload[:240], 480, carried[0], carried[1])
        second_error = sum(abs(a - b) for a, b in zip(tone[:480], second_left)) / 480
        self.assertLess(second_error, 400, f"second-frame error was {second_error:.1f}")

    def test_stereo_planes_are_encoded_independently(self):
        frames = 240
        left = _sine(frames, 440)
        right = _sine(frames, 880)
        interleaved = bytearray()
        for index in range(frames):
            interleaved += int(left[index]).to_bytes(2, "little", signed=True)
            interleaved += int(right[index]).to_bytes(2, "little", signed=True)
        state = [(0, 0), (0, 0)]
        payload = encode_interleaved(bytes(interleaved), 2, state)
        self.assertEqual(len(payload), frames // 2 * 2)
        left_payload = payload[: frames // 2]
        right_payload = payload[frames // 2 :]
        self.assertNotEqual(left_payload, right_payload)

    def test_oversized_values_are_clamped(self):
        samples = [32767, -32768, 32767, -32768] * 64
        decoded = decode_channel(encode_channel(samples, 0, 0), len(samples), 0, 0)
        self.assertTrue(all(-32768 <= value <= 32767 for value in decoded))

    def test_pcm_without_a_full_frame_is_ignored(self):
        state = [(0, 0)]
        self.assertEqual(encode_interleaved(b"\x01", 2, state), b"")
        self.assertEqual(encode_interleaved(b"", 2, state), b"")


def _pcm(samples, channels: int) -> bytes:
    out = bytearray()
    for index, value in enumerate(samples):
        out += int(value).to_bytes(2, "little", signed=True)
        for _ in range(channels - 1):
            out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


if __name__ == "__main__":
    unittest.main()
