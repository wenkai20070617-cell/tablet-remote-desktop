import contextlib
import json
import shutil
import unittest
import uuid
from pathlib import Path

from app import config as config_module
from app.config import (
    DEFAULTS,
    _normalize_config,
    client_ice_servers,
    turn_credentials,
)

SCRATCH = Path(__file__).resolve().parent / "_scratch"


@contextlib.contextmanager
def scratch_dir():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _base(**overrides):
    merged = dict(DEFAULTS)
    merged.update(password_salt="00" * 16, password_hash="ab" * 32)
    merged.update(overrides)
    return merged


class DefaultsTests(unittest.TestCase):
    def test_the_default_path_needs_no_paid_relay(self):
        normalized = _normalize_config(_base())
        self.assertEqual([], normalized["turn_urls"])
        self.assertEqual([], normalized["ice_servers"])
        servers = client_ice_servers(normalized)
        self.assertEqual(1, len(servers))
        self.assertTrue(str(servers[0]["urls"][0]).startswith("stun:"))
        self.assertNotIn("username", servers[0])

    def test_turn_is_off_unless_configured(self):
        normalized = _normalize_config(_base())
        self.assertIsNone(turn_credentials(normalized))
        self.assertFalse(normalized["turn_secret"])

    def test_legacy_keys_are_still_accepted(self):
        normalized = _normalize_config(
            _base(image_format="webp", audio_sample_rate=24000, audio_frames=1024)
        )
        self.assertEqual("webp", normalized["image_format"])
        self.assertEqual(24000, normalized["audio_sample_rate"])
        self.assertEqual(1024, normalized["audio_frames"])


class TurnTests(unittest.TestCase):
    def test_static_credentials_are_passed_through(self):
        normalized = _normalize_config(
            _base(
                turn_urls=["turn:turn.example.org:3478"],
                turn_username="user",
                turn_credential="secret",
            )
        )
        servers = client_ice_servers(normalized)
        turn = [entry for entry in servers if str(entry["urls"][0]).startswith("turn:")]
        self.assertEqual(1, len(turn))
        self.assertEqual("user", turn[0]["username"])
        self.assertEqual("secret", turn[0]["credential"])

    def test_ephemeral_credentials_are_generated_from_a_secret(self):
        normalized = _normalize_config(
            _base(turn_urls=["turns:turn.example.org:5349"], turn_secret="shared-secret")
        )
        first = turn_credentials(normalized)
        self.assertIsNotNone(first)
        username, credential = first
        self.assertTrue(username.split(":")[0].isdigit())
        self.assertTrue(credential)
        servers = client_ice_servers(normalized)
        relay = [entry for entry in servers if str(entry["urls"][0]).startswith("turns:")]
        self.assertEqual(1, len(relay))
        self.assertEqual(username, relay[0]["username"])
        self.assertEqual(credential, relay[0]["credential"])

    def test_the_ephemeral_username_carries_its_expiry(self):
        normalized = _normalize_config(
            _base(turn_urls=["turns:turn.example.org:5349"], turn_secret="s", turn_key_id="key1")
        )
        username, credential = turn_credentials(normalized, ttl=120)
        expiry, _, key_id = username.partition(":")
        self.assertEqual("key1", key_id)
        self.assertGreater(int(expiry), 0)
        self.assertTrue(credential)

    def test_a_secret_without_urls_is_refused(self):
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(turn_secret="secret"))

    def test_a_secret_and_static_credentials_cannot_be_combined(self):
        with self.assertRaises(RuntimeError):
            _normalize_config(
                _base(
                    turn_urls=["turn:turn.example.org:3478"],
                    turn_secret="secret",
                    turn_username="user",
                    turn_credential="pass",
                )
            )

    def test_half_a_credential_pair_is_refused(self):
        with self.assertRaises(RuntimeError):
            _normalize_config(
                _base(turn_urls=["turn:turn.example.org:3478"], turn_username="user")
            )

    def test_turn_urls_must_use_the_turn_scheme(self):
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(turn_urls=["stun:not-a-turn.example.org:3478"]))

    def test_explicit_ice_servers_are_honoured(self):
        normalized = _normalize_config(
            _base(
                ice_servers=[
                    {"urls": ["turns:relay.example.org:5349"], "username": "u", "credential": "c"}
                ]
            )
        )
        servers = client_ice_servers(normalized)
        relay = [entry for entry in servers if str(entry["urls"][0]).startswith("turns:")]
        self.assertEqual(1, len(relay))
        self.assertEqual("u", relay[0]["username"])

    def test_invalid_ice_server_entries_are_refused(self):
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(ice_servers=[{"urls": ["http://example.org"]}]))
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(ice_servers=["turn:example.org"]))


