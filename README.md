# Tablet Remote Desktop

Free, self-hosted remote desktop software for controlling a Windows, macOS, or Linux computer from a modern browser on a tablet, phone, or another computer.

- Browser client: no client application to install.
- Host transports: direct WebRTC for video, system audio, and control; automatic WebSocket fallback.
- Host functions: primary-screen viewing, pointer, keyboard, touch mapping, text clipboard, and system audio when a real loopback source is available.
- Public access option: Cloudflare Quick Tunnel, without making the application depend on a paid server or domain.
- Local access remains available without Cloudflare or another external connectivity provider.

> The code and Windows package have automated and local health verification. macOS, Linux X11, and Linux Wayland code paths exist but have not been executed on those operating systems in this repository's current verification record. See [Verification status](#verification-status).

## Zero-cost deployment policy

The required application, source build, and self-hosted deployment path must not require payment for a server, domain, API, subscription, relay, tunnel plan, or developer signing membership.

- Local-network use has no external service dependency.
- WebRTC uses direct peer-to-peer connectivity and the free Cloudflare STUN endpoint.
- No paid TURN relay is configured or required.
- If direct WebRTC cannot cross a NAT or firewall, the browser falls back to WebSocket through the address that loaded the page.
- Cloudflare Quick Tunnel is an optional free, best-effort public address. It has no uptime guarantee.
- Release packages are unsigned and unnotarized. Checksums are generated for verification.
- Third-party free-service terms and availability can change and are not controlled by this project.
- The policy covers required project software and deployment services, not hardware, electricity, or the user's existing internet connection.

## Platform support

| Host | Implemented path | Permission or dependency boundary | Current verification |
|---|---|---|---|
| Windows | MSS capture, PyAutoGUI input, native Unicode input, synthetic touch, SoundCard WASAPI loopback | UAC and secure desktop cannot be controlled by a normal user process | Package builds and starts; HTTP security and WebRTC synthetic-media tests pass. Live screen/input/audio were not exercised during the current verification. |
| macOS | MSS capture, PyAutoGUI pointer/keyboard, clipboard, permission detection, optional CoreAudio loopback | Screen & System Audio Recording and Accessibility permissions must be granted locally | Source, launcher, CI, and package workflow exist; macOS runtime is not verified. |
| Linux X11 | MSS capture, PyAutoGUI/Xlib input, clipboard, optional PulseAudio/PipeWire monitor | Requires an active X11 session and distribution packages used by PyAutoGUI/clipboard/audio | Source, launcher, CI, and package workflow exist; X11 runtime is not verified. |
| Linux Wayland | XDG RemoteDesktop + ScreenCast permission session, PipeWire stream through GStreamer, portal pointer/keyboard/touch input | The host user must approve the portal dialog; requires a compatible portal backend and GStreamer PipeWire plugin | Backend and browser authorization flow exist; Wayland runtime is not verified. |

The application reports unavailable capabilities instead of substituting microphone capture for missing system audio.

## Quick start from source

Python 3.10 through 3.14 is supported by the declared dependencies.

### Windows

Open PowerShell in the repository:

```powershell
.\start.ps1
```

The script finds a working Python installation, creates `.venv`, installs dependencies only when `requirements.txt` changes, and starts the host.

### macOS

Grant execute permission once, then start:

```bash
chmod +x start.sh start.command
./start.command
```

When macOS asks, grant:

- **Screen & System Audio Recording** for screen capture.
- **Accessibility** for remote pointer and keyboard input.

### Linux X11

Install Python, virtual-environment support, Tk, and the X11/clipboard/audio packages supplied by the distribution. On Debian or Ubuntu, the host packages include:

```bash
sudo apt install python3 python3-venv python3-tk scrot xclip
```

Then run:

```bash
chmod +x start.sh
./start.sh
```

### Linux Wayland

Install the desktop's XDG portal backend and the GStreamer PipeWire command-line plugin. On Debian or Ubuntu, the relevant host packages include:

```bash
sudo apt install python3 python3-venv python3-tk xdg-desktop-portal gstreamer1.0-tools gstreamer1.0-pipewire
```

Start with `./start.sh`. After browser login, the host desktop shows its own screen/control permission dialog. Remote control does not start unless the host user approves it.

## First login

The first start creates a random local password and prints it once in the host terminal. Configuration is stored outside the source tree:

- Windows: `%APPDATA%\TabletRemoteDesktop\config.json`
- macOS: `~/Library/Application Support/TabletRemoteDesktop/config.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/tablet-remote-desktop/config.json`

An existing repository-local `config.json` is still recognized for compatibility and remains excluded by `.gitignore`.

Reset the password:

```powershell
.\reset_password.ps1
```

or:

```bash
./reset_password.sh
```

## Connect from another device

The host binds to `127.0.0.1:18765` by default.

### Trusted local network

Explicitly enable LAN listening:

```powershell
$env:TRD_BIND = "0.0.0.0"
$env:TRD_ALLOW_INSECURE_LAN = "1"
.\start.ps1
```

or:

```bash
TRD_BIND=0.0.0.0 TRD_ALLOW_INSECURE_LAN=1 ./start.sh
```

Open `http://HOST_LAN_IP:18765` from the browser. Plain HTTP LAN use should only be enabled on a trusted network. Without the explicit acknowledgement above, non-loopback HTTP startup is refused.

### Free public address

Install `cloudflared` from the official download page, keep the host bound locally, and run:

```powershell
.\run_cloudflare_tunnel.ps1
```

or:

```bash
./run_cloudflare_tunnel.sh
```

Open the generated `https://...trycloudflare.com` address and enter the separate application password. Quick Tunnel is optional and does not replace application authentication.

## Transport behavior

