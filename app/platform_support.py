from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
from functools import lru_cache
from typing import Any


def _mac_permissions() -> dict[str, bool | None]:
    result: dict[str, bool | None] = {"screenRecording": None, "accessibility": None}
    try:
        core_graphics = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        core_graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        result["screenRecording"] = bool(core_graphics.CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    try:
        application_services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        application_services.AXIsProcessTrusted.restype = ctypes.c_bool
        result["accessibility"] = bool(application_services.AXIsProcessTrusted())
    except Exception:
        pass
    return result


@lru_cache(maxsize=1)
def _wayland_runtime_ready() -> bool:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    try:
        import dbus_next  # noqa: F401
    except Exception:
        return False
    launcher = shutil.which("gst-launch-1.0")
    inspector = shutil.which("gst-inspect-1.0")
    if not launcher or not inspector:
        return False
    for plugin in ("pipewiresrc", "videoconvert", "jpegenc"):
        try:
            result = subprocess.run(
                [inspector, plugin],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
    return True


def runtime_capabilities() -> dict[str, Any]:
    system = platform.system()
    result: dict[str, Any] = {
        "hostPlatform": system.lower(),
        "screen": True,
        "pointer": True,
        "keyboard": True,
        "clipboard": True,
        "nativeTouch": system == "Windows",
        "permission": {},
    }
    if system == "Darwin":
        result["permission"] = _mac_permissions()
    elif system == "Linux":
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower() or (
            "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"
        )
        portal_prerequisites = _wayland_runtime_ready() if session_type == "wayland" else None
        result.update(
            linuxSession=session_type,
            waylandPortalAvailable=portal_prerequisites,
        )
        if session_type == "wayland":
            # Permission and portal-backend support are confirmed only by the authorization call.
            result.update(screen=False, pointer=False, keyboard=False)
            result["permission"] = {"remoteDesktopPortal": False}
    return result