class RangeTests(unittest.TestCase):
    def test_out_of_range_values_are_refused(self):
        for key, value in (
            ("fps", 0),
            ("fps", 500),
            ("min_fps", 0),
            ("webrtc_fps", 0),
            ("cpu_budget", 2.0),
            ("audio_sample_rate", 1000),
            ("file_chunk_size", 100),
            ("monitor", 0),
            ("max_connections_per_session", 100),
        ):
            with self.assertRaises(RuntimeError, msg=key):
                _normalize_config(_base(**{key: value}))

    def test_booleans_are_required_for_flags(self):
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(clipboard_sync_enabled="yes"))

    def test_the_audio_codec_is_validated(self):
        self.assertEqual("adpcm", _normalize_config(_base(audio_codec="adpcm"))["audio_codec"])
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(audio_codec="mp3"))

    def test_the_ice_transport_policy_is_validated(self):
        self.assertEqual("relay", _normalize_config(_base(ice_transport_policy="relay"))["ice_transport_policy"])
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(ice_transport_policy="direct"))

    def test_watermarks_must_be_ordered(self):
        with self.assertRaises(RuntimeError):
            _normalize_config(_base(send_queue_low_water=8, send_queue_high_water=4))

    def test_the_frame_rate_floor_follows_the_ceiling(self):
        normalized = _normalize_config(_base(fps=3, min_fps=20))
        self.assertLessEqual(normalized["min_fps"], normalized["fps"])

    def test_the_bitrate_floor_follows_the_start(self):
        normalized = _normalize_config(
            _base(webrtc_bitrate_min=9_000_000, webrtc_bitrate_start=1_000_000)
        )
        self.assertLessEqual(normalized["webrtc_bitrate_min"], normalized["webrtc_bitrate_start"])
        self.assertLessEqual(
            normalized["webrtc_bitrate_start"], normalized["webrtc_bitrate_max"]
        )


class LoadConfigTests(unittest.TestCase):
    def test_an_existing_config_is_upgraded_with_new_defaults(self):
        with scratch_dir() as temporary:
            path = Path(temporary) / "config.json"
            legacy = {
                "bind": "127.0.0.1",
                "port": 18765,
                "password_salt": "00" * 16,
                "password_hash": "ab" * 32,
                "audio_frames": 1024,
                "session_ttl_seconds": 900,
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")
            original = config_module.CONFIG_PATH
            config_module.CONFIG_PATH = path
            try:
                loaded, generated = config_module.load_config()
            finally:
                config_module.CONFIG_PATH = original
            self.assertIsNone(generated)
            # New keys appear, old values survive, and the legacy key is mapped.
            self.assertIn("file_chunk_size", loaded)
            self.assertEqual(1024, loaded["audio_frames"])
            self.assertEqual(900, loaded["session_idle_ttl_seconds"])
            self.assertEqual([], loaded["turn_urls"])
            self.assertTrue(loaded["ice_servers_normalized"])

    def test_a_missing_config_generates_a_password(self):
        with scratch_dir() as temporary:
            path = Path(temporary) / "fresh.json"
            original = config_module.CONFIG_PATH
            config_module.CONFIG_PATH = path
            try:
                loaded, generated = config_module.load_config()
            finally:
                config_module.CONFIG_PATH = original
            self.assertTrue(generated)
            self.assertTrue(path.exists())
            self.assertTrue(loaded["password_hash"])
            self.assertEqual(generated, generated.strip())


if __name__ == "__main__":
    unittest.main()
