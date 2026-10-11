from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import secrets
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULTS: dict[str, Any] = {
    "bind": "127.0.0.1",
    "port": 18765,
    # --- fallback JPEG/WebSocket channel ---------------------------------
    "fps": 15,
    "image_format": "jpeg",
    "jpeg_quality": 58,
    "max_width": 2560,
    "min_fps": 5,
    "min_jpeg_quality": 28,
    "min_width": 960,
    "target_bytes_per_frame": 260_000,
    "cpu_budget": 0.75,
    "frame_send_timeout_seconds": 2.5,
    "send_queue_high_water": 4,
    "send_queue_low_water": 1,
    "interactive_fps": 15,
    "interactive_jpeg_quality": 40,
    "interactive_max_width": 1280,
    "active_seconds": 1.2,
    "settle_seconds": 3.5,
    "preview_seconds": 8.0,
    # The screen is sampled continuously; these only govern how often an
    # unchanged frame is re-sent, never how often the desktop is captured.
    "unchanged_preview_interval": 2.0,
    "unchanged_full_interval": 4.0,
    "capture_serialize": True,
    # --- capture ---------------------------------------------------------
    "monitor": 1,
    "cursor_enabled": True,
    "cursor_interval_seconds": 0.1,
    # --- change detection ------------------------------------------------
    "change_detection_enabled": True,
    "change_thumbnail_width": 96,
    "change_thumbnail_height": 60,
    # --- WebRTC ----------------------------------------------------------
    "webrtc_enabled": True,
    "webrtc_fps": 30,
    "webrtc_min_fps": 8,
    "webrtc_bitrate_min": 400_000,
    "webrtc_bitrate_start": 2_500_000,
    "webrtc_bitrate_max": 8_000_000,
    "webrtc_max_width": 1920,
    "ice_transport_policy": "all",
    "stun_urls": ["stun:stun.cloudflare.com:3478"],
    # TURN is opt-in.  The default zero-cost path uses STUN plus the
    # WebSocket fallback; a TURN relay is only contacted when configured.
    "turn_urls": [],
    "turn_username": "",
    "turn_credential": "",
    "turn_secret": "",
    "turn_key_id": "",
    "turn_credential_ttl_seconds": 3600,
    "ice_servers": [],
    # --- input -----------------------------------------------------------
    "input_reliable_queue": 512,
    "input_motion_queue": 64,
    "input_message_rate": 600.0,
    "input_message_burst": 1200.0,
    "input_motion_bytes_limit": 262_144,
    "input_reliable_bytes_limit": 1_048_576,
    # --- reconnection ----------------------------------------------------
    "webrtc_grace_seconds": 10.0,
    "webrtc_recover_seconds": 30.0,
    "peer_idle_timeout_seconds": 90.0,
    # --- clipboard -------------------------------------------------------
    "clipboard_sync_enabled": True,
    "clipboard_poll_seconds": 1.5,
    # --- files -----------------------------------------------------------
    "file_transfer_enabled": True,
    "file_upload_root": "",
    "file_download_root": "",
    "file_chunk_size": 1_048_576,
    "file_max_size": 4_294_967_296,
    "file_reserve_free_bytes": 268_435_456,
    # --- audio -----------------------------------------------------------
    "audio_enabled": True,
    "audio_sample_rate": 48000,
    "audio_frames": 960,
    "audio_bitrate": 64_000,
    "audio_device": "",
    "audio_codec": "adpcm",
    "audio_jitter_seconds": 0.18,
    # --- sessions --------------------------------------------------------
    "session_idle_ttl_seconds": 3600,
    "session_absolute_ttl_seconds": 43200,
    "max_sessions": 32,
    "max_connections_per_session": 6,
    "login_attempts": 5,
    "login_window_seconds": 60,
    "login_lockout_seconds": 300,
    "allowed_origins": [],
    "allowed_hosts": [],
}


def _default_config_path() -> Path:
    explicit = os.environ.get("TRD_CONFIG_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if LEGACY_CONFIG_PATH.exists():
        return LEGACY_CONFIG_PATH
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "TabletRemoteDesktop" / "config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "TabletRemoteDesktop" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "tablet-remote-desktop" / "config.json"


CONFIG_PATH = _default_config_path()


def hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 250_000
    )
    return digest.hex()


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    if os.name != "nt":
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
    temporary.replace(path)


