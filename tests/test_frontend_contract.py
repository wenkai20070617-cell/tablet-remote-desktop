import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    def test_all_queried_ids_exist(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        queried = set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', script))
        self.assertEqual(set(), queried - ids)

    def test_webrtc_and_websocket_fallback_are_present(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("RTCPeerConnection", script)
        self.assertIn("fallbackToWebSocket", script)
        self.assertNotIn("socket.send(JSON.stringify({ type: \"client_info\"", script.split("function connectWebSocket", 1)[0])

    def test_clipboard_has_http_fallback(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("navigator.clipboard?.writeText", script)
        self.assertIn("navigator.clipboard?.readText", script)
        self.assertIn("window.prompt", script)
        self.assertIn('connection.connectionState !== "connected"', script)
        self.assertIn("if (x < 0 || x > 1 || y < 0 || y > 1) return null", script)


if __name__ == "__main__":
    unittest.main()