1. The browser authenticates through same-origin HTTPS or HTTP.
2. It requests a direct WebRTC connection using STUN only.
3. The host sends an encoded video track and, when available, an encoded system-audio track.
4. Pointer, keyboard, touch, and client-size messages use an encrypted WebRTC data channel.
5. If WebRTC cannot connect, the existing JPEG/WebP screen channel, PCM audio channel, and control WebSocket are used automatically.

The fallback does not guarantee public connectivity by itself: the browser must still be able to reach the HTTP/WebSocket address that served the page.

## Security controls

- Random 32-byte-equivalent session tokens stored only in host memory.
- PBKDF2-SHA256 password hashing and constant-time comparison.
- Login attempt window and temporary lockout.
- PBKDF2 work runs outside the web event loop.
- Idle and absolute session expiry.
- Per-session connection limits.
- Host allowlisting plus strict same-origin checks for writes and WebSocket handshakes.
- `HttpOnly`, `SameSite=Strict`, and HTTPS-aware `Secure` cookies.
- Request-body, WebSocket-message, clipboard, text, coordinate, touch-count, and input-value limits.
- Security response headers and a restrictive Content Security Policy.
- Local binding by default and a visible terminal stop action.
- Generated secrets, logs, tools, virtual environments, and build output excluded from Git.
- Missing system-audio loopback reports unavailable instead of capturing a microphone.

Close the terminal or press `Ctrl+C` to stop the foreground host. Windows background mode uses project-owned PID files:

```powershell
.\start_detached.ps1
.\stop_detached.ps1
```

The stop script does not terminate unrelated Python or `cloudflared` processes.

## Clipboard

The toolbar provides:

- **&#21462;&#21098;&#36148;&#26495;**: copies host text into the browser device clipboard.
- **&#21457;&#21098;&#36148;&#26495;**: sends browser-device clipboard text to the host.

Browser clipboard access normally requires HTTPS or localhost and a user click. When that browser API is unavailable on plain LAN HTTP, the interface opens a manual copy/paste prompt. Only text clipboard data is supported.

## Tests

Run all repository tests:

```bash
python -m unittest discover -s tests -v
node --check app/static/app.js
```

The test suite covers session expiry and active-session capacity, connection limits, login lockout, proxy identity, Host/Origin matching, message validation, browser DOM contracts, clipboard fallback, Wayland keysym mapping, WebRTC negotiation cleanup, video, data-channel control, and audio-frame formatting.

## Build downloadable packages

Install build dependencies and build on the target operating system:

```bash
python -m pip install -r requirements-dev.txt
python build_release.py
```

PyInstaller is not a cross-compiler, so Windows, macOS, and Linux packages are built on their respective operating systems. The script creates an archive and a `.sha256` file in `dist/`.

Two public-repository GitHub Actions workflows are included:

- `CI`: compiles and tests on Windows, macOS, and Linux.
- `Build releases`: builds all three packages. A `v*` tag creates a GitHub Release and uploads packages and checksums.

These workflows use only standard GitHub-hosted runners. They do not create Actions artifacts or caches; tagged packages are uploaded directly as GitHub Release assets. GitHub currently provides standard runners without runner charges for public repositories.

## Verification status

Verified on the current Windows machine:

- Python compilation for application, build scripts, and tests.
- JavaScript syntax.
- 21 automated tests, including local WebRTC video/control/audio and Wayland FD/touch tests using generated state rather than the user's screen or microphone.
- HTTP authentication, Cookie, Host/Origin rejection, security headers, and live-connection closure on logout against a temporary local server.
- WebRTC dependencies import successfully.
- Windows PyInstaller package creation.
- Packaged executable starts, serves `/api/status`, and reports WebRTC available.
- Generated Windows archive SHA-256 matches its checksum file.

Not verified here:

- Live screen capture, input injection, clipboard, or system-audio transfer after the current changes.
- macOS runtime and permission flow.
- Linux X11 runtime.
- Linux Wayland portal, PipeWire, and input flow.
- Public-tunnel end-to-end access.
- GitHub-hosted CI and release jobs, because the repository has not been pushed with these changes.

Support claims should be updated only after the corresponding operating-system package and end-to-end test pass.

## Repository discovery

Recommended GitHub description:

> Free self-hosted cross-platform remote desktop with a browser client, WebRTC direct connection, and WebSocket fallback.

Recommended topics:

`remote-desktop`, `remote-control`, `self-hosted`, `cross-platform`, `webrtc`, `websocket`, `browser-client`, `windows`, `macos`, `linux`, `wayland`, `python`

AI users are an intended audience, but the application does not currently implement an AI feature; inaccurate AI topics should not be added.

## License decision still required

No open-source license has been selected yet. A public GitHub repository can be viewed and forked through GitHub, but general permission to use, modify, redistribute, or sell the code is not granted until a license file is added by the owner.

## Technical references

- [GitHub: About repository READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)
- [GitHub: Repository topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics)
- [GitHub: Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [GitHub Actions: GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [W3C WebRTC Recommendation](https://www.w3.org/TR/webrtc/)
- [Cloudflare Realtime TURN/STUN service addresses](https://developers.cloudflare.com/realtime/turn/)
- [Cloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- [PyInstaller operating mode](https://pyinstaller.org/en/stable/operating-mode.html)
- [XDG Remote Desktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- [XDG ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [GStreamer appsink](https://gstreamer.freedesktop.org/documentation/application-development/advanced/pipeline-manipulation.html)
- [Apple Screen & System Audio Recording permission](https://support.apple.com/guide/mac-help/mchld6aa7d23/mac)
- [Apple Accessibility permission](https://support.apple.com/guide/mac-help/mh43185/mac)
- [Apple developer membership comparison](https://developer.apple.com/support/compare-memberships/)