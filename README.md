# Tablet Remote Desktop

Free, self-hosted remote desktop software for controlling a Windows, macOS, or Linux computer from a modern browser on a tablet, phone, or another computer.

- Browser client: no client application to install.
- Host transports: direct WebRTC (Opus audio, adaptive video) for everyday use; the JPEG/WebSocket channel is a fallback, not the default path.
- Long-session design: a single reused capture surface, a drift-corrected frame loop, separated reliable and lossy input channels, automatic reconnection, and a supervisor that restarts the host and the tunnel.
- Office features: remote cursor overlay, drag-to-pan when zoomed, resumable file transfer both ways, automatic bidirectional text clipboard, multi-monitor selection, and real key down/up so long presses and combinations work.
- Public access options: fixed-hostname Cloudflare Named Tunnel (recommended) or a zero-configuration Quick Tunnel.
- Local access works with no external service at all.

> The code and the Windows package have automated and local health verification. macOS, Linux X11, and Linux Wayland code paths exist but have not been executed on those operating systems in this repository's current verification record. See [Verification status](#verification-status).

## Zero-cost deployment policy

The required application, source build, and self-hosted deployment path must not require payment for a server, domain, API, subscription, relay, tunnel plan, or developer signing membership.

- Local-network use has no external service dependency.
- WebRTC uses direct peer-to-peer connectivity and the free Cloudflare STUN endpoint.
- TURN support is built in but **off by default**: no paid relay is configured or required, and no credentials ship with the project. If your network cannot be traversed directly, you may point `turn_urls` at your own relay (including a self-hosted one) or leave it empty and use the WebSocket fallback.
- If direct WebRTC cannot cross a NAT or firewall, the browser falls back to WebSocket through the address that loaded the page.
- Cloudflare Quick Tunnel is an optional free, best-effort public address with no uptime guarantee. A Named Tunnel is also free but needs a domain you already control.
- Release packages are unsigned and unnotarized. Checksums are generated for verification.
- Third-party free-service terms and availability can change and are not controlled by this project.
- The policy covers required project software and deployment services, not hardware, electricity, or the user's existing internet connection.

## Platform support

| Host | Implemented path | Permission or dependency boundary | Current verification |
|---|---|---|---|
| Windows | Shared MSS capture surface, PyAutoGUI input, native Unicode input, synthetic touch, hold/release key injection, SoundCard WASAPI loopback | UAC and secure desktop cannot be controlled by a normal user process; Windows refuses synthetic input while a higher-integrity window has focus, which is reported rather than hidden | Package builds and starts; capture, WebRTC negotiation, file transfer, clipboard, and input state are exercised by the automated tests and the end-to-end smoke run on this machine |
| macOS | MSS capture, PyAutoGUI pointer/keyboard, clipboard, permission detection, optional CoreAudio loopback | Screen & System Audio Recording and Accessibility permissions must be granted locally | Source, launcher, CI, and package workflow exist; macOS runtime is not verified. |
| Linux X11 | MSS capture, PyAutoGUI/Xlib input, clipboard, optional PulseAudio/PipeWire monitor | Requires an active X11 session and distribution packages used by PyAutoGUI/clipboard/audio | Source, launcher, CI, and package workflow exist; X11 runtime is not verified. |
| Linux Wayland | XDG RemoteDesktop + ScreenCast permission session, PipeWire stream through GStreamer, portal pointer/keyboard/touch input including key hold/release | The host user must approve the portal dialog; requires a compatible portal backend and GStreamer PipeWire plugin | Backend, browser authorization flow, keysym mapping, and touch slot handling exist; Wayland runtime is not verified. |

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

An existing repository-local `config.json` is still recognized for compatibility and remains excluded by `.gitignore`. Existing configuration files are upgraded in memory: new keys take their defaults, and your current values are kept.

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

### Public address for long-term use

For work you depend on, use a Named Tunnel with a hostname you control. The address is stable across restarts and is not affected by Quick Tunnel availability.

One-time setup:

```bash
cloudflared tunnel login
cloudflared tunnel create my-remote-desktop
cloudflared tunnel route dns my-remote-desktop remote.example.com
```

Then create `logs/tunnel.json`:

```json
{
  "name": "my-remote-desktop",
  "credentials": "C:\\Users\\me\\.cloudflared\\<tunnel-id>.json",
  "url": "http://127.0.0.1:18765"
}
```

