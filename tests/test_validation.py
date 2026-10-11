import unittest

from app.validation import (
    MAX_CLIPBOARD_LENGTH,
    MAX_PASSWORD_LENGTH,
    ValidationError,
    validate_client_message,
    validate_clipboard_payload,
    validate_input_event,
    validate_password_payload,
    validate_relative_path,
    validate_sha256,
    validate_transfer_id,
    validate_upload_start,
    validate_webrtc_offer,
)


class ValidationTests(unittest.TestCase):
    def test_rejects_non_object_and_non_finite_numbers(self):
        with self.assertRaises(ValidationError):
            validate_client_message([])
        with self.assertRaises(ValidationError):
            validate_input_event({"kind": "move", "x": float("nan"), "y": 0})

    def test_rejects_large_or_unknown_input(self):
        with self.assertRaises(ValidationError):
            validate_input_event({"kind": "mouse_delta", "dx": 5001, "dy": 0})
        with self.assertRaises(ValidationError):
            validate_input_event({"kind": "shell", "command": "whoami"})

    def test_normalizes_supported_input(self):
        event = validate_client_message(
            {
                "type": "input",
                "kind": "key",
                "key": "Enter",
                "ctrl": True,
                "meta": True,
            }
        )
        self.assertEqual(event["type"], "input")
        self.assertEqual(event["kind"], "key")
        self.assertTrue(event["ctrl"])
        self.assertTrue(event["meta"])

    def test_limits_touch_contacts(self):
        contact = {"id": 1, "x": 0.5, "y": 0.5, "state": "down"}
        with self.assertRaises(ValidationError):
            validate_input_event({"kind": "touch", "contacts": [contact] * 11})

    def test_limits_password_payload(self):
        self.assertEqual("valid", validate_password_payload({"password": "valid"}))
        with self.assertRaises(ValidationError):
            validate_password_payload({"password": "x" * (MAX_PASSWORD_LENGTH + 1)})
        with self.assertRaises(ValidationError):
            validate_password_payload({"password": 123})

    def test_limits_clipboard_payload(self):
        self.assertEqual("text", validate_clipboard_payload({"text": "text"}))
        with self.assertRaises(ValidationError):
            validate_clipboard_payload({"text": "x" * (MAX_CLIPBOARD_LENGTH + 1)})
        with self.assertRaises(ValidationError):
            validate_clipboard_payload({"text": None})


class KeyHoldTests(unittest.TestCase):
    def test_key_actions_are_normalised(self):
        for action in ("down", "up", "press"):
            event = validate_input_event({"kind": "key", "key": "Control", "action": action})
            self.assertEqual(action, event["action"])
            self.assertEqual("Control", event["key"])

    def test_an_unknown_key_action_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_input_event({"kind": "key", "key": "a", "action": "hold"})

    def test_modifier_keys_are_accepted(self):
        for key in ("Control", "Shift", "Alt", "Meta", "CapsLock", "F12"):
            self.assertEqual(key, validate_input_event({"kind": "key", "key": key})["key"])

    def test_an_oversized_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_input_event({"kind": "key", "key": "x" * 64})

    def test_the_repeat_flag_is_carried(self):
        event = validate_input_event({"kind": "key", "key": "a", "repeat": True})
        self.assertTrue(event["repeat"])
        self.assertFalse(validate_input_event({"kind": "key", "key": "a"})["repeat"])


class ClientInfoTests(unittest.TestCase):
    def test_viewport_and_monitor_are_normalised(self):
        message = validate_client_message(
            {
                "type": "client_info",
                "viewportWidth": 1280,
                "viewportHeight": 800,
                "displayWidth": 2560,
                "displayHeight": 1600,
                "monitor": 2,
            }
        )
        self.assertEqual(1280, message["viewportWidth"])
        self.assertEqual(2, message["monitor"])

    def test_an_unknown_monitor_index_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_client_message({"type": "client_info", "monitor": 99})

    def test_monitor_selection_is_validated(self):
        self.assertEqual(3, validate_client_message({"type": "monitor_select", "monitor": 3})["monitor"])
        with self.assertRaises(ValidationError):
            validate_client_message({"type": "monitor_select", "monitor": 0})

    def test_ping_carries_a_timestamp(self):
        message = validate_client_message({"type": "ping", "at": 1700000000000})
        self.assertEqual(1700000000000, message["at"])

    def test_client_stats_are_bounded(self):
        message = validate_client_message(
            {"type": "stats", "clientFps": 30, "clientRtt": 42, "clientLoss": 0.02, "mode": "webrtc"}
        )
        self.assertEqual("webrtc", message["mode"])
        with self.assertRaises(ValidationError):
            validate_client_message({"type": "stats", "clientLoss": 5})


