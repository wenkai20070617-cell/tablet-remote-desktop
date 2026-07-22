import unittest
from pathlib import Path

from app.wayland_portal import WaylandPortalSession, is_wayland_session


class WaylandPortalTests(unittest.TestCase):
    def test_keysyms(self):
        self.assertEqual(WaylandPortalSession._keysym("Enter"), 0xFF0D)
        self.assertEqual(WaylandPortalSession._keysym("A"), ord("A"))
        self.assertEqual(WaylandPortalSession._keysym("中"), 0x01000000 | ord("中"))

    def test_windows_is_not_wayland(self):
        self.assertFalse(is_wayland_session())

    def test_unix_fd_negotiation_is_enabled(self):
        source = (Path(__file__).resolve().parents[1] / "app/wayland_portal.py").read_text(encoding="utf-8")
        self.assertIn("MessageBus(negotiate_unix_fd=True)", source)


class WaylandTouchTests(unittest.IsolatedAsyncioTestCase):
    async def test_touch_slots_do_not_collide_after_release(self):
        calls = []

        class Remote:
            async def call_notify_touch_down(self, _session, _options, _node, slot, _x, _y):
                calls.append(("down", slot))

            async def call_notify_touch_motion(self, _session, _options, _node, slot, _x, _y):
                calls.append(("move", slot))

            async def call_notify_touch_up(self, _session, _options, slot):
                calls.append(("up", slot))

        portal = WaylandPortalSession()
        portal.active = True
        portal.remote = Remote()
        portal.session_handle = "/session"
        portal.stream_node = 7

        async def send(raw_id, state):
            await portal.handle_input({
                "kind": "touch",
                "contacts": [{"id": raw_id, "state": state, "x": 0.5, "y": 0.5}],
            })

        await send(1, "down")
        await send(2, "down")
        await send(1, "up")
        await send(3, "down")
        self.assertEqual(calls, [("down", 0), ("down", 1), ("up", 0), ("down", 0)])


if __name__ == "__main__":
    unittest.main()