def save_config(config: dict[str, Any]) -> None:
    _write_private_json(CONFIG_PATH, config)


def _bounded_int(config: dict[str, Any], key: str, low: int, high: int) -> int:
    value = config.get(key)
    if isinstance(value, bool):
        raise RuntimeError(f"Configuration value {key} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Configuration value {key} must be an integer") from exc
    if not low <= number <= high:
        raise RuntimeError(f"Configuration value {key} must be from {low} through {high}")
    return number


def _bounded_float(config: dict[str, Any], key: str, low: float, high: float) -> float:
    value = config.get(key)
    if isinstance(value, bool):
        raise RuntimeError(f"Configuration value {key} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Configuration value {key} must be numeric") from exc
    if not low <= number <= high:
        raise RuntimeError(f"Configuration value {key} must be from {low} through {high}")
    return number


def _text(config: dict[str, Any], key: str, limit: int = 2048) -> str:
    value = config.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise RuntimeError(f"Configuration value {key} must be a string of at most {limit} characters")
    return value


def _ice_servers(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise STUN/TURN configuration into an ordered ICE server list."""
    entries: list[dict[str, Any]] = []
    stun = [item.strip() for item in config["stun_urls"] if item.strip()]
    if stun:
        entries.append({"urls": stun})
    turn = [item.strip() for item in config["turn_urls"] if item.strip()]
    for item in turn:
        if not item.startswith(("turn:", "turns:")):
            raise RuntimeError("Every turn_urls entry must use turn: or turns:")
    if turn:
        username = config["turn_username"]
        credential = config["turn_credential"]
        if config["turn_secret"] and not username and not credential:
            # RFC 7635 style ephemeral credentials: the URL is registered here
            # and the username/credential pair is minted per client request.
            entries.append({"urls": turn})
        elif username and credential:
            entries.append({"urls": turn, "username": username, "credential": credential})
        elif username or credential:
            raise RuntimeError("turn_username and turn_credential must be set together")
        else:
            # Anonymous TURN is unusual but legal for a private relay.
            entries.append({"urls": turn})
    for raw in config["ice_servers"]:
        if not isinstance(raw, dict):
            raise RuntimeError("Every ice_servers entry must be an object")
        urls = raw.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls:
            raise RuntimeError("Every ice_servers entry needs a urls list")
        cleaned = []
        for item in urls:
            if not isinstance(item, str) or not item.startswith(("stun:", "stuns:", "turn:", "turns:")):
                raise RuntimeError("ICE server URLs must use stun:, stuns:, turn: or turns:")
            if len(item) > 2048:
                raise RuntimeError("ICE server URL is too long")
            cleaned.append(item)
        entry: dict[str, Any] = {"urls": cleaned}
        username = raw.get("username")
        credential = raw.get("credential")
        if username is not None:
            if not isinstance(username, str) or len(username) > 512:
                raise RuntimeError("ICE server username is invalid")
            entry["username"] = username
        if credential is not None:
            if not isinstance(credential, str) or len(credential) > 512:
                raise RuntimeError("ICE server credential is invalid")
            entry["credential"] = credential
        entries.append(entry)
    return entries


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    if not isinstance(normalized.get("bind"), str) or not normalized["bind"].strip():
        raise RuntimeError("Configuration value bind must be a non-empty string")
    normalized["bind"] = normalized["bind"].strip()
    int_ranges = {
        "port": (1, 65535),
        "fps": (1, 60),
        "jpeg_quality": (1, 95),
        "max_width": (640, 8192),
        "min_fps": (1, 60),
        "min_jpeg_quality": (1, 90),
        "min_width": (320, 8192),
        "target_bytes_per_frame": (16_384, 16_777_216),
        "send_queue_high_water": (1, 256),
        "send_queue_low_water": (0, 128),
        "interactive_fps": (1, 60),
        "interactive_jpeg_quality": (1, 95),
        "interactive_max_width": (640, 8192),
        "change_thumbnail_width": (16, 1024),
        "change_thumbnail_height": (16, 1024),
        "monitor": (1, 32),
        "webrtc_fps": (1, 60),
        "webrtc_min_fps": (1, 60),
        "webrtc_bitrate_min": (50_000, 100_000_000),
        "webrtc_bitrate_start": (50_000, 100_000_000),
        "webrtc_bitrate_max": (50_000, 100_000_000),
        "webrtc_max_width": (640, 8192),
        "turn_credential_ttl_seconds": (60, 604_800),
        "input_reliable_queue": (8, 8192),
        "input_motion_queue": (1, 4096),
        "input_message_rate": (10.0, 20_000.0),
        "input_message_burst": (10.0, 40_000.0),
        "input_motion_bytes_limit": (4096, 16_777_216),
        "input_reliable_bytes_limit": (4096, 67_108_864),
        "clipboard_poll_seconds": (0.25, 60.0),
        "file_chunk_size": (16_384, 16_777_216),
        "file_max_size": (1_048_576, 1_099_511_627_776),
        "file_reserve_free_bytes": (0, 1_099_511_627_776),
        "audio_sample_rate": (8000, 96000),
        "audio_frames": (128, 8192),
        "audio_bitrate": (8_000, 510_000),
        "session_idle_ttl_seconds": (60, 604800),
        "session_absolute_ttl_seconds": (60, 2592000),
        "max_sessions": (1, 256),
        "max_connections_per_session": (1, 16),
        "login_attempts": (1, 100),
        "login_window_seconds": (1, 3600),
        "login_lockout_seconds": (1, 86400),
    }
    for key, (low, high) in int_ranges.items():
        normalized[key] = _bounded_int(normalized, key, low, high)
    float_ranges = {
        "cpu_budget": (0.1, 1.0),
        "cursor_interval_seconds": (0.02, 2.0),
        "frame_send_timeout_seconds": (0.1, 30.0),
        "active_seconds": (0.1, 60.0),
        "settle_seconds": (0.1, 60.0),
        "preview_seconds": (0.1, 120.0),
        "unchanged_preview_interval": (0.1, 120.0),
        "unchanged_full_interval": (0.1, 600.0),
        "webrtc_grace_seconds": (1.0, 120.0),
        "webrtc_recover_seconds": (2.0, 600.0),
        "peer_idle_timeout_seconds": (10.0, 3600.0),
        "audio_jitter_seconds": (0.02, 2.0),
    }
    for key, (low, high) in float_ranges.items():
        normalized[key] = _bounded_float(normalized, key, low, high)
    for key in (
        "change_detection_enabled",
        "audio_enabled",
        "webrtc_enabled",
        "clipboard_sync_enabled",
        "file_transfer_enabled",
        "capture_serialize",
        "cursor_enabled",
    ):
        if not isinstance(normalized.get(key), bool):
            raise RuntimeError(f"Configuration value {key} must be true or false")
    if normalized.get("image_format") not in {"jpeg", "webp"}:
        raise RuntimeError("Configuration value image_format must be jpeg or webp")
    if normalized.get("audio_codec") not in {"adpcm", "pcm"}:
        raise RuntimeError("Configuration value audio_codec must be adpcm or pcm")
    if normalized.get("ice_transport_policy") not in {"all", "relay"}:
        raise RuntimeError("Configuration value ice_transport_policy must be all or relay")
    for key in ("stun_urls", "turn_urls", "allowed_origins", "allowed_hosts"):
        value = normalized.get(key)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or len(item) > 2048 for item in value
        ):
            raise RuntimeError(f"Configuration value {key} must be a list of strings")
    if any(not item.startswith(("stun:", "stuns:")) for item in normalized["stun_urls"]):
        raise RuntimeError("Every stun_urls entry must use stun: or stuns:")
    for key in (
        "turn_username",
        "turn_credential",
        "turn_secret",
        "turn_key_id",
        "audio_device",
        "file_upload_root",
        "file_download_root",
    ):
        normalized[key] = _text(normalized, key, 4096)
    if not isinstance(normalized.get("ice_servers"), list):
        raise RuntimeError("Configuration value ice_servers must be a list")
    if normalized["turn_secret"] and not normalized["turn_urls"]:
        raise RuntimeError("turn_secret requires at least one turn_urls entry")
    if normalized["turn_secret"] and (normalized["turn_username"] or normalized["turn_credential"]):
        raise RuntimeError("Use either turn_secret or turn_username/turn_credential, not both")
    if normalized.get("send_queue_low_water", 0) >= normalized.get("send_queue_high_water", 1):
        raise RuntimeError("send_queue_low_water must be lower than send_queue_high_water")
    if normalized["session_absolute_ttl_seconds"] < normalized["session_idle_ttl_seconds"]:
        raise RuntimeError("session_absolute_ttl_seconds cannot be shorter than the idle timeout")
    if normalized["fps"] < normalized["min_fps"]:
        normalized["min_fps"] = normalized["fps"]
    if normalized["max_width"] < normalized["min_width"]:
        normalized["min_width"] = normalized["max_width"]
    if normalized["webrtc_fps"] < normalized["webrtc_min_fps"]:
        normalized["webrtc_min_fps"] = normalized["webrtc_fps"]
    if normalized["webrtc_bitrate_start"] > normalized["webrtc_bitrate_max"]:
        normalized["webrtc_bitrate_start"] = normalized["webrtc_bitrate_max"]
    if normalized["webrtc_bitrate_min"] > normalized["webrtc_bitrate_start"]:
        normalized["webrtc_bitrate_min"] = normalized["webrtc_bitrate_start"]
    for key in ("password_salt", "password_hash"):
        value = normalized.get(key)
        if not isinstance(value, str):
            raise RuntimeError(f"Configuration value {key} is missing")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise RuntimeError(f"Configuration value {key} must be hexadecimal") from exc
    normalized["ice_servers_normalized"] = _ice_servers(normalized)
    return normalized


def load_config() -> tuple[dict[str, Any], str | None]:
    config = dict(DEFAULTS)
    generated_password: str | None = None
    if CONFIG_PATH.exists():
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Configuration must contain a JSON object: {CONFIG_PATH}")
        config.update(loaded)
    else:
        generated_password = secrets.token_urlsafe(14)
        salt = secrets.token_hex(16)
        config.update(
            password_salt=salt,
            password_hash=hash_password(generated_password, salt),
        )
        save_config(config)

    # Compatibility with the original configuration keys.
    if "session_ttl_seconds" in config:
        config["session_idle_ttl_seconds"] = int(config["session_ttl_seconds"])
    if not config.get("interactive_fps"):
        config["interactive_fps"] = config.get("active_fps", DEFAULTS["interactive_fps"])
    return _normalize_config(config), generated_password


def turn_credentials(config: dict[str, Any], ttl: int | None = None) -> tuple[str, str] | None:
    """Return TURN credentials, generating ephemeral ones when a secret is set."""
    secret = str(config.get("turn_secret", ""))
    if not secret:
        username = str(config.get("turn_username", ""))
        credential = str(config.get("turn_credential", ""))
        if username and credential:
            return username, credential
        return None
    lifetime = int(ttl if ttl is not None else config.get("turn_credential_ttl_seconds", 3600))
    expiry = int(time.time()) + max(60, lifetime)
    key_id = str(config.get("turn_key_id", "")).strip()
    username = f"{expiry}:{key_id}" if key_id else str(expiry)
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1)
    import base64

    credential = base64.b64encode(digest.digest()).decode("ascii")
    return username, credential


def client_ice_servers(config: dict[str, Any]) -> list[dict[str, Any]]:
    """ICE servers as sent to the browser, with TURN credentials resolved.

    With a shared ``turn_secret`` the credentials are minted per call so the
    expiry is always relative to the moment the client connects.
    """
    entries = config.get("ice_servers_normalized", [])
    resolved: list[dict[str, Any]] = []
    ephemeral: tuple[str, str] | None = None
    for entry in entries:
        urls = [str(item) for item in entry.get("urls", [])]
        if not urls:
            continue
        item: dict[str, Any] = {"urls": urls}
        if "username" in entry and "credential" in entry:
            item["username"] = str(entry["username"])
            item["credential"] = str(entry["credential"])
        elif any(url.startswith(("turn:", "turns:")) for url in urls):
            if ephemeral is None:
                ephemeral = turn_credentials(config)
            if ephemeral is not None:
                item["username"], item["credential"] = ephemeral
        resolved.append(item)
    return resolved
