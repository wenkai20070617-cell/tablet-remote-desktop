import unittest

from app.input_state import (
    INPUT_MOTION_KINDS,
    INPUT_RELIABLE_KINDS,
    input_event_is_reliable,
    key_token,
    new_input_state,
    reset_input_state,
)


class ReliableInputTests(unittest.TestCase):
    def test_state_changing_events_are_reliable(self):
        for kind in ("down", "up", "click", "double", "mouse_button", "key", "touch", "wheel"):
            self.assertIn(kind, INPUT_RELIABLE_KINDS, kind)

    def test_pointer_motion_is_lossy(self):
        for kind in ("move", "mouse_delta"):
            self.assertIn(kind, INPUT_MOTION_KINDS, kind)
            self.assertNotIn(kind, INPUT_RELIABLE_KINDS, kind)

    def test_no_event_is_both(self):
        self.assertEqual(set(), INPUT_RELIABLE_KINDS & INPUT_MOTION_KINDS)

    def test_touch_updates_are_lossy_but_state_changes_are_reliable(self):
        self.assertFalse(
            input_event_is_reliable(
                {"kind": "touch", "contacts": [{"id": 1, "state": "update"}]}
            )
        )
        for state in ("down", "up", "cancel"):
            self.assertTrue(
                input_event_is_reliable(
                    {"kind": "touch", "contacts": [{"id": 1, "state": state}]}
                ),
                state,
            )


class KeyTokenTests(unittest.TestCase):
    def test_named_keys_are_normalised(self):
        self.assertEqual(key_token({"key": "Enter"}), "enter")
        self.assertEqual(key_token({"key": "Escape"}), "esc")
        self.assertEqual(key_token({"key": " "}), "space")
        self.assertEqual(key_token({"key": "ArrowLeft"}), "left")

    def test_modifiers_are_normalised(self):
        self.assertEqual(key_token({"key": "Control"}), "ctrl")
        self.assertEqual(key_token({"key": "Shift"}), "shift")
        self.assertEqual(key_token({"key": "Meta"}), "win")
        self.assertEqual(key_token({"key": "Alt"}), "alt")

    def test_printable_keys_are_lowercased(self):
        self.assertEqual(key_token({"key": "A"}), "a")
        self.assertEqual(key_token({"key": "7"}), "7")

    def test_missing_key_is_rejected(self):
        self.assertIsNone(key_token({"key": ""}))


class InputStateTests(unittest.TestCase):
    def test_a_new_state_holds_nothing_down(self):
        state = new_input_state(1280)
        self.assertEqual(state["viewport_width"], 1280)
        self.assertEqual(set(), state["buttons_down"])
        self.assertEqual(set(), state["keys_down"])
        self.assertEqual({}, state["touch_id_map"])

    def test_reset_clears_every_latched_value(self):
        state = new_input_state()
        state["buttons_down"].add("left")
        state["keys_down"].update({"ctrl", "a"})
        state["touch_id_map"][1] = 0
        state["touch_points"][1] = (0.5, 0.5)
        state["generic_touch_down"] = True
        reset_input_state(state)
        self.assertEqual(set(), state["buttons_down"])
        self.assertEqual(set(), state["keys_down"])
        self.assertEqual({}, state["touch_id_map"])
        self.assertEqual({}, state["touch_points"])
        self.assertFalse(state["generic_touch_down"])


if __name__ == "__main__":
    unittest.main()
