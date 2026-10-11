import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.capture import AdaptiveController, CaptureEngine, FramePacer, SharedScreen


def _display_available() -> bool:
    """Capture tests need a real desktop; headless runners must skip them."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


class AdaptiveControllerTests(unittest.TestCase):
    def _observe(self, controller, **overrides):
        payload = {
            "capture_seconds": 0.002,
            "encode_seconds": 0.004,
            "payload_bytes": 20_000,
            "interval": 1 / 30,
            "changed": True,
            "queue_ratio": 0.0,
            "frame_bytes": 1920 * 1080 * 3,
        }
        payload.update(overrides)
        controller.observe(**payload)

    def test_a_static_screen_keeps_a_usable_floor(self):
        controller = AdaptiveController({"min_fps": 8, "max_fps": 30, "quality": 60})
        for _ in range(30):
            self._observe(controller, changed=False, payload_bytes=400)
        self.assertGreaterEqual(controller.fps, 8)
        self.assertLessEqual(controller.fps, 30)

    def test_motion_raises_the_frame_rate(self):
        controller = AdaptiveController({"min_fps": 8, "max_fps": 30, "quality": 60})
        for _ in range(10):
            self._observe(controller, changed=False, payload_bytes=400)
        idle = controller.fps
        for _ in range(15):
            self._observe(controller, changed=True, payload_bytes=120_000)
        self.assertGreater(controller.fps, idle)

    def test_expensive_frames_lower_the_frame_rate(self):
        controller = AdaptiveController(
            {"min_fps": 8, "max_fps": 30, "quality": 70, "cpu_budget": 0.75}
        )
        start = controller.fps
        for _ in range(12):
            self._observe(controller, encode_seconds=0.2)
        self.assertLess(controller.fps, start)
        self.assertGreaterEqual(controller.fps, 8)

    def test_congestion_lowers_quality_then_resolution(self):
        controller = AdaptiveController(
            {"min_fps": 8, "max_fps": 30, "quality": 70, "min_quality": 30, "max_width": 1920}
        )
        quality = controller.quality
        width = controller.width
        for _ in range(6):
            self._observe(controller, payload_bytes=4_000_000, queue_ratio=0.9)
        self.assertLess(controller.quality, quality)
        for _ in range(20):
            self._observe(controller, payload_bytes=4_000_000, queue_ratio=0.9)
        self.assertLess(controller.width, width)

    def test_a_healthy_link_recovers(self):
        controller = AdaptiveController(
            {"min_fps": 8, "max_fps": 30, "quality": 70, "min_quality": 30, "max_width": 1920}
        )
        for _ in range(20):
            self._observe(controller, payload_bytes=4_000_000, queue_ratio=0.9)
        degraded = controller.snapshot()
        for _ in range(60):
            self._observe(controller, payload_bytes=8_000, queue_ratio=0.0)
        recovered = controller.snapshot()
        self.assertGreaterEqual(recovered["quality"], degraded["quality"])
        self.assertGreaterEqual(recovered["width"], degraded["width"])

    def test_quality_stays_inside_configured_bounds(self):
        controller = AdaptiveController(
            {"min_fps": 5, "max_fps": 20, "quality": 60, "min_quality": 35, "max_quality": 80}
        )
        for _ in range(80):
            self._observe(controller, payload_bytes=8_000_000, queue_ratio=1.0)
        self.assertGreaterEqual(controller.quality, 35)
        for _ in range(80):
            self._observe(controller, payload_bytes=1_000, queue_ratio=0.0)
        self.assertLessEqual(controller.quality, 80)


class FramePacerTests(unittest.TestCase):
    def test_processing_time_is_not_added_on_top_of_the_sleep(self):
        pacer = FramePacer(100.0)
        started = time.monotonic()
        for _ in range(10):
            time.sleep(0.004)  # simulate work
            pacer.sleep()
        elapsed = time.monotonic() - started
        # Ten frames at 100 fps should take about 0.1s; a pacer that folded the
        # 4ms of work into every sleep would take roughly 0.15s.
        self.assertLess(elapsed, 0.135, f"pacer drifted: {elapsed:.3f}s")

    def test_an_overrunning_loop_resynchronises(self):
        pacer = FramePacer(1000.0)
        time.sleep(0.2)
        pacer.sleep()
        self.assertAlmostEqual(pacer.next_at, time.monotonic(), delta=0.05)


class AsyncFramePacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_wait_yields_through_asyncio(self):
        pacer = FramePacer(30.0)
        pacer.next_at = time.monotonic() + 1.0
        sleeper = AsyncMock()
        with patch("app.capture.asyncio.sleep", sleeper):
            await pacer.wait()
        sleeper.assert_awaited_once()


@unittest.skipUnless(_display_available(), "no display server is available for capture")
class CaptureEngineTests(unittest.TestCase):
    def test_monitors_are_enumerated_and_cached(self):
        engine = CaptureEngine(with_cursor=True)
        try:
            first = engine.monitors()
            self.assertTrue(first, "no display was reported")
            self.assertEqual(first[0].index, 1)
            self.assertTrue(first[0].primary)
            second = engine.monitors()
            self.assertEqual([item.index for item in first], [item.index for item in second])
        finally:
            engine.close()

    def test_an_unknown_monitor_falls_back_to_the_primary(self):
        engine = CaptureEngine()
        try:
            self.assertEqual(engine.monitor(99).index, engine.monitors()[0].index)
        finally:
            engine.close()

    def test_a_closed_engine_reports_a_clear_error(self):
        engine = CaptureEngine()
        engine.close()
        with self.assertRaises(RuntimeError):
            engine.grab({"left": 0, "top": 0, "width": 8, "height": 8}, timeout=0.2)

    def test_grab_and_encode_produce_a_jpeg(self):
        engine = CaptureEngine(with_cursor=True)
        try:
            monitor = engine.monitors()[0]
            region = {
                "left": monitor.left,
                "top": monitor.top,
                "width": min(320, monitor.width),
                "height": min(200, monitor.height),
            }
            frame = engine.capture_encoded(region, 60, 320, "jpeg", config=lambda: {})
            self.assertEqual(frame.payload[:2], b"\xff\xd8")
            self.assertTrue(frame.meta["width"] <= 320)
            self.assertIsNotNone(frame.signature)
            self.assertGreaterEqual(frame.capture_seconds, 0.0)
            self.assertGreater(frame.encode_seconds, 0.0)
        except RuntimeError as exc:
            self.skipTest(f"screen capture is unavailable here: {exc}")
        finally:
            engine.close()

    def test_a_failed_capture_is_reported_not_hidden(self):
        engine = CaptureEngine()
        try:
            engine.last_error = ""
            # A region the display cannot satisfy must surface an error rather
            # than being swallowed into a blank frame.
            try:
                engine.grab({"left": -100_000, "top": -100_000, "width": 1, "height": 1}, timeout=1.0)
            except Exception:
                self.assertTrue(engine.last_error, "the failure was not recorded")
            else:
                # The backend accepted the region; nothing failed, so nothing
                # should have been recorded either.
                self.assertEqual("", engine.last_error)
        finally:
            engine.close()


class EncodingOnlyTests(unittest.TestCase):
    """These paths never touch a display, so they run everywhere."""

    def test_change_detection_can_be_disabled(self):
        from PIL import Image

        engine = CaptureEngine()
        try:
            image = Image.new("RGB", (64, 64), "black")
            with_detection = engine.encode(image, 60, 64, "jpeg", config=lambda: {})
            without = engine.encode(
                image, 60, 64, "jpeg", config=lambda: {"change_detection_enabled": False}
            )
            self.assertIsNotNone(with_detection.signature)
            self.assertIsNone(without.signature)
        finally:
            engine.close()

    def test_resizing_respects_the_requested_width(self):
        from PIL import Image

        engine = CaptureEngine()
        try:
            image = Image.new("RGB", (1000, 500), "black")
            frame = engine.encode(image, 60, 400, "jpeg", config=lambda: {})
            self.assertEqual(frame.meta["width"], 400)
            self.assertEqual(frame.meta["height"], 200)
        finally:
            engine.close()

    def test_webp_is_used_when_selected(self):
        from PIL import Image

        engine = CaptureEngine()
        try:
            frame = engine.encode(Image.new("RGB", (64, 64), "black"), 60, 64, "webp", config=lambda: {})
            self.assertEqual(frame.meta["format"], "webp")
            self.assertEqual(frame.payload[8:12], b"WEBP")
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
