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
        self.assertIn("hostStatus.stunUrls", script)
        self.assertNotIn("stun:stun.cloudflare.com:3478", script)
        self.assertIn("lastRtcFrameAt", script)
        self.assertNotIn("socket.send(JSON.stringify({ type: \"client_info\"", script.split("function connectWebSocket", 1)[0])

    def test_turn_configuration_is_consumed_without_hardcoding_it(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("iceServers", script)
        self.assertIn("iceServerList", script)
        # Credentials must come from the host, never from the page.
        self.assertNotIn("turn_credential", script)
        self.assertNotIn("turn_secret", script)

    def test_reliable_and_lossy_control_channels_are_separate(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn('createDataChannel("control"', script)
        self.assertIn('createDataChannel("motion"', script)
        self.assertIn("maxRetransmits: 0", script)
        self.assertIn("bufferedAmount", script)
        self.assertIn("RELIABLE_BYTES_SOFT_LIMIT", script)

    def test_reconnection_and_recovery_paths_are_present(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("restartIce", script)
        self.assertIn("recoverWebRTC", script)
        self.assertIn("scheduleWebRTCRecovery", script)
        self.assertIn("/api/webrtc/recover", script)
        self.assertIn("网络抖动，保持WebRTC", script)

    def test_office_features_have_ui_hooks(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        for token in (
            "remoteCursor",
            "panToggle",
            "monitorButton",
            "filePicker",
            "transferList",
            "metricsHud",
            "decodeAdpcm",
            "clipboard_poll",
        ):
            self.assertIn(token, script + html, token)

    def test_file_chunks_are_hashed_and_delete_uses_the_declared_route(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        server = (ROOT / "app/server.py").read_text(encoding="utf-8")
        self.assertIn('crypto.subtle.digest("SHA-256"', script)
        self.assertIn('"X-Chunk-SHA256"', script)
        self.assertIn("verifyResumePrefix", script)
        self.assertIn("resumeHashes", script)
        self.assertIn('/api/files/delete?', script)
        self.assertIn('app.router.add_delete("/api/files/delete", files_delete)', server)

    def test_touch_updates_use_the_lossy_channel(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn('contact?.state !== "update"', script)

    def test_session_expiry_and_backoff_recovery_are_present(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("sessionIsAuthenticated", script)
        self.assertIn("scheduleMainReconnect", script)
        self.assertIn("reconnectDelay", script)
        self.assertIn("登录已过期，请重新输入密码", script)

    def test_clipboard_has_http_fallback(self):
        script = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("navigator.clipboard?.writeText", script)
        self.assertIn("navigator.clipboard?.readText", script)
        self.assertIn("window.prompt", script)
        self.assertIn('connection.connectionState !== "connected"', script)
        self.assertIn("if (x < 0 || x > 1 || y < 0 || y > 1) return null", script)


if __name__ == "__main__":
    unittest.main()
