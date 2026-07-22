import unittest

from app.validation import ValidationError, validate_client_message, validate_input_event


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


if __name__ == "__main__":
    unittest.main()