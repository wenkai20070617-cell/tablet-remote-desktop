import asyncio
import unittest
from unittest.mock import patch

from PIL import Image

from app.webrtc import (
    WEBRTC_AVAILABLE,
    DesktopVideoTrack,
    SystemAudioTrack,
    apply_sender_bitrate,
    build_ice_servers,
    create_peer_answer,
    peer_connection_expired,
)

if WEBRTC_AVAILABLE:
    from aiortc import RTCPeerConnection, RTCSessionDescription


def _frame_source():
    return Image.new("RGB", (320, 180), "black")


class BitrateControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_aiortc_115_encoder_fallback_applies_the_target(self):
        class Track:
            kind = "video"

        class Encoder:
            target_bitrate = 0

        class Sender:
            track = Track()

        class Peer:
            def __init__(self):
                self.sender = Sender()
                setattr(self.sender, "_RTCRtpSender__encoder", Encoder())

            def getSenders(self):
                return [self.sender]

        peer = Peer()
        applied = await apply_sender_bitrate(peer, {"video": 1_500_000})
        self.assertTrue(applied)
        encoder = getattr(peer.sender, "_RTCRtpSender__encoder")
        self.assertEqual(encoder.target_bitrate, 1_500_000)


class PeerLifetimeTests(unittest.TestCase):
    def test_failed_and_disconnected_peers_use_their_configured_windows(self):
        common = {
            "now": 100.0,
            "last_seen": 90.0,
            "grace_seconds": 10.0,
            "recover_seconds": 30.0,
            "idle_seconds": 90.0,
        }
        self.assertFalse(
            peer_connection_expired("failed", unhealthy_since=91.0, **common)
        )
        self.assertTrue(
            peer_connection_expired("failed", unhealthy_since=90.0, **common)
        )
        self.assertFalse(
            peer_connection_expired("disconnected", unhealthy_since=71.0, **common)
        )
        self.assertTrue(
            peer_connection_expired("disconnected", unhealthy_since=70.0, **common)
        )
        self.assertFalse(
            peer_connection_expired("connected", unhealthy_since=0.0, **common)
        )


@unittest.skipUnless(WEBRTC_AVAILABLE, "aiortc is not installed")
class WebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_frame_shape_and_timestamps(self):
        import numpy as np

        class Recorder:
            def record(self, frames):
                return np.zeros((frames, 2), dtype=np.float32)

            def read_planar(self):
                return np.zeros((2, 480), dtype=np.int16)

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
                    frame_source=_frame_source,
                    on_control_message=lambda _message: None,
                    on_state_change=lambda _state: None,
                    stun_urls=[],
                    fps=5,
                    audio_enabled=False,
                    audio_sample_rate=24000,
                    audio_frames=1024,
                )
        self.assertTrue(fake.closed)

    async def test_turn_credentials_reach_the_ice_configuration(self):
        entries = [
            {"urls": ["stun:stun.example.org:3478"]},
            {"urls": ["turn:turn.example.org:3478"], "username": "u", "credential": "c"},
        ]
        servers = build_ice_servers(entries)
        self.assertEqual(len(servers), 2)
        self.assertEqual(str(servers[1].urls[0]), "turn:turn.example.org:3478")
        self.assertEqual(servers[1].username, "u")
        self.assertEqual(servers[1].credential, "c")

    async def test_video_track_survives_a_missing_frame(self):
        calls = {"count": 0}

        def source():
            calls["count"] += 1
            if calls["count"] == 2:
                return None
            return Image.new("RGB", (64, 36), "black"), 0.01

        track = DesktopVideoTrack(source, fps=30, min_fps=5)
        first = await track.recv()
        self.assertEqual((first.width, first.height), (64, 36))
        self.assertEqual(track.stats["captureErrors"], 0)
        # A missing capture must produce a placeholder frame, not an exception.
        second = await track.recv()
        self.assertEqual((second.width, second.height), (640, 360))
        self.assertEqual(track.stats["captureErrors"], 1)
        self.assertEqual(track.stats["frames"], 2)

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
            frame_source=_frame_source,
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