Start it with `.\run_cloudflare_tunnel.ps1` (or `./run_cloudflare_tunnel.sh`), or let `.\install_autostart.ps1` supervise it for you. Environment overrides `TRD_TUNNEL_NAME`, `TRD_TUNNEL_URL`, and `TRD_TUNNEL_CREDENTIALS` work too.

Without `logs/tunnel.json`, the same script starts a Quick Tunnel and prints a random `https://...trycloudflare.com` address. Quick Tunnel is fine for a quick test; it has no uptime guarantee and the address changes on every start.

Either way you still enter the separate application password. A tunnel does not replace application authentication.

## Keeping it running

Remote access is only useful if the host comes back after a reboot or a crash.

```powershell
.\install_autostart.ps1            # start the watchdog at logon
.\install_autostart.ps1 -StartNow  # register and start it now
.\install_autostart.ps1 -Boot      # also start at boot (elevated PowerShell)
.\install_autostart.ps1 -Remove    # unregister
```

The watchdog (`watch.ps1`) checks `http://127.0.0.1:18765/api/health` every 15 seconds, restarts the host when it exits or stops answering, and restarts `cloudflared` when the tunnel exits. It writes a timestamped line to `logs/watch.log` for every action. Restart backoff is exponential and capped at 60 seconds for the host and 120 seconds for the tunnel.

Run it in the foreground when you want to watch it:

```powershell
.\watch.ps1                # supervise host and tunnel
.\watch.ps1 -NoTunnel      # host only
```

`watch.ps1` is a thin wrapper around `tools\service_runner.ps1`, which holds the actual supervision loop. `start_detached.ps1` starts that runner detached, so the host keeps running after the shell that launched it exits.

```powershell
.\start_detached.ps1         # start the host and a detached supervisor
.\start_detached.ps1 -Watch  # start the host and supervise in this window
.\stop_detached.ps1          # stop the supervisor, the host, and the tunnel
```

`stop_detached.ps1` does not terminate unrelated Python or `cloudflared` processes.

