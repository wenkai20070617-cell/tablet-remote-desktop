import asyncio
import unittest
from unittest.mock import patch

from PIL import Image

from app.webrtc import WEBRTC_AVAILABLE, SystemAudioTrack, create_peer_answer

if WEBRTC_AVAILABLE:
    from aiortc import RTCPeerConnection, RTCSessionDescription


@unittest.skipUnless(WEBRTC_AVAILABLE, "aiortc is not installed")
class WebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_frame_shape_and_timestamps(self):
        import numpy as np

        class Recorder:
            def record(self, frames):
                return np.zeros((frames, 2), dtype=np.float32)

            def __exit__(self, *_args):
                return None

        track = SystemAudioTrack(Recorder(), 24000, 480, "test")
        try:
            frame = await track.recv()
            self.assertEqual(frame.sample_rate, 24000)
            self.assertEqual(frame.samples, 480)
            self.assertEqual(frame.layout.name, "stereo")
        finally:
            track.stop()

    async def test_failed_negotiation_closes_peer(self):
        class FakePeer:
            def __init__(self):
                self.closed = False
                self.connectionState = "new"

            def addTrack(self, _track):
                return None

            def on(self, _event):
                return lambda callback: callback

            async def setRemoteDescription(self, _description):
                raise ValueError("bad SDP")

            async def close(self):
                self.closed = True

        fake = FakePeer()
        with patch("app.webrtc.RTCPeerConnection", return_value=fake):
            with self.assertRaises(ValueError):
                await create_peer_answer(
                    offer_sdp="invalid",
                    offer_type="offer",
                    capture=lambda: Image.new("RGB", (32, 32), "black"),
                    on_control_message=lambda _message: None,
                    on_state_change=lambda _state: None,
                    stun_urls=[],
                    fps=5,
                    audio_enabled=False,
                    audio_sample_rate=24000,
                    audio_frames=1024,
                )
        self.assertTrue(fake.closed)

    async def test_local_video_and_control_channel_negotiation(self):
        client = RTCPeerConnection()
        received_track = asyncio.get_running_loop().create_future()
        control_open = asyncio.Event()
        received_messages = []
        client.addTransceiver("video", direction="recvonly")
        control = client.createDataChannel("control")

        @client.on("track")
        def on_track(track):
            if track.kind == "video" and not received_track.done():
                received_track.set_result(track)

        @control.on("open")
        def on_open():
            control_open.set()

        offer = await client.createOffer()
        await client.setLocalDescription(offer)
        server, answer = await create_peer_answer(
            offer_sdp=client.localDescription.sdp,
            offer_type=client.localDescription.type,
            capture=lambda: Image.new("RGB", (320, 180), "black"),
            on_control_message=received_messages.append,
            on_state_change=lambda _state: None,
            stun_urls=[],
            fps=5,
            audio_enabled=False,
            audio_sample_rate=24000,
            audio_frames=1024,
        )
        try:
            await client.setRemoteDescription(
                RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
            )
            track = await asyncio.wait_for(received_track, timeout=10)
            frame = await asyncio.wait_for(track.recv(), timeout=10)
            self.assertEqual((frame.width, frame.height), (320, 180))
            await asyncio.wait_for(control_open.wait(), timeout=10)
            control.send('{"type":"client_info","viewportWidth":800}')
            for _ in range(50):
                if received_messages:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(received_messages, ['{"type":"client_info","viewportWidth":800}'])
        finally:
            await client.close()
            await server.close()


if __name__ == "__main__":
    unittest.main()