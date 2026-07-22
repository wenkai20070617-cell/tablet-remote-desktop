from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULTS: dict[str, Any] = {
    "bind": "127.0.0.1",
    "port": 18765,
    "fps": 1,
    "image_format": "jpeg",
    "jpeg_quality": 58,
    "max_width": 2560,
    "active_fps": 15,
    "active_jpeg_quality": 40,
    "active_max_width": 1280,
    "active_seconds": 1.2,
    "settle_fps": 8,
    "settle_jpeg_quality": 48,
    "settle_max_width": 1600,
    "settle_seconds": 3.5,
    "preview_fps": 3,
    "preview_jpeg_quality": 52,
    "preview_max_width": 1920,
    "preview_seconds": 8.0,
    "change_detection_enabled": True,
    "change_thumbnail_width": 96,
    "change_thumbnail_height": 60,
    "unchanged_preview_interval": 2.0,
    "unchanged_full_interval": 6.0,
    "frame_send_timeout_seconds": 2.5,
    "session_idle_ttl_seconds": 3600,
    "session_absolute_ttl_seconds": 43200,
    "max_sessions": 32,
    "max_connections_per_session": 4,
    "login_attempts": 5,
    "login_window_seconds": 60,
    "login_lockout_seconds": 300,
    "audio_enabled": True,
    "audio_sample_rate": 24000,
    "audio_frames": 1024,
    "webrtc_enabled": True,
    "stun_urls": ["stun:stun.cloudflare.com:3478"],
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


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    if not isinstance(normalized.get("bind"), str) or not normalized["bind"].strip():
        raise RuntimeError("Configuration value bind must be a non-empty string")
    normalized["bind"] = normalized["bind"].strip()
    int_ranges = {
        "port": (1, 65535),
        "fps": (1, 30),
        "jpeg_quality": (1, 95),
        "max_width": (640, 8192),
        "active_fps": (1, 30),
        "active_jpeg_quality": (1, 95),
        "active_max_width": (640, 8192),
        "settle_fps": (1, 30),
        "settle_jpeg_quality": (1, 95),
        "settle_max_width": (640, 8192),
        "preview_fps": (1, 30),
        "preview_jpeg_quality": (1, 95),
        "preview_max_width": (640, 8192),
        "change_thumbnail_width": (16, 1024),
        "change_thumbnail_height": (16, 1024),
        "session_idle_ttl_seconds": (60, 604800),
        "session_absolute_ttl_seconds": (60, 2592000),
        "max_sessions": (1, 256),
        "max_connections_per_session": (1, 16),
        "login_attempts": (1, 100),
        "login_window_seconds": (1, 3600),
        "login_lockout_seconds": (1, 86400),
        "audio_sample_rate": (8000, 96000),
        "audio_frames": (128, 8192),
    }
    for key, (low, high) in int_ranges.items():
        normalized[key] = _bounded_int(normalized, key, low, high)
    float_ranges = {
        "active_seconds": (0.1, 60.0),
        "settle_seconds": (0.1, 60.0),
        "preview_seconds": (0.1, 120.0),
        "unchanged_preview_interval": (0.1, 120.0),
        "unchanged_full_interval": (0.1, 600.0),
        "frame_send_timeout_seconds": (0.1, 30.0),
    }
    for key, (low, high) in float_ranges.items():
        normalized[key] = _bounded_float(normalized, key, low, high)
    for key in ("change_detection_enabled", "audio_enabled", "webrtc_enabled"):
        if not isinstance(normalized.get(key), bool):
            raise RuntimeError(f"Configuration value {key} must be true or false")
    if normalized.get("image_format") not in {"jpeg", "webp"}:
        raise RuntimeError("Configuration value image_format must be jpeg or webp")
    for key in ("stun_urls", "allowed_origins", "allowed_hosts"):
        value = normalized.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) or len(item) > 2048 for item in value):
            raise RuntimeError(f"Configuration value {key} must be a list of strings")
    if any(not item.startswith(("stun:", "stuns:")) for item in normalized["stun_urls"]):
        raise RuntimeError("Every stun_urls entry must use stun: or stuns:")
    if normalized["session_absolute_ttl_seconds"] < normalized["session_idle_ttl_seconds"]:
        raise RuntimeError("session_absolute_ttl_seconds cannot be shorter than the idle timeout")
    for key in ("password_salt", "password_hash"):
        value = normalized.get(key)
        if not isinstance(value, str):
            raise RuntimeError(f"Configuration value {key} is missing")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise RuntimeError(f"Configuration value {key} must be hexadecimal") from exc
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

    # Compatibility with the original configuration key.
    if "session_ttl_seconds" in config:
        config["session_idle_ttl_seconds"] = int(config["session_ttl_seconds"])
    return _normalize_config(config), generated_password