Verify the whole lifecycle on your own machine:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_lifecycle.ps1
```

It stops anything running, starts the background host, checks `/api/health`, starts it a second time to confirm the supervisor and the host are reused rather than duplicated, kills the host to confirm it comes back, and finally stops everything.

### Status and logs

- `GET /api/health` is an unauthenticated liveness probe returning `status`, `uptime`, active `sessions`/`peers`, and `capture` state. It never exposes configuration, credentials, or session data.
- `GET /api/status` is the authenticated capability document the browser uses.
- Logs live in `logs/`: `watch.log`, `server.out.log`, `server.err.log`, `cloudflared.out.log`, `cloudflared.err.log`.

## Transport behavior

1. The browser authenticates through same-origin HTTPS or HTTP.
2. It requests a direct WebRTC connection. The ICE server list comes from `/api/status`: free STUN by default, plus TURN only if you configured it. If `turn_secret` is set, short-lived TURN credentials are minted per request instead of shipping a long-lived secret to the browser.
3. The host sends an adaptive video track and, when a loopback device is available, an Opus audio track.
4. Pointer, keyboard, touch, clipboard, and file messages use WebRTC data channels: a reliable ordered channel for anything that changes state, and an unordered channel with no retransmits for pointer motion.
5. If WebRTC cannot connect or drops, the JPEG/WebSocket screen channel, the ADPCM audio channel, and the control WebSocket take over automatically. The client keeps retrying WebRTC in the background and switches back once it succeeds.
6. In WebRTC mode the JPEG channel is closed, so the host is not encoding the screen twice.

The fallback does not guarantee public connectivity by itself: the browser must still be able to reach the HTTP/WebSocket address that served the page.

### Capture and quality adaptation

- One capture surface is created per process and reused for every frame. Re-creating it per frame reallocates a device context and a bitmap on the Windows backend, which costs more than the capture itself.
- A shared capture task grabs the desktop once per tick for every consumer; WebRTC encoding and the JPEG fallback read the newest frame instead of each triggering their own grab.
- Only the pixels the client viewport can display are read from the desktop, so a 2560-wide screen serving a 1024-wide viewport moves roughly a sixth of the data.
- The frame pacer re-anchors on an absolute schedule, so processing time is not added on top of every sleep.
- Frame rate, JPEG/WebP quality, and capture width move independently. Congestion or high CPU lowers quality first, then resolution, then frame rate. A static screen drops toward the floor frame rate but never collapses to 1 FPS while a client is watching, so clocks, notifications, and small updates still arrive.
- Over WebRTC, `getStats()` drives encoder bitrate between `webrtc_bitrate_min` and `webrtc_bitrate_max` using the browser's available-bandwidth estimate, packet loss, and round-trip time.

### Reconnection

- A `disconnected` peer is held, not torn down. The client keeps the peer for a grace window and tries an ICE restart.
- If an in-place restart is not possible, the host answers the restart request with an explicit recover hint and the client renegotiates with a fresh ICE generation. The session slot is reused, so a flapping link does not exhaust the connection limit.
- After a fallback, WebRTC recovery is attempted on a backoff schedule, and the JPEG channel is closed again once WebRTC is live.
- All reconnects use randomised exponential backoff capped at 10 seconds.

### Input on a weak link

- Clicks, key down/up, touch `down`/`up`/`cancel`, and wheel go on the reliable ordered channel.
- Pointer motion goes on the lossy channel and is dropped, not queued, when it cannot keep up: the newest position is the only useful one.
- `bufferedAmount` is checked before every send. When the reliable channel is backed up, state-changing events are diverted to the control WebSocket instead of being dropped.
- When the reliable queue is full, pending motion samples are discarded to make room. A mouse-up or touch-end is never dropped.
- Held input belongs to the session, not the connection, so anything left pressed is released when the session ends, and re-pressed after a reconnect.

### Audio

- WebRTC audio is Opus, as negotiated by the SDP.
- The WebSocket fallback sends 4-bit IMA ADPCM (4:1) instead of raw PCM, so it costs about 384 kbit/s for stereo 48 kHz instead of 1536 kbit/s. The codec is implemented in this repository, needs no extra dependency, and no network service.
- The browser holds a small jitter buffer, resynchronises instead of letting the lead grow without bound after a pause, and reconstructs the stream continuously across frames.
- Loopback devices are selectable with `audio_device`; a missing or wrong device is reported instead of silently capturing a microphone.

## Office features

- **Remote cursor**: the host reports the pointer position on the control channel and the browser draws it over the video. The position is mapped through the displayed image rectangle, so it stays correct at any zoom or scaling.
- **Drag to pan**: the `拖动` toolbar button switches the viewer between remote input and local panning. Pinch or Ctrl+wheel zooms; dragging pans. In input mode two-finger pinch still zooms.
- **File transfer**: the `文件` panel browses the host's receive directory and send directory, uploads with progress and cancel, downloads any file in the send directory, and resumes after an interruption. Uploads are chunked, acknowledged with the host's authoritative offset, verified with SHA-256 on completion, and stored under a `.part` name until they verify. Path traversal is rejected, absolute paths are refused, and a free-space reserve is kept.
- **Clipboard**: text is synchronised both ways. The host polls its clipboard and pushes changes; the browser writes them locally and can push its own clipboard back. The manual `取剪贴板` / `发剪贴板` buttons remain for browsers that block clipboard access on plain HTTP.
- **Multi-monitor**: `/api/status` lists the displays, and the `屏1` toolbar button cycles between them. Selecting a display renegotiates the video source.
- **Keyboard**: key down and key up are separate events, so long presses, held modifiers, and combinations (`Ctrl+Alt+Del`-style sequences, `Shift`-drag, and so on) behave normally. Releasing the page or the session releases everything the client was holding.

## Security controls

- Random 32-byte-equivalent session tokens stored only in host memory.
- PBKDF2-SHA256 password hashing and constant-time comparison.
- Login attempt window and temporary lockout.
- PBKDF2 work runs outside the web event loop.
- Idle and absolute session expiry.
- Per-session connection limits, reused across a reconnect.
- Host allowlisting plus strict same-origin checks for writes and WebSocket handshakes.
- `HttpOnly`, `SameSite=Strict`, and HTTPS-aware `Secure` cookies.
- Request-body, WebSocket-message, clipboard, text, coordinate, touch-count, input-value, file-name, and transfer-id limits.
- Path-traversal rejection and root confinement for every file operation.
- SHA-256 verification of completed uploads; unverifiable uploads are discarded.
- TURN credentials are never embedded in the page unless you configured TURN; with `turn_secret` only short-lived credentials are sent.
- `/api/health` is the only unauthenticated data endpoint, and it exposes no configuration or credentials.
- Security response headers and a restrictive Content Security Policy.
- Local binding by default and a visible terminal stop action.
- Generated secrets, logs, tools, virtual environments, and build output excluded from Git.
- Missing system-audio loopback reports unavailable instead of capturing a microphone.

Close the terminal or press `Ctrl+C` to stop the foreground host.

## Monitoring

The toolbar exposes a metrics panel with live values:

- **FPS** measured from the video frame callback (WebRTC) or the delivered frame rate (fallback).
- **Latency** from WebRTC round-trip time, or from a `ping`/`pong` round trip on the active channel.
- **Loss** from `getStats()` packet loss on the inbound video stream.
- **Bitrate** from received bytes per second.
- **Mode** and resolution of the current channel.

The same numbers drive the adaptation: the client reports its own FPS, latency, loss, and dropped-frame count to the host, and the host combines them with its own capture and encode timings. HTTP and WebSocket round trips count toward an input session's idle timeout, so a session stays alive while you are watching and closes when you are not.

## Configuration reference

The settings that matter most for long-term use, with their defaults:

| Key | Default | Purpose |
|---|---|---|
| `stun_urls` | free Cloudflare STUN | Direct connectivity |
| `turn_urls` | `[]` | Optional TURN relay URLs (`turn:`/`turns:`) |
| `turn_username` / `turn_credential` | `""` | Static TURN credentials |
| `turn_secret` / `turn_key_id` | `""` | Shared secret for short-lived TURN credentials |
| `ice_servers` | `[]` | Full ICE server objects, for anything the keys above do not cover |
| `ice_transport_policy` | `all` | Set to `relay` to force a TURN relay |
| `webrtc_fps` / `webrtc_min_fps` | `30` / `8` | WebRTC frame-rate ceiling and floor |
| `webrtc_bitrate_min` / `_start` / `_max` | 0.4 / 2.5 / 8 Mbit/s | Adaptive encoder bitrate range |
| `fps` / `min_fps` | `15` / `5` | Fallback channel frame-rate ceiling and floor |
| `cpu_budget` | `0.75` | Fraction of a frame interval capture+encode may consume |
| `monitor` | `1` | Display captured at startup |
| `cursor_enabled` | `true` | Report the host cursor position |
| `clipboard_sync_enabled` | `true` | Automatic clipboard synchronisation |
| `audio_codec` | `adpcm` | Fallback audio codec (`adpcm` or raw `pcm`) |
| `audio_device` | `""` | Loopback device name; empty picks the first |
| `file_transfer_enabled` | `true` | Enable the file panel and its endpoints |
| `file_upload_root` | `~/Downloads` | Where received files are stored |
| `file_download_root` | `~` | Which tree the `发送目录` browser may read |
| `file_max_size` | 4 GiB | Largest single upload |
| `file_reserve_free_bytes` | 256 MiB | Free space kept on the host |
| `webrtc_grace_seconds` | `10` | How long a dropped peer is held before recovery |
| `session_idle_ttl_seconds` | `3600` | Idle session timeout |

Out-of-range or inconsistent values are rejected at startup with a message naming the key, instead of failing later.

## Tests

Run all repository checks:

```bash
python -m unittest discover -s tests -v
node --check app/static/app.js
python tools/smoke_test.py
```

On Windows, the background lifecycle has its own verifier:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_lifecycle.ps1
```