class TransferMessageTests(unittest.TestCase):
    def test_transfer_ids_are_restricted_to_url_safe_characters(self):
        self.assertEqual("abc-123_XY", validate_transfer_id("abc-123_XY"))
        for value in ("", "a b", "../etc", "a/b", "x" * 200, 12, None):
            with self.assertRaises(ValidationError, msg=repr(value)):
                validate_transfer_id(value)

    def test_checksums_must_be_hexadecimal_sha256(self):
        digest = "a" * 64
        self.assertEqual(digest, validate_sha256(digest.upper()))
        self.assertEqual("", validate_sha256(""))
        self.assertEqual("", validate_sha256(None))
        for value in ("abc", "z" * 64, "a" * 63):
            with self.assertRaises(ValidationError, msg=value):
                validate_sha256(value)

    def test_upload_start_requires_a_name_and_a_size(self):
        payload = validate_upload_start({"name": "report.pdf", "size": 1024, "relative": "docs/report.pdf"})
        self.assertEqual("report.pdf", payload["name"])
        self.assertEqual("docs/report.pdf", payload["relative"])
        self.assertFalse(payload["overwrite"])
        with self.assertRaises(ValidationError):
            validate_upload_start({"name": "", "size": 1})
        with self.assertRaises(ValidationError):
            validate_upload_start({"name": "a", "size": -1})

    def test_relative_paths_reject_null_bytes(self):
        self.assertEqual("docs/a.txt", validate_relative_path("docs/a.txt"))
        self.assertEqual("", validate_relative_path(None))
        with self.assertRaises(ValidationError):
            validate_relative_path("a\x00b")
        with self.assertRaises(ValidationError):
            validate_relative_path("x" * 2000)

    def test_transfer_progress_and_cancel_are_validated(self):
        progress = validate_client_message(
            {
                "type": "transfer_progress",
                "transferId": "abc",
                "received": 10,
                "total": 100,
                "direction": "upload",
                "name": "a.bin",
            }
        )
        self.assertEqual(10, progress["received"])
        cancel = validate_client_message(
            {"type": "transfer_cancel", "transferId": "abc", "removePartial": True}
        )
        self.assertTrue(cancel["removePartial"])


class OfferValidationTests(unittest.TestCase):
    def test_a_valid_offer_is_normalised(self):
        payload = validate_webrtc_offer(
            {"type": "offer", "sdp": "v=0\r\n", "viewportWidth": 1600, "viewportHeight": 900, "monitor": 2}
        )
        self.assertEqual("offer", payload["type"])
        self.assertEqual(1600, payload["viewportWidth"])
        self.assertEqual(2, payload["monitor"])
        self.assertEqual("", payload["peerId"])

    def test_an_oversized_or_mistyped_offer_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_webrtc_offer({"type": "answer", "sdp": "v=0"})
        with self.assertRaises(ValidationError):
            validate_webrtc_offer({"type": "offer", "sdp": ""})
        with self.assertRaises(ValidationError):
            validate_webrtc_offer({"type": "offer", "sdp": "x" * 200_001})
        with self.assertRaises(ValidationError):
            validate_webrtc_offer({"type": "offer", "sdp": "v=0", "peerId": "y" * 80})

    def test_viewport_bounds_are_enforced(self):
        with self.assertRaises(ValidationError):
            validate_webrtc_offer({"type": "offer", "sdp": "v=0", "viewportWidth": 10})


if __name__ == "__main__":
    unittest.main()