`tools/smoke_test.py` starts the real application in-process and exercises it end to end: capture and encoding, the health and status endpoints, the JPEG fallback channel, the control channel including key hold/release, clipboard, a full resumable upload with a replayed chunk and a checksum, downloads, path-traversal rejection, origin rejection, WebRTC negotiation with both data channels, video frames, ICE-restart behaviour, and the ADPCM round trip.

The unit suite covers session expiry and capacity, connection limits, login lockout, proxy identity, Host/Origin matching, message validation, browser DOM contracts, clipboard fallback, Wayland keysym mapping, WebRTC negotiation cleanup, video and audio frame formatting, capture engine reuse, the adaptive controller, frame pacing, the ADPCM codec, reliable-versus-lossy input classification, configuration normalization and TURN credential handling, and file transfer safety, resumption, and verification.

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
- 116 automated tests, including local WebRTC video/control/audio negotiation, capture-engine reuse and adaptive control, ADPCM round-trip accuracy, file-transfer resumption and checksum verification, and configuration/TURN normalization, using generated state rather than the user's screen or microphone.
- An end-to-end smoke run against the real application: live capture and JPEG encoding, the health and status endpoints, the JPEG fallback channel with server metrics, control-channel key hold/release, clipboard, a resumable upload with a replayed chunk and SHA-256 verification, download and path-traversal rejection, strict origin rejection, WebRTC offer/answer with control and motion data channels, a real video frame, ICE-restart fallback behaviour, and the ADPCM codec.
- The full background lifecycle against the real machine: `start_detached.ps1` starts the host and a detached supervisor, `/api/health` answers, a second start reuses both instead of duplicating them, killing the host makes the supervisor start it again (confirmed by pid change and a fresh health response), and `stop_detached.ps1` stops everything and removes the pid files.
- HTTP authentication, Cookie, Host/Origin rejection, security headers, and live-connection closure on logout against a temporary local server.
- WebRTC dependencies import successfully.
- Windows PyInstaller package creation.
- Packaged executable starts, serves `/api/status`, and reports WebRTC available.
- Generated Windows archive SHA-256 matches its checksum file.
- PowerShell syntax validation for `watch.ps1`, `install_autostart.ps1`, `start_detached.ps1`, `stop_detached.ps1`, and `run_cloudflare_tunnel.ps1`.

Not verified here:

- Live input injection into another application: Windows refuses synthetic input while a higher-integrity window has focus, so on this machine the injection calls return an error that the host now reports instead of hiding. Click, key, and touch delivery still need a normal-desktop check.
- Visual appearance and interaction of the browser UI on a real tablet, including the cursor overlay, pan, pinch zoom, and the file panel.
- macOS runtime and permission flow.
- Linux X11 runtime.
- Linux Wayland portal, PipeWire, and input flow.
- Named Tunnel end to end, and public access in general, because no domain or tunnel credentials are configured here.
- TURN traversal, because no TURN server is configured here.
- Real-world behaviour on home broadband, mobile, campus, and corporate networks.
- GitHub-hosted CI and release jobs, because the repository has not been pushed with these changes.

Support claims should be updated only after the corresponding operating-system package and end-to-end test pass.

## Repository discovery

Recommended GitHub description:

> Free self-hosted cross-platform remote desktop with a browser client, adaptive WebRTC video, resumable file transfer, and a supervised WebSocket fallback.

Recommended topics:

`remote-desktop`, `remote-control`, `self-hosted`, `cross-platform`, `webrtc`, `websocket`, `browser-client`, `windows`, `macos`, `linux`, `wayland`, `python`

AI users are an intended audience, but the application does not currently implement an AI feature; inaccurate AI topics should not be added.

## License

This project is released under the [MIT License](LICENSE).

## Technical references

- [GitHub: About repository READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)
- [GitHub: Repository topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository)
- [GitHub: Releases](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-releases)
- [GitHub Actions: GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [W3C WebRTC Recommendation](https://www.w3.org/TR/webrtc/)
- [RFC 8829: Javascript Session Establishment Protocol](https://www.rfc-editor.org/rfc/rfc8829) (ICE restart and renegotiation)
- [RFC 7635: TURN third-party authorization](https://www.rfc-editor.org/rfc/rfc7635) (shared-secret TURN credentials)
- [aiortc: RTCIceServer](https://aiortc.readthedocs.io/en/latest/api.html#aiortc.RTCIceServer)
- [Cloudflare Realtime TURN/STUN service addresses](https://developers.cloudflare.com/realtime/turn/)
- [Cloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- [Cloudflare Tunnel: locally-managed tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/)
- [Cloudflare Tunnel: run as a service](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/run-as-a-service/)
- [PyInstaller operating mode](https://pyinstaller.org/en/stable/operating-mode.html)
- [XDG Remote Desktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- [XDG ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [GStreamer appsink](https://gstreamer.freedesktop.org/documentation/application-development/advanced/pipeline-manipulation.html)
- [Apple Screen & System Audio Recording permission](https://support.apple.com/guide/mac-help/mchld6aa7d23/mac)
- [Apple Accessibility permission](https://support.apple.com/guide/mac-help/mh43185/mac)
- [Apple developer membership comparison](https://developer.apple.com/support/compare-memberships/)
