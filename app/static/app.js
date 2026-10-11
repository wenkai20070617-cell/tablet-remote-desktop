const loginPanel = document.getElementById("loginPanel");
const remotePanel = document.getElementById("remotePanel");
const loginForm = document.getElementById("loginForm");
const passwordInput = document.getElementById("password");
const loginMessage = document.getElementById("loginMessage");
const viewer = document.getElementById("viewer");
const stage = document.getElementById("stage");
const screen = document.getElementById("screen");
const screenVideo = document.getElementById("screenVideo");
const emptyState = document.getElementById("emptyState");
const connectionState = document.getElementById("connectionState");
const statusDot = document.getElementById("statusDot");
const audioState = document.getElementById("audioState");
const transportState = document.getElementById("transportState");
const pullClipboard = document.getElementById("pullClipboard");
const pushClipboard = document.getElementById("pushClipboard");
const keyboardButton = document.getElementById("keyboardButton");
const keyboardSink = document.getElementById("keyboardSink");
const mouseControl = document.getElementById("mouseControl");
const mouseToggle = document.getElementById("mouseToggle");
const mousePad = document.getElementById("mousePad");
const mouseLeft = document.getElementById("mouseLeft");
const mouseRight = document.getElementById("mouseRight");
const mouseWheelUp = document.getElementById("mouseWheelUp");
const mouseWheelDown = document.getElementById("mouseWheelDown");
const mouseDragLock = document.getElementById("mouseDragLock");
const mouseSensitivity = document.getElementById("mouseSensitivity");
const mouseScrollPad = document.getElementById("mouseScrollPad");
const zoomReset = document.getElementById("zoomReset");
const panToggle = document.getElementById("panToggle");
const monitorButton = document.getElementById("monitorButton");
const filesButton = document.getElementById("filesButton");
const filePanel = document.getElementById("filePanel");
const filePanelClose = document.getElementById("filePanelClose");
const fileRootUpload = document.getElementById("fileRootUpload");
const fileRootDownload = document.getElementById("fileRootDownload");
const filePath = document.getElementById("filePath");
const fileList = document.getElementById("fileList");
const fileSendButton = document.getElementById("fileSendButton");
const fileUpButton = document.getElementById("fileUpButton");
const transferList = document.getElementById("transferList");
const filePicker = document.getElementById("filePicker");
const metricsHud = document.getElementById("metricsHud");
const metricsToggle = document.getElementById("metricsToggle");
const metricFps = document.getElementById("metricFps");
const metricLatency = document.getElementById("metricLatency");
const metricLoss = document.getElementById("metricLoss");
const metricBitrate = document.getElementById("metricBitrate");
const metricMode = document.getElementById("metricMode");
const metricResolution = document.getElementById("metricResolution");
const cursorLayer = document.getElementById("cursorLayer");
const remoteCursor = document.getElementById("remoteCursor");
const binaryDecoder = new TextDecoder();

// ---------------------------------------------------------------- state
let ws = null;
let inputWs = null;
let audioWs = null;
let reconnectTimer = null;
let inputReconnectTimer = null;
let audioReconnectTimer = null;
let webrtcRecoverTimer = null;
let metricsTimer = null;
let reconnectAttempts = 0;
let inputReconnectAttempts = 0;
let audioReconnectAttempts = 0;
let statusCheckPromise = null;
let connectionGeneration = 0;
let peerConnection = null;
let rtcControl = null;
let rtcMotion = null;
let rtcMediaStream = null;
let rtcFallbackTimer = null;
let rtcConnected = false;
let rtcVideoReady = false;
let rtcHadAudio = false;
let rtcPeerId = "";
let rtcIceRestarting = false;
let rtcGraceUntil = 0;
let hostStatus = null;
let shouldReconnect = true;
let lastMoveAt = 0;
let audioContext = null;
let nextAudioTime = 0;
let audioDrift = 0;
let audioResyncs = 0;
let audioDecoderState = null;
let zoom = 1;
let panMode = false;
let panPointerId = null;
let panStart = null;
let screenObjectUrl = null;
let frameRendering = false;
let pendingFrame = null;
let lastRenderedSignature = null;
let lastTouchFrameAt = 0;
let remoteTextFocusActive = false;
let composingText = false;
let ignoreNextKeyboardInput = false;
let keyboardArmedUntil = 0;
let mousePadActive = false;
let mousePadPointerId = null;
let mousePadLast = null;
let mousePadDownAt = 0;
let mousePadTravel = 0;
let mouseDragLocked = false;
let pendingMouseDx = 0;
let pendingMouseDy = 0;
let mouseDeltaScheduled = false;
let mouseScrollPointerId = null;
let mouseScrollLastY = 0;
let mouseScrollCarry = 0;
let remoteCursorAt = 0;
let remoteCursorVisible = false;
let clipboardSyncEnabled = true;
let clipboardRevision = 0;
let lastAutoClipboardText = "";
let suppressClipboardEcho = false;
let holdToRepeatTimer = null;
let holdToRepeatKey = null;
let fileRoot = "upload";
let fileCurrentPath = "";
let transferCounter = 0;
const transfers = new Map();
const heldKeys = new Set();
const KEYBOARD_SENTINEL = "\u200b";
const activeTouches = new Map();
const FRAME_STALL_TIMEOUT_MS = 15000;
const RECONNECT_MAX_DELAY_MS = 10000;
const MOTION_BYTES_SOFT_LIMIT = 196608;
const RELIABLE_BYTES_SOFT_LIMIT = 786432;
let frameWatchdogTimer = null;
let lastMainMessageAt = 0;
let lastRtcFrameAt = 0;
let frameLoadTimer = null;
let droppedFrames = 0;
let metrics = {
  mode: "websocket",
  fps: 0,
  latency: 0,
  loss: 0,
  bitrate: 0,
  resolution: "",
  targetFps: 0,
  quality: 0,
  bytes: 0,
  rttFrames: [],
  lastBytesAt: 0,
  lastBytes: 0,
};
let pendingTransferCancel = new Set();

// ---------------------------------------------------------------- helpers
function clearReconnectTimers() {
  for (const timer of [reconnectTimer, inputReconnectTimer, audioReconnectTimer, webrtcRecoverTimer]) {
    if (timer) window.clearTimeout(timer);
  }
  reconnectTimer = null;
  inputReconnectTimer = null;
  audioReconnectTimer = null;
  webrtcRecoverTimer = null;
}

function closeMainWebSocket() {
  const socket = ws;
  ws = null;
  if (socket && socket.readyState !== WebSocket.CLOSED) socket.close();
}

function showLogin(message = "") {
  connectionGeneration += 1;
  statusCheckPromise = null;
  shouldReconnect = false;
  clearReconnectTimers();
  setMouseDragLocked(false);
  stopFrameWatchdog();
  stopMetrics();
  cancelActiveTouches();
  releaseAllKeys();
  closeMainWebSocket();
  closeInputWebSocket();
  closeAudioWebSocket();
  closeWebRTC();
  hostStatus = null;
  remoteCursor.classList.add("hidden");
  remotePanel.classList.add("hidden");
  loginPanel.classList.remove("hidden");
  loginMessage.textContent = message;
  passwordInput.value = "";
  passwordInput.focus();
}

async function fetchStatus() {
  const res = await fetch("/api/status", { cache: "no-store" });
  if (!res.ok) throw new Error(`status request failed: ${res.status}`);
  const status = await res.json();
  if (!status || typeof status.authenticated !== "boolean") {
    throw new Error("invalid status response");
  }
  return status;
}

async function checkStatus() {
  hostStatus = await fetchStatus();
  if (hostStatus.authenticated) {
    await showRemote();
  } else {
    showLogin();
  }
  return hostStatus;
}

function sessionIsAuthenticated() {
  if (statusCheckPromise) return statusCheckPromise;
  const check = fetchStatus().then((status) => status.authenticated);
  statusCheckPromise = check;
  const clear = () => {
    if (statusCheckPromise === check) statusCheckPromise = null;
  };
  check.then(clear, clear);
  return check;
}

function reconnectDelay(attempt, baseDelay) {
  const exponential = Math.min(RECONNECT_MAX_DELAY_MS, baseDelay * (2 ** Math.min(attempt, 5)));
  return Math.round(exponential * (0.85 + Math.random() * 0.3));
}

async function mayReconnect() {
  if (!shouldReconnect || remotePanel.classList.contains("hidden")) return false;
  const generation = connectionGeneration;
  try {
    const authenticated = await sessionIsAuthenticated();
    if (
      generation !== connectionGeneration ||
      !shouldReconnect ||
      remotePanel.classList.contains("hidden")
    ) return false;
    if (authenticated) return true;
    showLogin("登录已过期，请重新输入密码");
    return false;
  } catch (_) {
    return (
      generation === connectionGeneration &&
      shouldReconnect &&
      !remotePanel.classList.contains("hidden")
    );
  }
}

function scheduleMainReconnect() {
  if (reconnectTimer) window.clearTimeout(reconnectTimer);
  const delay = reconnectDelay(reconnectAttempts, 1000);
  reconnectAttempts += 1;
  reconnectTimer = window.setTimeout(async () => {
    reconnectTimer = null;
    if (!(await mayReconnect())) return;
    connectWebSocket();
  }, delay);
}

function scheduleInputReconnect() {
  if (inputReconnectTimer) window.clearTimeout(inputReconnectTimer);
  const delay = reconnectDelay(inputReconnectAttempts, 700);
  inputReconnectAttempts += 1;
  inputReconnectTimer = window.setTimeout(async () => {
    inputReconnectTimer = null;
    if (!(await mayReconnect())) return;
    connectInputWebSocket();
  }, delay);
}

function scheduleAudioReconnect() {
  if (audioReconnectTimer) window.clearTimeout(audioReconnectTimer);
  const delay = reconnectDelay(audioReconnectAttempts, 1200);
  audioReconnectAttempts += 1;
  audioReconnectTimer = window.setTimeout(async () => {
    audioReconnectTimer = null;
    if (!(await mayReconnect())) return;
    connectAudioWebSocket();
  }, delay);
}

function scheduleWebRTCRecovery() {
  if (webrtcRecoverTimer) window.clearTimeout(webrtcRecoverTimer);
  const delay = reconnectDelay(reconnectAttempts, 2500);
  reconnectAttempts += 1;
  webrtcRecoverTimer = window.setTimeout(async () => {
    webrtcRecoverTimer = null;
    if (!(await mayReconnect())) return;
    if (rtcConnected) return;
    connectWebRTC({ recover: true });
  }, delay);
}

// ---------------------------------------------------------------- login
loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginMessage.textContent = "";
  const submitButton = loginForm.querySelector('button[type="submit"]');
  if (submitButton) submitButton.disabled = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: passwordInput.value }),
    });
    if (!res.ok) {
      if (res.status === 401) {
        loginMessage.textContent = "密码错误";
      } else if (res.status === 429) {
        const retryAfter = Number.parseInt(res.headers.get("Retry-After") || "1", 10);
        loginMessage.textContent = `尝试过多，请在 ${Number.isFinite(retryAfter) ? retryAfter : 1} 秒后重试`;
      } else if (res.status === 400) {
        loginMessage.textContent = "登录请求无效";
      } else {
        loginMessage.textContent = `登录失败（HTTP ${res.status}）`;
      }
      return;
    }
    passwordInput.value = "";
    await checkStatus();
  } catch (_) {
    loginMessage.textContent = "无法连接到电脑服务";
  } finally {
    if (submitButton) submitButton.disabled = false;
  }
});

document.getElementById("logout").addEventListener("click", async () => {
  shouldReconnect = false;
  clearReconnectTimers();
  try {
    const response = await fetch("/api/logout", { method: "POST" });
    if (!response.ok && response.status !== 401) throw new Error("logout failed");
    showLogin();
  } catch (_) {
    showLogin("无法确认退出状态，请重新登录");
  }
});

document.getElementById("fullscreen").addEventListener("click", async () => {
  ensureAudio();
  if (!document.fullscreenElement && remotePanel.requestFullscreen) {
    await remotePanel.requestFullscreen().catch(() => {});
  } else if (document.exitFullscreen) {
    await document.exitFullscreen().catch(() => {});
  }
});

document.getElementById("zoomOut").addEventListener("click", () => setZoom(zoom - 0.15));
document.getElementById("zoomIn").addEventListener("click", () => setZoom(zoom + 0.15));
zoomReset.addEventListener("click", () => setZoom(1));
keyboardButton.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  ensureAudio();
  focusKeyboardSink();
});
keyboardButton.addEventListener("click", (event) => {
  event.preventDefault();
  ensureAudio();
  focusKeyboardSink();
});

panToggle.addEventListener("click", () => setPanMode(!panMode));
metricsToggle.addEventListener("click", () => {
  metricsHud.classList.toggle("expanded");
});

function setPanMode(active) {
  panMode = active;
  panToggle.classList.toggle("active", active);
  panToggle.setAttribute("aria-pressed", active ? "true" : "false");
  viewer.classList.toggle("panning", active);
  if (!active) cancelActiveTouches();
}

// ---------------------------------------------------------------- mouse pad
function setMouseControlOpen(open) {
  mouseControl.classList.toggle("collapsed", !open);
  mouseToggle.setAttribute("aria-expanded", open ? "true" : "false");
  mouseToggle.textContent = open ? "\u00d7" : "\u9f20";
  if (!open && mouseDragLocked) {
    setMouseDragLocked(false);
  }
}

mouseToggle.addEventListener("click", (event) => {
  event.preventDefault();
  setMouseControlOpen(mouseControl.classList.contains("collapsed"));
});

function sendMouseButton(button, action = "click") {
  sendInput({ kind: "mouse_button", button, action }, action !== "click");
}

function mouseSpeed() {
  const value = Number.parseFloat(mouseSensitivity.value);
  return Number.isFinite(value) ? value : 1.4;
}

function queueMouseDelta(dx, dy) {
  pendingMouseDx += dx;
  pendingMouseDy += dy;
  if (mouseDeltaScheduled) return;
  mouseDeltaScheduled = true;
  window.requestAnimationFrame(() => {
    mouseDeltaScheduled = false;
    const x = pendingMouseDx;
    const y = pendingMouseDy;
    pendingMouseDx = 0;
    pendingMouseDy = 0;
    if (Math.abs(x) < 0.4 && Math.abs(y) < 0.4) return;
    sendInput({ kind: "mouse_delta", dx: x, dy: y }, false);
  });
}

function setMouseDragLocked(active) {
  if (mouseDragLocked === active) return;
  mouseDragLocked = active;
  mouseDragLock.classList.toggle("active", active);
  mouseDragLock.setAttribute("aria-pressed", active ? "true" : "false");
  sendMouseButton(0, active ? "down" : "up");
}

function resetMousePadState() {
  mousePadActive = false;
  mousePadPointerId = null;
  mousePadLast = null;
}

function bindMouseButton(element, button) {
  element.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    element.setPointerCapture(event.pointerId);
    sendMouseButton(button, "down");
  });
  element.addEventListener("pointerup", (event) => {
    event.preventDefault();
    sendMouseButton(button, "up");
  });
  element.addEventListener("pointercancel", () => {
    sendMouseButton(button, "up");
  });
  element.addEventListener("contextmenu", (event) => event.preventDefault());
}

bindMouseButton(mouseLeft, 0);
bindMouseButton(mouseRight, 2);

mouseWheelUp.addEventListener("click", () => {
  sendInput({ kind: "wheel", delta: -5 }, true);
});

mouseWheelDown.addEventListener("click", () => {
  sendInput({ kind: "wheel", delta: 5 }, true);
});

mouseDragLock.addEventListener("click", () => {
  setMouseDragLocked(!mouseDragLocked);
});

// A pagehide/visibilitychange release is the last chance to avoid a stuck
// button on the host, so it goes out on the reliable channel only.
window.addEventListener("beforeunload", () => {
  releaseAllKeys();
  if (mouseDragLocked) {
    setMouseDragLocked(false);
  }
});

mouseScrollPad.addEventListener("contextmenu", (event) => event.preventDefault());

mouseScrollPad.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  mouseScrollPad.setPointerCapture(event.pointerId);
  mouseScrollPointerId = event.pointerId;
  mouseScrollLastY = event.clientY;
  mouseScrollCarry = 0;
});

mouseScrollPad.addEventListener("pointermove", (event) => {
  event.preventDefault();
  if (event.pointerId !== mouseScrollPointerId) return;
  const dy = event.clientY - mouseScrollLastY;
  mouseScrollLastY = event.clientY;
  mouseScrollCarry += dy;
  const steps = Math.trunc(mouseScrollCarry / 8);
  if (!steps) return;
  mouseScrollCarry -= steps * 8;
  sendInput({ kind: "wheel", delta: steps }, true);
});

function resetMouseScrollPad(event) {
  if (event.pointerId !== mouseScrollPointerId) return;
  mouseScrollPointerId = null;
  mouseScrollCarry = 0;
}

mouseScrollPad.addEventListener("pointerup", resetMouseScrollPad);
mouseScrollPad.addEventListener("pointercancel", resetMouseScrollPad);

mousePad.addEventListener("contextmenu", (event) => event.preventDefault());

mousePad.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  mousePad.setPointerCapture(event.pointerId);
  mousePadActive = true;
  mousePadPointerId = event.pointerId;
  mousePadLast = { x: event.clientX, y: event.clientY };
  mousePadDownAt = performance.now();
  mousePadTravel = 0;
});

mousePad.addEventListener("pointermove", (event) => {
  event.preventDefault();
  if (!mousePadActive || event.pointerId !== mousePadPointerId || !mousePadLast) return;
  const sample = pointerSample(event);
  const dx = sample.clientX - mousePadLast.x;
  const dy = sample.clientY - mousePadLast.y;
  mousePadLast = { x: sample.clientX, y: sample.clientY };
  mousePadTravel += Math.abs(dx) + Math.abs(dy);
  if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
  const pointerBoost = event.pointerType === "touch" ? 1 : 0.85;
  queueMouseDelta(dx * mouseSpeed() * pointerBoost, dy * mouseSpeed() * pointerBoost);
});

function finishMousePad(event) {
  if (!mousePadActive || event.pointerId !== mousePadPointerId) return;
  event.preventDefault();
  const elapsed = performance.now() - mousePadDownAt;
  if (!mouseDragLocked && mousePadTravel < 8 && elapsed < 280) {
    sendMouseButton(0, "click");
  }
  resetMousePadState();
}

mousePad.addEventListener("pointerup", finishMousePad);
mousePad.addEventListener("pointercancel", (event) => {
  if (event.pointerId !== mousePadPointerId) return;
  resetMousePadState();
});

// ---------------------------------------------------------------- transport
async function authorizeHostPlatform() {
  if (!hostStatus || hostStatus.linuxSession !== "wayland" || hostStatus.waylandAuthorized) return true;
  setConnectionState("\u8bf7\u5728\u4e3b\u673a\u4e0a\u5141\u8bb8\u5c4f\u5e55\u4e0e\u63a7\u5236\u6743\u9650", "connecting");
  let response;
  try {
    response = await fetch("/api/platform/authorize", { method: "POST" });
  } catch (_) {
    return false;
  }
  if (response.status === 401) {
    showLogin("登录已过期，请重新输入密码");
    return false;
  }
  if (!response.ok) return false;
  const result = await response.json();
  hostStatus.waylandAuthorized = Boolean(result.authorized);
  return hostStatus.waylandAuthorized;
}

async function showRemote() {
  connectionGeneration += 1;
  statusCheckPromise = null;
  shouldReconnect = true;
  reconnectAttempts = 0;
  inputReconnectAttempts = 0;
  audioReconnectAttempts = 0;
  loginMessage.textContent = "";
  loginPanel.classList.add("hidden");
  remotePanel.classList.remove("hidden");
  clipboardSyncEnabled = hostStatus?.clipboardSync !== false;
  updateMonitorButton();
  startFrameWatchdog();
  startMetrics();
  viewer.focus();
  if (await authorizeHostPlatform()) {
    startPreferredTransport();
  } else {
    setConnectionState("\u4e3b\u673a\u672a\u6388\u6743Wayland\u8fdc\u63a7", "closed");
  }
  requestWakeLock();
}

function setConnectionState(text, state) {
  connectionState.textContent = text;
  statusDot.classList.toggle("connected", state === "connected");
  statusDot.classList.toggle("closed", state === "closed");
}

function startFrameWatchdog() {
  if (frameWatchdogTimer) return;
  frameWatchdogTimer = window.setInterval(() => {
    if (document.hidden || remotePanel.classList.contains("hidden")) return;
    const now = performance.now();
    if (rtcConnected && lastRtcFrameAt > 0 && now - lastRtcFrameAt > FRAME_STALL_TIMEOUT_MS) {
      // Give the peer a chance to heal with an ICE restart before abandoning it.
      if (!rtcIceRestarting && now > rtcGraceUntil) {
        rtcIceRestarting = true;
        rtcGraceUntil = now + 12000;
        restartIce().catch(() => {});
        return;
      }
      setConnectionState("WebRTC画面停滞，切换连接", "closed");
      fallbackToWebSocket("WebRTC画面停滞，使用WebSocket");
      return;
    }
    if (
      ws &&
      ws.readyState === WebSocket.OPEN &&
      lastMainMessageAt > 0 &&
      now - lastMainMessageAt > FRAME_STALL_TIMEOUT_MS
    ) {
      setConnectionState("\u753b\u9762\u91cd\u8fde\u4e2d", "closed");
      ws.close();
    }
  }, 3000);
}

function stopFrameWatchdog() {
  if (!frameWatchdogTimer) return;
  window.clearInterval(frameWatchdogTimer);
  frameWatchdogTimer = null;
}

function waitForIceGatheringComplete(connection, timeoutMs = 6000) {
  if (connection.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = window.setTimeout(finish, timeoutMs);
    function finish() {
      window.clearTimeout(timeout);
      connection.removeEventListener("icegatheringstatechange", changed);
      resolve();
    }
    function changed() {
      if (connection.iceGatheringState === "complete") finish();
    }
    connection.addEventListener("icegatheringstatechange", changed);
  });
}

function setScreenTransport(mode) {
  const usingWebRTC = mode === "webrtc";
  rtcConnected = usingWebRTC;
  metrics.mode = usingWebRTC ? "webrtc" : "websocket";
  screenVideo.classList.toggle("hidden", !usingWebRTC);
  screen.classList.toggle("hidden", usingWebRTC);
  transportState.textContent = usingWebRTC ? "WebRTC" : "WebSocket";
  if (usingWebRTC) {
    screenVideo.classList.add("ready");
    markScreenReady();
  }
  layoutStage();
}

function observeRtcFrames(connection) {
  lastRtcFrameAt = performance.now();
  if (typeof screenVideo.requestVideoFrameCallback !== "function") return;
  let frames = 0;
  let windowStartedAt = performance.now();
  const observe = () => {
    if (connection !== peerConnection) return;
    lastRtcFrameAt = performance.now();
    frames += 1;
    const now = performance.now();
    if (now - windowStartedAt >= 1000) {
      metrics.fps = Math.round((frames * 1000) / (now - windowStartedAt));
      frames = 0;
      windowStartedAt = now;
      renderMetrics();
    }
    screenVideo.requestVideoFrameCallback(observe);
  };
  screenVideo.requestVideoFrameCallback(observe);
}

for (const eventName of ["loadeddata", "playing", "timeupdate"]) {
  screenVideo.addEventListener(eventName, () => {
    if (peerConnection) lastRtcFrameAt = performance.now();
  });
}

function closeWebRTC() {
  if (rtcFallbackTimer) {
    window.clearTimeout(rtcFallbackTimer);
    rtcFallbackTimer = null;
  }
  const control = rtcControl;
  const motion = rtcMotion;
  rtcControl = null;
  rtcMotion = null;
  const connection = peerConnection;
  peerConnection = null;
  for (const channel of [control, motion]) {
    if (channel) {
      try { channel.close(); } catch (_) {}
    }
  }
  if (connection) {
    try { connection.close(); } catch (_) {}
  }
  if (rtcMediaStream) {
    for (const track of rtcMediaStream.getTracks()) track.stop();
  }
  rtcMediaStream = null;
  screenVideo.srcObject = null;
  screenVideo.muted = false;
  rtcConnected = false;
  rtcVideoReady = false;
  rtcHadAudio = false;
  rtcPeerId = "";
  rtcIceRestarting = false;
  rtcGraceUntil = 0;
  lastRtcFrameAt = 0;
  remoteCursor.classList.add("hidden");
}

function fallbackToWebSocket(message = "WebRTC\u4e0d\u53ef\u7528\uff0c\u4f7f\u7528WebSocket") {
  closeWebRTC();
  setScreenTransport("websocket");
  transportState.textContent = message;
  connectWebSocket();
  connectInputWebSocket();
  connectAudioWebSocket();
  // The fallback is not a dead end: keep trying to win WebRTC back.
  scheduleWebRTCRecovery();
}

async function restartIce() {
  const connection = peerConnection;
  if (!connection || !rtcPeerId) return false;
  try {
    if (typeof connection.restartIce === "function") connection.restartIce();
    const offer = await connection.createOffer({ iceRestart: true });
    await connection.setLocalDescription(offer);
    await waitForIceGatheringComplete(connection, 4000);
    const response = await fetch("/api/webrtc/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ peerId: rtcPeerId }),
    });
    if (response.status === 401) {
      showLogin("登录已过期，请重新输入密码");
      return false;
    }
    if (response.status === 409) {
      // The host cannot renew ICE credentials in place; renegotiate fully.
      rtcIceRestarting = false;
      return recoverWebRTC();
    }
    if (!response.ok) return false;
    const answer = await response.json();
    if (connection !== peerConnection) return false;
    await connection.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    if (connection === peerConnection) rtcIceRestarting = false;
    return true;
  } catch (_) {
    if (connection === peerConnection) rtcIceRestarting = false;
    return false;
  }
}

async function recoverWebRTC() {
  if (!hostStatus?.webrtc) return false;
  setConnectionState("重新协商WebRTC", "connecting");
  const previousPeerId = rtcPeerId;
  try {
    const connection = peerConnection;
    if (connection) {
      const offer = await connection.createOffer({ iceRestart: true });
      await connection.setLocalDescription(offer);
      await waitForIceGatheringComplete(connection, 4000);
      const info = sendClientInfo(null);
      const response = await fetch("/api/webrtc/recover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: connection.localDescription.type,
          sdp: connection.localDescription.sdp,
          viewportWidth: info.viewportWidth,
          viewportHeight: info.viewportHeight,
          monitor: currentMonitor(),
          peerId: previousPeerId,
        }),
      });
      if (!response.ok) throw new Error("recovery rejected");
      const answer = await response.json();
      if (connection !== peerConnection) return true;
      rtcPeerId = answer.peerId || previousPeerId;
      await connection.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
      return true;
    }
    // No live peer left: build a fresh one.
    await connectWebRTC({ recover: true });
    return true;
  } catch (_) {
    return false;
  }
}

async function activateWebRTC(connection) {
  if (
    connection !== peerConnection ||
    connection.connectionState !== "connected" ||
    !rtcVideoReady
  ) return;
  try {
    await screenVideo.play();
  } catch (_) {
    screenVideo.muted = true;
    try {
      await screenVideo.play();
      if (rtcHadAudio) audioState.textContent = "\u70b9\u51fb\u753b\u9762\u5f00\u542f\u58f0\u97f3";
    } catch (_) {
      if (connection === peerConnection) fallbackToWebSocket("WebRTC\u64ad\u653e\u5931\u8d25\uff0c\u4f7f\u7528WebSocket");
      return;
    }
  }
  if (connection !== peerConnection || connection.connectionState !== "connected") return;
  setScreenTransport("webrtc");
  if (!lastRtcFrameAt) lastRtcFrameAt = performance.now();
  setConnectionState("\u5df2\u8fde\u63a5", "connected");
  connectInputWebSocket();
  if (rtcFallbackTimer) {
    window.clearTimeout(rtcFallbackTimer);
    rtcFallbackTimer = null;
  }
  // WebRTC carries the screen and control again, so the JPEG channel is idle.
  closeMainWebSocket();
  closeAudioWebSocket();
  reconnectAttempts = 0;
}

function sendClientInfo(target) {
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const viewportWidth = Math.round(Math.max(viewer.clientWidth, 320) * dpr);
  const viewportHeight = Math.round(Math.max(viewer.clientHeight, 240) * dpr);
  const payload = {
    type: "client_info",
    viewportWidth,
    viewportHeight,
    displayWidth: window.screen ? window.screen.width : viewportWidth,
    displayHeight: window.screen ? window.screen.height : viewportHeight,
  };
  if (target && target.readyState === "open") {
    try {
      target.send(JSON.stringify(payload));
    } catch (_) {}
  }
  return payload;
}

function iceServerList() {
  const configured = Array.isArray(hostStatus?.iceServers) ? hostStatus.iceServers : [];
  if (configured.length) {
    return configured
      .map((entry) => {
        const urls = Array.isArray(entry.urls) ? entry.urls : [entry.urls];
        const cleaned = urls.filter((url) => typeof url === "string" && url.trim());
        if (!cleaned.length) return null;
        const server = { urls: cleaned };
        if (entry.username) server.username = entry.username;
        if (entry.credential) server.credential = entry.credential;
        return server;
      })
      .filter(Boolean);
  }
  const stunUrls = Array.isArray(hostStatus?.stunUrls)
    ? hostStatus.stunUrls.filter((url) => typeof url === "string" && url.trim())
    : [];
  return stunUrls.length ? [{ urls: stunUrls }] : [];
}

async function connectWebRTC(options = {}) {
  if (!window.RTCPeerConnection || !hostStatus || !hostStatus.webrtc) {
    fallbackToWebSocket();
    return;
  }
  const recovering = Boolean(options.recover);
  closeWebRTC();
  const configuration = { iceServers: iceServerList() };
  if (hostStatus.iceTransportPolicy === "relay") configuration.iceTransportPolicy = "relay";
  const connection = new RTCPeerConnection(configuration);
  peerConnection = connection;
  rtcMediaStream = new MediaStream();
  screenVideo.srcObject = rtcMediaStream;
  // Two channels: state-changing control travels reliably, pointer motion is
  // lossy so it can never delay a click or a key release.
  const control = connection.createDataChannel("control", { ordered: true });
  const motion = connection.createDataChannel("motion", {
    ordered: false,
    maxRetransmits: 0,
  });
  rtcControl = control;
  rtcMotion = motion;
  control.addEventListener("open", () => {
    sendClientInfo(control);
    flushHeldKeys();
  });
  const recoverControl = () => {
    if (connection !== peerConnection || remotePanel.classList.contains("hidden")) return;
    connectInputWebSocket();
  };
  control.addEventListener("close", recoverControl);
  control.addEventListener("error", recoverControl);
  connection.addTransceiver("video", { direction: "recvonly" });
  connection.addTransceiver("audio", { direction: "recvonly" });
  connection.addEventListener("track", (event) => {
    rtcMediaStream.addTrack(event.track);
    if (event.track.kind === "video") {
      rtcVideoReady = true;
      observeRtcFrames(connection);
      activateWebRTC(connection);
      event.track.addEventListener("ended", () => {
        if (connection === peerConnection && !remotePanel.classList.contains("hidden")) {
          fallbackToWebSocket("WebRTC\u5df2\u7ed3\u675f\uff0c\u4f7f\u7528WebSocket");
        }
      }, { once: true });
    } else if (event.track.kind === "audio") {
      rtcHadAudio = true;
      audioState.textContent = screenVideo.muted ? "\u70b9\u51fb\u753b\u9762\u5f00\u542f\u58f0\u97f3" : "\u58f0\u97f3\u5df2\u5f00\u542f";
    }
  });
  connection.addEventListener("connectionstatechange", () => {
    if (connection !== peerConnection) return;
    const state = connection.connectionState;
    if (state === "connected") {
      activateWebRTC(connection);
      return;
    }
    if (state === "disconnected") {
      // A short drop is normal on Wi-Fi. Hold the peer and let ICE recover.
      setConnectionState("网络抖动，保持WebRTC", "connecting");
      rtcGraceUntil = performance.now() + 10000;
      return;
    }
    if (state === "failed") {
      setConnectionState("WebRTC中断，尝试恢复", "connecting");
      rtcGraceUntil = performance.now() + 6000;
      restartIce().then((ok) => {
        if (!ok && connection === peerConnection) {
          fallbackToWebSocket("WebRTC重连失败，使用WebSocket");
        }
      });
      return;
    }
    if (state === "closed" && !remotePanel.classList.contains("hidden")) {
      fallbackToWebSocket();
    }
  });
  setConnectionState(recovering ? "WebRTC恢复中" : "WebRTC\u8fde\u63a5\u4e2d", "connecting");
  rtcFallbackTimer = window.setTimeout(
    () => fallbackToWebSocket("WebRTC\u8d85\u65f6\uff0c\u4f7f\u7528WebSocket"),
    14000,
  );
  try {
    await connection.setLocalDescription(await connection.createOffer());
    await waitForIceGatheringComplete(connection);
    const info = sendClientInfo(null);
    const endpoint = recovering ? "/api/webrtc/recover" : "/api/webrtc/offer";
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type: connection.localDescription.type,
        sdp: connection.localDescription.sdp,
        viewportWidth: info.viewportWidth,
        viewportHeight: info.viewportHeight,
        monitor: currentMonitor(),
        peerId: rtcPeerId,
      }),
    });
    if (response.status === 401) {
      showLogin("登录已过期，请重新输入密码");
      return;
    }
    if (!response.ok) throw new Error("WebRTC offer rejected");
    const answer = await response.json();
    if (connection !== peerConnection) return;
    rtcPeerId = answer.peerId || "";
    await connection.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    if (!answer.audio) audioState.textContent = "\u7cfb\u7edf\u58f0\u97f3\u4e0d\u53ef\u7528";
  } catch (_) {
    if (connection === peerConnection) {
      if (recovering) scheduleWebRTCRecovery();
      else fallbackToWebSocket();
    }
  }
}

function startPreferredTransport() {
  if (hostStatus && hostStatus.webrtc) {
    connectWebRTC();
  } else {
    fallbackToWebSocket();
  }
}

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  ws = socket;
  socket.binaryType = "arraybuffer";
  setConnectionState("\u8fde\u63a5\u4e2d", "connecting");
  socket.addEventListener("open", () => {
    if (ws !== socket) return;
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    reconnectAttempts = 0;
    lastMainMessageAt = performance.now();
    setConnectionState("\u5df2\u8fde\u63a5", "connected");
    setScreenTransport("websocket");
    sendClientInfo(socket);
    ensureAudio();
    connectInputWebSocket();
    connectAudioWebSocket();
    if (clipboardSyncEnabled) requestClipboardSync();
  });

  socket.addEventListener("close", () => {
    if (ws !== socket) return;
    ws = null;
    setConnectionState("\u5df2\u65ad\u5f00", "closed");
    closeAudioWebSocket();
    if (!shouldReconnect || remotePanel.classList.contains("hidden")) return;
    if (rtcConnected) {
      // WebRTC still carries the screen; only the JPEG channel is gone.
      scheduleWebRTCRecovery();
      return;
    }
    scheduleMainReconnect();
  });
  socket.addEventListener("message", (event) => {
    if (ws !== socket) return;
    lastMainMessageAt = performance.now();
    handleSocketMessage(event, handleMainJsonMessage, handleMainBinaryMessage).catch(() => {
      if (ws === socket && socket.readyState === WebSocket.OPEN) socket.close();
    });
  });
}

function connectInputWebSocket() {
  if (inputWs && (inputWs.readyState === WebSocket.OPEN || inputWs.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (rtcControl && rtcControl.readyState === "open") {
    // The data channel is the better control path; the socket is not needed.
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws/input`);
  inputWs = socket;
  socket.addEventListener("open", () => {
    if (inputWs !== socket) return;
    if (inputReconnectTimer) {
      window.clearTimeout(inputReconnectTimer);
      inputReconnectTimer = null;
    }
    inputReconnectAttempts = 0;
    try {
      socket.send(JSON.stringify({ type: "client_info", inputOnly: true }));
    } catch (_) {}
    flushHeldKeys();
  });
  socket.addEventListener("message", (event) => {
    if (inputWs !== socket || typeof event.data !== "string") return;
    try {
      handleMainJsonMessage(JSON.parse(event.data));
    } catch (_) {
      socket.close();
    }
  });
  socket.addEventListener("close", () => {
    if (inputWs !== socket) return;
    inputWs = null;
    if (!shouldReconnect || remotePanel.classList.contains("hidden")) return;
    if (rtcControl && rtcControl.readyState === "open") return;
    scheduleInputReconnect();
  });
}

function closeInputWebSocket() {
  if (inputReconnectTimer) {
    window.clearTimeout(inputReconnectTimer);
    inputReconnectTimer = null;
  }
  const socket = inputWs;
  inputWs = null;
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    socket.close();
  }
}

async function unpackBinaryMessage(data) {
  const buffer = data instanceof Blob ? await data.arrayBuffer() : data;
  const view = new DataView(buffer);
  const headerLength = view.getUint32(0);
  const headerStart = 4;
  const payloadStart = headerStart + headerLength;
  if (headerLength < 1 || payloadStart > buffer.byteLength) {
    throw new Error("bad binary message");
  }
  const header = new Uint8Array(buffer, headerStart, headerLength);
  return {
    meta: JSON.parse(binaryDecoder.decode(header)),
    payload: buffer.slice(payloadStart),
  };
}

async function handleSocketMessage(event, jsonHandler, binaryHandler) {
  if (typeof event.data === "string") {
    jsonHandler(JSON.parse(event.data));
    return;
  }
  const { meta, payload } = await unpackBinaryMessage(event.data);
  binaryHandler(meta, payload);
}

// ---------------------------------------------------------------- frames
function markScreenReady() {
  if (rtcConnected) screenVideo.classList.add("ready");
  else screen.classList.add("ready");
  emptyState.classList.add("hidden");
}

function renderFrameBlob(payload, format = "jpeg", signature = null) {
  const mime = format === "webp" ? "image/webp" : "image/jpeg";
  const nextUrl = URL.createObjectURL(new Blob([payload], { type: mime }));
  const previousUrl = screenObjectUrl;
  screenObjectUrl = nextUrl;
  frameRendering = true;
  let settled = false;
  const finish = () => {
    if (settled) return;
    settled = true;
    if (frameLoadTimer) {
      window.clearTimeout(frameLoadTimer);
      frameLoadTimer = null;
    }
    if (previousUrl) URL.revokeObjectURL(previousUrl);
    frameRendering = false;
    if (signature) lastRenderedSignature = signature;
    screen.onload = null;
    screen.onerror = null;
    if (pendingFrame) {
      const next = pendingFrame;
      pendingFrame = null;
      showFrameBlob(next.payload, next.format, next.signature);
    }
  };
  screen.onload = () => {
    finish();
  };
  screen.onerror = () => {
    finish();
  };
  frameLoadTimer = window.setTimeout(finish, 5000);
  screen.src = nextUrl;
  markScreenReady();
}

function showFrameBlob(payload, format = "jpeg", signature = null) {
  if (signature && signature === lastRenderedSignature) {
    return;
  }
  if (signature && signature === pendingFrame?.signature) {
    return;
  }
  metrics.bytes += payload.byteLength || 0;
  if (frameRendering) {
    // Keep only the newest frame: an old one is worthless for a live screen.
    if (pendingFrame) droppedFrames += 1;
    pendingFrame = { payload, format, signature };
    return;
  }
  renderFrameBlob(payload, format, signature);
}

function clearScreenObjectUrl() {
  if (frameLoadTimer) {
    window.clearTimeout(frameLoadTimer);
    frameLoadTimer = null;
  }
  if (screenObjectUrl) {
    URL.revokeObjectURL(screenObjectUrl);
    screenObjectUrl = null;
  }
  pendingFrame = null;
  frameRendering = false;
  lastRenderedSignature = null;
}

function handleMainJsonMessage(msg) {
  if (msg.type === "frame") {
    showFrameBase64(msg.jpeg);
  } else if (msg.type === "clipboard") {
    handleRemoteClipboard(msg);
  } else if (msg.type === "text_focus") {
    remoteTextFocusActive = Boolean(msg.active);
    if (remoteTextFocusActive) {
      focusKeyboardSink();
    } else if (document.activeElement === keyboardSink) {
      keyboardSink.blur();
    }
  } else if (msg.type === "cursor") {
    updateRemoteCursor(msg);
  } else if (msg.type === "metrics" || msg.type === "server_metrics") {
    handleServerMetrics(msg);
  } else if (msg.type === "audio_status" || msg.type === "audio") {
    handleAudioMessage(msg);
  } else if (msg.type === "transfer_cancel_ack") {
    markTransferCancelled(msg.transferId);
  } else if (msg.type === "monitor") {
    metrics.resolution = `${msg.width}\u00d7${msg.height}`;
    renderMetrics();
  } else if (msg.type === "pong") {
    const rtt = Date.now() - Number(msg.at || Date.now());
    if (Number.isFinite(rtt) && rtt >= 0) {
      metrics.rttFrames.push(rtt);
      if (metrics.rttFrames.length > 12) metrics.rttFrames.shift();
      metrics.latency = Math.round(
        metrics.rttFrames.reduce((sum, value) => sum + value, 0) / metrics.rttFrames.length,
      );
      renderMetrics();
    }
  } else if (msg.type === "error") {
    setConnectionState(msg.message, "closed");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
    }
  }
}

function handleMainBinaryMessage(meta, payload) {
  if (meta.type === "frame") {
    metrics.bytes += payload.byteLength || 0;
    metrics.targetFps = meta.fps || metrics.targetFps;
    if (meta.width) metrics.resolution = `${meta.width}\u00d7${meta.height}`;
    showFrameBlob(payload, meta.format, meta.signature || null);
  } else if (meta.type === "audio") {
    handleAudioBinaryMessage(meta, payload);
  }
}

function showFrameBase64(jpeg) {
  clearScreenObjectUrl();
  screen.src = `data:image/jpeg;base64,${jpeg}`;
  markScreenReady();
}

// ---------------------------------------------------------------- cursor
function updateRemoteCursor(msg) {
  const visible = msg.visible !== false;
  remoteCursorVisible = visible;
  remoteCursor.classList.toggle("hidden", !visible);
  if (!visible) return;
  const x = Math.max(0, Math.min(1, Number(msg.x) || 0));
  const y = Math.max(0, Math.min(1, Number(msg.y) || 0));
  const target = rtcConnected ? screenVideo : screen;
  const rect = target.getBoundingClientRect();
  const stageRect = stage.getBoundingClientRect();
  const left = rect.left - stageRect.left + x * rect.width;
  const top = rect.top - stageRect.top + y * rect.height;
  remoteCursor.style.transform = `translate(${Math.round(left - 4)}px, ${Math.round(top - 2)}px)`;
  remoteCursorAt = performance.now();
}

function hideStaleCursor() {
  if (remoteCursorVisible && performance.now() - remoteCursorAt > 4000) {
    remoteCursor.classList.add("hidden");
    remoteCursorVisible = false;
  }
}

// ---------------------------------------------------------------- audio
function connectAudioWebSocket() {
  if (rtcConnected) return;
  if (audioWs && (audioWs.readyState === WebSocket.OPEN || audioWs.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws/audio`);
  socket.binaryType = "arraybuffer";
  audioWs = socket;
  socket.addEventListener("open", () => {
    if (audioWs !== socket) return;
    if (audioReconnectTimer) {
      window.clearTimeout(audioReconnectTimer);
      audioReconnectTimer = null;
    }
    audioReconnectAttempts = 0;
    audioDecoderState = null;
    ensureAudio();
  });
  socket.addEventListener("close", () => {
    if (audioWs !== socket) return;
    audioWs = null;
    if (!shouldReconnect || remotePanel.classList.contains("hidden") || rtcConnected) return;
    audioState.textContent = "\u58f0\u97f3\u91cd\u8fde\u4e2d";
    scheduleAudioReconnect();
  });
  socket.addEventListener("message", (event) => {
    handleSocketMessage(event, handleAudioMessage, handleAudioBinaryMessage).catch(() => {});
  });
}

function closeAudioWebSocket() {
  if (audioReconnectTimer) {
    window.clearTimeout(audioReconnectTimer);
    audioReconnectTimer = null;
  }
  const socket = audioWs;
  audioWs = null;
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    socket.close();
  }
}

function handleAudioMessage(msg) {
  if (msg.type === "audio_status") {
    audioState.textContent = msg.ok
      ? `\u58f0\u97f3\u5df2\u5f00\u542f${msg.codec === "adpcm" ? "（ADPCM）" : ""}`
      : `\u58f0\u97f3\u4e0d\u53ef\u7528 ${msg.message || ""}`;
  } else if (msg.type === "audio") {
    playPcm(msg);
  }
}

function handleAudioBinaryMessage(meta, payload) {
  if (meta.type !== "audio") return;
  metrics.bytes += payload.byteLength || 0;
  const bytes = new Uint8Array(payload);
  if (meta.codec === "adpcm") {
    const samples = decodeAdpcm(bytes, meta.channels || 2, meta.frames || 960);
    scheduleAudio(samples, meta.sampleRate, meta.channels || 2);
    return;
  }
  const usable = bytes.byteLength - (bytes.byteLength % ((meta.channels || 2) * 2));
  const view = new DataView(bytes.buffer, bytes.byteOffset, usable);
  const samples = new Int16Array(usable / 2);
  for (let i = 0; i < samples.length; i += 1) samples[i] = view.getInt16(i * 2, true);
  scheduleAudio(samples, meta.sampleRate, meta.channels || 2);
}

// IMA ADPCM decoder mirroring app/audio.py.
const ADPCM_STEP_TABLE = [
  7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
  34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
  157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
  598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
  2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
  6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
  18500, 20350, 22385, 24623, 27086, 29794, 32767,
];
const ADPCM_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8];

function decodeAdpcm(bytes, channels, frames) {
  const perChannel = Math.ceil(frames / 2);
  const out = new Int16Array(frames * channels);
  if (!audioDecoderState || audioDecoderState.channels !== channels) {
    audioDecoderState = {
      channels,
      predictors: new Array(channels).fill(0),
      indices: new Array(channels).fill(0),
    };
  }
  for (let channel = 0; channel < channels; channel += 1) {
    const start = channel * perChannel;
    if (start >= bytes.length) break;
    const end = Math.min(bytes.length, start + perChannel);
    let predictor = audioDecoderState.predictors[channel];
    let index = audioDecoderState.indices[channel];
    let produced = 0;
    for (let offset = start; offset < end && produced < frames; offset += 1) {
      const byte = bytes[offset];
      for (const code of [byte & 0x0f, (byte >> 4) & 0x0f]) {
        if (produced >= frames) break;
        const step = ADPCM_STEP_TABLE[index];
        let delta = step >> 3;
        if (code & 4) delta += step;
        if (code & 2) delta += step >> 1;
        if (code & 1) delta += step >> 2;
        predictor += code & 8 ? -delta : delta;
        if (predictor > 32767) predictor = 32767;
        else if (predictor < -32768) predictor = -32768;
        index += ADPCM_INDEX_TABLE[code];
        if (index < 0) index = 0;
        else if (index > 88) index = 88;
        out[produced * channels + channel] = predictor;
        produced += 1;
      }
    }
    audioDecoderState.predictors[channel] = predictor;
    audioDecoderState.indices[channel] = index;
  }
  return out;
}

function ensureAudio() {
  if (rtcConnected && screenVideo.muted) {
    screenVideo.muted = false;
    screenVideo.play().then(() => {
      if (rtcHadAudio) audioState.textContent = "\u58f0\u97f3\u5df2\u5f00\u542f";
    }).catch(() => {
      screenVideo.muted = true;
      if (rtcHadAudio) audioState.textContent = "\u6d4f\u89c8\u5668\u963b\u6b62\u58f0\u97f3\u64ad\u653e";
    });
  }
  if (!audioContext) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) {
      audioContext = new Ctx({ latencyHint: "interactive" });
      nextAudioTime = audioContext.currentTime + jitterTarget();
    }
  }
  if (audioContext && audioContext.state === "suspended") {
    audioContext.resume();
  }
}

function jitterTarget() {
  const configured = Number(hostStatus?.audioJitterSeconds);
  return Number.isFinite(configured) && configured > 0 ? Math.min(1.0, configured) : 0.18;
}

async function requestWakeLock() {
  if (!navigator.wakeLock) return;
  try {
    await navigator.wakeLock.request("screen");
  } catch (_) {}
}

function playPcm(msg) {
  const bytes = Uint8Array.from(atob(msg.pcm), (ch) => ch.charCodeAt(0));
  const samples = new Int16Array(bytes.buffer);
  scheduleAudio(samples, msg.sampleRate, msg.channels || 2);
}

function scheduleAudio(samples, sampleRate, channels = 2) {
  ensureAudio();
  if (!audioContext) return;
  const frames = Math.floor(samples.length / channels);
  if (!frames) return;
  const buffer = audioContext.createBuffer(channels, frames, sampleRate);
  for (let ch = 0; ch < channels; ch += 1) {
    const out = buffer.getChannelData(ch);
    for (let i = 0; i < frames; i += 1) {
      out[i] = samples[i * channels + ch] / 32768;
    }
  }
  const src = audioContext.createBufferSource();
  src.buffer = buffer;
  src.connect(audioContext.destination);
  const now = audioContext.currentTime;
  let startAt = Math.max(now + 0.02, nextAudioTime);
  // Jitter buffer: hold a small lead, and resynchronise instead of letting the
  // lead grow without bound after a network pause.
  const lead = startAt - now;
  audioDrift = lead;
  if (lead > 1.2 || lead < 0) {
    startAt = now + jitterTarget();
    audioResyncs += 1;
  }
  src.start(startAt);
  nextAudioTime = startAt + buffer.duration;
}

// ---------------------------------------------------------------- input send
function channelBuffered(channel) {
  try {
    return Number(channel.bufferedAmount || 0);
  } catch (_) {
    return 0;
  }
}

function sendInput(payload, reliable) {
  const message = JSON.stringify({ type: "input", ...payload });
  const needsReliable = reliable === true || isReliableInput(payload);
  if (rtcControl && rtcControl.readyState === "open") {
    if (needsReliable) {
      if (channelBuffered(rtcControl) > RELIABLE_BYTES_SOFT_LIMIT) {
        // The control channel is backed up on a weak link; the input socket is
        // the safer path for an event that must not be lost.
        if (inputWs && inputWs.readyState === WebSocket.OPEN) {
          inputWs.send(message);
          return;
        }
      }
      rtcControl.send(message);
      return;
    }
    if (rtcMotion && rtcMotion.readyState === "open") {
      if (channelBuffered(rtcMotion) > MOTION_BYTES_SOFT_LIMIT) return;
      rtcMotion.send(message);
      return;
    }
    if (channelBuffered(rtcControl) > MOTION_BYTES_SOFT_LIMIT) return;
    rtcControl.send(message);
    return;
  }
  if (inputWs && inputWs.readyState === WebSocket.OPEN) {
    if (channelBuffered(inputWs) > RELIABLE_BYTES_SOFT_LIMIT) {
      if (needsReliable) inputWs.send(message);
      return;
    }
    inputWs.send(message);
    return;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(message);
  }
}

function isReliableInput(payload) {
  if (payload.kind === "move" || payload.kind === "mouse_delta") return false;
  if (payload.kind === "touch") {
    const contacts = Array.isArray(payload.contacts) ? payload.contacts : [];
    return !contacts.length || contacts.some((contact) => contact?.state !== "update");
  }
  return true;
}

function sendText(text) {
  if (!text) return;
  sendInput({ kind: "text", text }, true);
}

function requestClipboardSync(since = clipboardRevision) {
  if (!clipboardSyncEnabled) return;
  sendInput({ type: "clipboard_poll", since }, true);
}

function handleRemoteClipboard(msg) {
  const revision = Number(msg.revision || 0);
  if (revision && revision < clipboardRevision) return;
  clipboardRevision = revision || clipboardRevision;
  const text = typeof msg.text === "string" ? msg.text : "";
  if (!clipboardSyncEnabled || !text || text === lastAutoClipboardText) return;
  lastAutoClipboardText = text;
  if (!navigator.clipboard?.writeText) return;
  if (document.hidden) return;
  navigator.clipboard.writeText(text).catch(() => {
    suppressClipboardEcho = true;
  });
}

// ---------------------------------------------------------------- keyboard
function stripKeyboardSentinel(text) {
  return String(text || "").split(KEYBOARD_SENTINEL).join("");
}

function resetKeyboardSink() {
  keyboardSink.value = KEYBOARD_SENTINEL;
  const pos = keyboardSink.value.length;
  try {
    keyboardSink.setSelectionRange(pos, pos);
  } catch (error) {
    // Some Android WebViews reject selection changes while the IME is composing.
  }
}

function focusKeyboardSink() {
  if (remotePanel.classList.contains("hidden")) return;
  resetKeyboardSink();
  try {
    keyboardSink.focus({ preventScroll: true });
  } catch (error) {
    keyboardSink.focus();
  }
  window.setTimeout(resetKeyboardSink, 0);
}

function armKeyboardAfterTouch() {
  keyboardArmedUntil = performance.now() + 1200;
  const tick = () => {
    if (performance.now() > keyboardArmedUntil) return;
    if (remoteTextFocusActive) {
      focusKeyboardSink();
      return;
    }
    window.setTimeout(tick, 80);
  };
  tick();
}

keyboardSink.addEventListener("compositionstart", () => {
  composingText = true;
});

keyboardSink.addEventListener("compositionend", (event) => {
  composingText = false;
  const text = stripKeyboardSentinel(event.data || keyboardSink.value);
  sendText(text);
  resetKeyboardSink();
  ignoreNextKeyboardInput = true;
  window.setTimeout(() => {
    ignoreNextKeyboardInput = false;
  }, 0);
});

keyboardSink.addEventListener("beforeinput", (event) => {
  if (composingText) return;
  if (event.inputType === "insertLineBreak") {
    event.preventDefault();
    sendInput({ kind: "key", key: "Enter", action: "press" }, true);
    resetKeyboardSink();
    return;
  }
  if (event.inputType === "deleteContentBackward") {
    event.preventDefault();
    sendInput({ kind: "key", key: "Backspace", action: "press" }, true);
    resetKeyboardSink();
    return;
  }
  if (event.inputType === "deleteContentForward") {
    event.preventDefault();
    sendInput({ kind: "key", key: "Delete", action: "press" }, true);
    resetKeyboardSink();
    return;
  }
  if (event.inputType && event.inputType.startsWith("insert")) {
    const text = stripKeyboardSentinel(event.data || "");
    if (text) {
      event.preventDefault();
      sendText(text);
      resetKeyboardSink();
    }
  }
});

keyboardSink.addEventListener("input", () => {
  if (composingText || ignoreNextKeyboardInput) return;
  const text = stripKeyboardSentinel(keyboardSink.value);
  if (!text) {
    resetKeyboardSink();
    return;
  }
  sendText(text);
  resetKeyboardSink();
});

function isModifierKey(key) {
  return ["Control", "Shift", "Alt", "Meta", "AltGraph", "CapsLock"].includes(key);
}

document.addEventListener("keydown", (event) => {
  if (remotePanel.classList.contains("hidden")) return;
  const active = document.activeElement;
  if (active === keyboardSink || active === passwordInput) return;
  event.preventDefault();
  if (!event.repeat || !isModifierKey(event.key)) {
    const token = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    heldKeys.add(token);
    sendInput(
      {
        kind: "key",
        key: event.key,
        action: "down",
        token,
        repeat: event.repeat,
        ctrl: event.ctrlKey,
        alt: event.altKey,
        shift: event.shiftKey,
        meta: event.metaKey,
      },
      true,
    );
  } else {
    // Auto-repeat of a printable key: deliver the repeat as a tap so long
    // presses keep typing instead of flooding key-down events.
    sendInput({ kind: "key", key: event.key, action: "press" }, true);
  }
});

document.addEventListener("keyup", (event) => {
  if (remotePanel.classList.contains("hidden")) return;
  const active = document.activeElement;
  if (active === keyboardSink || active === passwordInput) return;
  event.preventDefault();
  const token = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  heldKeys.delete(token);
  sendInput({ kind: "key", key: event.key, action: "up", token }, true);
});

function releaseAllKeys() {
  for (const token of Array.from(heldKeys)) {
    sendInput({ kind: "key", key: token, action: "up", token }, true);
  }
  heldKeys.clear();
}

function flushHeldKeys() {
  // After a reconnect the host may have released everything; re-assert the
  // keys the user is still physically holding.
  for (const token of heldKeys) {
    sendInput({ kind: "key", key: token, action: "down", token }, true);
  }
}

// ---------------------------------------------------------------- touch/pointer
function normalizedPoint(event) {
  const target = rtcConnected ? screenVideo : screen;
  const rect = target.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) return null;
  const x = (event.clientX - rect.left) / rect.width;
  const y = (event.clientY - rect.top) / rect.height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x, y };
}

function pointerSample(event) {
  const points =
    typeof event.getCoalescedEvents === "function" ? event.getCoalescedEvents() : [];
  const last = points.length ? points[points.length - 1] : event;
  return {
    clientX: last.clientX,
    clientY: last.clientY,
    pointerId: event.pointerId,
    pointerType: event.pointerType,
    button: event.button,
  };
}

function touchContact(event, state) {
  const point = normalizedPoint(event);
  if (!point) return null;
  return {
    id: event.pointerId,
    x: point.x,
    y: point.y,
    state,
  };
}

function sendTouchFrame(changedEvent, changedState, force = false) {
  const now = performance.now();
  if (!force && now - lastTouchFrameAt < 16) return;
  lastTouchFrameAt = now;

  const contacts = [];
  for (const [id, event] of activeTouches) {
    const contact = touchContact(event, id === changedEvent.pointerId ? changedState : "update");
    if (contact) contacts.push(contact);
  }
  if (contacts.length) {
    // down/up/cancel are state changes and must never be dropped; a pure
    // "update" frame is motion and may be.
    sendInput({ kind: "touch", contacts }, changedState !== "update" || force);
  }
}

function handleTouchDown(event) {
  ensureAudio();
  viewer.setPointerCapture(event.pointerId);
  viewer.focus();
  const sample = pointerSample(event);
  activeTouches.set(event.pointerId, sample);
  sendTouchFrame(sample, "down", true);
}

function handleTouchMove(event) {
  if (!activeTouches.has(event.pointerId)) return;
  const sample = pointerSample(event);
  activeTouches.set(event.pointerId, sample);
  sendTouchFrame(sample, "update");
}

function handleTouchUp(event, state = "up") {
  const sample = pointerSample(event);
  activeTouches.set(event.pointerId, sample);
  sendTouchFrame(sample, state, true);
  activeTouches.delete(event.pointerId);
  if (state === "up") {
    armKeyboardAfterTouch();
  }
}

function cancelActiveTouches() {
  const samples = Array.from(activeTouches.values());
  for (const sample of samples) {
    sendTouchFrame(sample, "cancel", true);
    activeTouches.delete(sample.pointerId);
  }
  if (mouseDragLocked) {
    setMouseDragLocked(false);
  }
}

// ---------------------------------------------------------------- pan & zoom
function setZoom(next, focusPoint = null) {
  const previous = zoom;
  zoom = Math.max(0.7, Math.min(3.0, next));
  if (Math.abs(zoom - previous) < 0.0001) return;
  applyZoom(focusPoint);
}

function applyZoom(focusPoint = null) {
  const centerX = viewer.scrollLeft + viewer.clientWidth / 2;
  const centerY = viewer.scrollTop + viewer.clientHeight / 2;
  const anchor = focusPoint || { x: centerX, y: centerY };
  const ratio = zoom / (stage.dataset.zoom ? Number(stage.dataset.zoom) : 1);
  stage.dataset.zoom = String(zoom);
  stage.style.transform = `scale(${zoom})`;
  // Keep the point under the finger stable while zooming.
  const offsetX = anchor.x * (ratio - 1);
  const offsetY = anchor.y * (ratio - 1);
  stage.style.left = `${-offsetX}px`;
  stage.style.top = `${-offsetY}px`;
  zoomReset.textContent = `${Math.round(zoom * 100)}%`;
  layoutStage();
}

function layoutStage() {
  const target = rtcConnected ? screenVideo : screen;
  const naturalWidth = target.clientWidth || viewer.clientWidth;
  const naturalHeight = target.clientHeight || viewer.clientHeight;
  const scaledWidth = naturalWidth * zoom;
  const scaledHeight = naturalHeight * zoom;
  stage.style.width = `${naturalWidth}px`;
  stage.style.height = `${naturalHeight}px`;
  viewer.style.overflow = "auto";
  viewer.dataset.scaledWidth = String(Math.round(scaledWidth));
  viewer.dataset.scaledHeight = String(Math.round(scaledHeight));
  stage.style.transformOrigin = "0 0";
}

viewer.addEventListener("contextmenu", (event) => event.preventDefault());

viewer.addEventListener("pointerdown", (event) => {
  if (panMode) {
    event.preventDefault();
    panPointerId = event.pointerId;
    panStart = {
      x: event.clientX,
      y: event.clientY,
      left: viewer.scrollLeft,
      top: viewer.scrollTop,
    };
    viewer.setPointerCapture(event.pointerId);
    return;
  }
  event.preventDefault();
  if (event.pointerType === "touch") {
    handleTouchDown(event);
    return;
  }
  ensureAudio();
  viewer.focus();
  const sample = pointerSample(event);
  const p = normalizedPoint(sample);
  if (p) sendInput({ kind: "down", ...p, button: sample.button }, true);
});

viewer.addEventListener("pointermove", (event) => {
  if (panMode && panPointerId === event.pointerId && panStart) {
    event.preventDefault();
    viewer.scrollLeft = panStart.left - (event.clientX - panStart.x);
    viewer.scrollTop = panStart.top - (event.clientY - panStart.y);
    return;
  }
  if (panMode) return;
  event.preventDefault();
  if (event.pointerType === "touch") {
    handleTouchMove(event);
    return;
  }
  const now = performance.now();
  if (now - lastMoveAt < 12) return;
  lastMoveAt = now;
  const sample = pointerSample(event);
  const p = normalizedPoint(sample);
  if (p) sendInput({ kind: "move", ...p, button: sample.button }, false);
});

function endPan(event) {
  if (panPointerId !== event.pointerId) return;
  panPointerId = null;
  panStart = null;
}

viewer.addEventListener("pointerup", (event) => {
  if (panMode) {
    endPan(event);
    return;
  }
  event.preventDefault();
  if (event.pointerType === "touch") {
    handleTouchUp(event);
    return;
  }
  const sample = pointerSample(event);
  const p = normalizedPoint(sample);
  if (p) sendInput({ kind: "up", ...p, button: sample.button }, true);
});

viewer.addEventListener("pointercancel", (event) => {
  endPan(event);
  if (event.pointerType === "touch" && activeTouches.has(event.pointerId)) {
    handleTouchUp(event, "cancel");
  }
});

viewer.addEventListener("dblclick", (event) => {
  if (event.pointerType === "touch" || panMode) return;
  const p = normalizedPoint(pointerSample(event));
  if (p) sendInput({ kind: "double", ...p, button: event.button }, true);
});

viewer.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    if (event.ctrlKey) {
      setZoom(zoom - Math.sign(event.deltaY) * 0.12, { x: event.clientX, y: event.clientY });
      return;
    }
    const p = normalizedPoint(event);
    if (!p) return;
    sendInput(
      {
        kind: "wheel",
        ...p,
        delta: Math.sign(event.deltaY) * 4,
        deltaX: Math.sign(event.deltaX) * 4,
      },
      true,
    );
  },
  { passive: false },
);

// Pinch zoom while panning is enabled.
const pinchState = new Map();
viewer.addEventListener(
  "touchstart",
  (event) => {
    if (event.touches.length === 2) {
      event.preventDefault();
      for (const touch of event.touches) pinchState.set(touch.identifier, touch);
    }
  },
  { passive: false },
);
viewer.addEventListener(
  "touchmove",
  (event) => {
    if (event.touches.length !== 2) return;
    event.preventDefault();
    const previous = Array.from(pinchState.values());
    pinchState.clear();
    for (const touch of event.touches) pinchState.set(touch.identifier, touch);
    if (previous.length !== 2) return;
    const distance = Math.hypot(
      event.touches[0].clientX - event.touches[1].clientX,
      event.touches[0].clientY - event.touches[1].clientY,
    );
    const previousDistance = Math.hypot(
      previous[0].clientX - previous[1].clientX,
      previous[0].clientY - previous[1].clientY,
    );
    if (!previousDistance) return;
    const factor = distance / previousDistance;
    if (Math.abs(factor - 1) < 0.004) return;
    setZoom(zoom * factor, {
      x: (event.touches[0].clientX + event.touches[1].clientX) / 2,
      y: (event.touches[0].clientY + event.touches[1].clientY) / 2,
    });
  },
  { passive: false },
);
viewer.addEventListener("touchend", (event) => {
  if (event.touches.length < 2) pinchState.clear();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    cancelActiveTouches();
    return;
  }
  lastMainMessageAt = performance.now();
  lastRtcFrameAt = performance.now();
  if (remotePanel.classList.contains("hidden")) return;
  if (rtcConnected) {
    screenVideo.play().catch(() => {});
    if (rtcControl && rtcControl.readyState === "open") flushHeldKeys();
  } else {
    connectWebSocket();
    connectInputWebSocket();
  }
});

window.addEventListener("pagehide", () => {
  releaseAllKeys();
  cancelActiveTouches();
});

// ---------------------------------------------------------------- monitors
function monitorList() {
  return Array.isArray(hostStatus?.monitors) ? hostStatus.monitors : [];
}

function currentMonitor() {
  const stored = Number(window.localStorage?.getItem("trd-monitor") || "0");
  return stored > 0 ? stored : Number(hostStatus?.monitor || 1);
}

function updateMonitorButton() {
  const list = monitorList();
  const active = currentMonitor();
  monitorButton.textContent = `\u5c4f${active}`;
  monitorButton.disabled = list.length <= 1;
  monitorButton.title = list.length
    ? list.map((item) => `\u5c4f${item.index}: ${item.width}\u00d7${item.height}`).join("\n")
    : "\u5355\u663e\u793a\u5668";
}

monitorButton.addEventListener("click", () => {
  const list = monitorList();
  if (list.length <= 1) return;
  const active = currentMonitor();
  const position = list.findIndex((item) => item.index === active);
  const next = list[(position + 1) % list.length];
  window.localStorage?.setItem("trd-monitor", String(next.index));
  updateMonitorButton();
  sendInput({ type: "monitor_select", monitor: next.index }, true);
  closeMainWebSocket();
  closeWebRTC();
  setScreenTransport(hostStatus?.webrtc ? "webrtc" : "websocket");
  if (hostStatus?.webrtc) connectWebRTC();
  else connectWebSocket();
});

// ---------------------------------------------------------------- clipboard
async function copyHostClipboard() {
  const response = await fetch("/api/clipboard", { cache: "no-store" });
  if (response.status === 401) {
    showLogin("登录已过期，请重新输入密码");
    throw new Error("session expired");
  }
  if (!response.ok) throw new Error("host clipboard unavailable");
  const data = await response.json();
  const value = data.text || "";
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard API unavailable");
    await navigator.clipboard.writeText(value);
    return true;
  } catch (_) {
    window.prompt("\u6d4f\u89c8\u5668\u4e0d\u80fd\u76f4\u63a5\u5199\u5165\u526a\u8d34\u677f\uff0c\u8bf7\u624b\u52a8\u590d\u5236\uff1a", value);
    return false;
  }
}

async function sendLocalClipboard() {
  let value;
  try {
    if (!navigator.clipboard?.readText) throw new Error("clipboard API unavailable");
    value = await navigator.clipboard.readText();
  } catch (_) {
    value = window.prompt("\u6d4f\u89c8\u5668\u4e0d\u80fd\u76f4\u63a5\u8bfb\u53d6\u526a\u8d34\u677f\uff0c\u8bf7\u5728\u6b64\u7c98\u8d34\u540e\u53d1\u9001\uff1a", "");
    if (value === null) throw new Error("clipboard input cancelled");
  }
  const response = await fetch("/api/clipboard", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: value }),
  });
  if (response.status === 401) {
    showLogin("登录已过期，请重新输入密码");
    throw new Error("session expired");
  }
  if (!response.ok) throw new Error("host clipboard unavailable");
  lastAutoClipboardText = value;
}

pullClipboard.addEventListener("click", async () => {
  try {
    const copied = await copyHostClipboard();
    pullClipboard.textContent = copied ? "\u5df2\u590d\u5236" : "\u8bf7\u624b\u52a8\u590d\u5236";
  } catch (_) {
    pullClipboard.textContent = "\u590d\u5236\u5931\u8d25";
  }
  window.setTimeout(() => { pullClipboard.textContent = "\u53d6\u526a\u8d34\u677f"; }, 1200);
});

pushClipboard.addEventListener("click", async () => {
  try {
    await sendLocalClipboard();
    pushClipboard.textContent = "\u5df2\u53d1\u9001";
  } catch (_) {
    pushClipboard.textContent = "\u53d1\u9001\u5931\u8d25";
  }
  window.setTimeout(() => { pushClipboard.textContent = "\u53d1\u526a\u8d34\u677f"; }, 1200);
});

// ---------------------------------------------------------------- files
function formatBytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1048576).toFixed(1)} MB`;
  return `${(size / 1073741824).toFixed(2)} GB`;
}

function setFileRoot(root) {
  fileRoot = root;
  fileCurrentPath = "";
  fileRootUpload.classList.toggle("active", root === "upload");
  fileRootDownload.classList.toggle("active", root === "download");
  refreshFileList();
}

filesButton.addEventListener("click", () => {
  const hidden = filePanel.classList.toggle("hidden");
  if (!hidden) refreshFileList();
});
filePanelClose.addEventListener("click", () => filePanel.classList.add("hidden"));
fileRootUpload.addEventListener("click", () => setFileRoot("upload"));
fileRootDownload.addEventListener("click", () => setFileRoot("download"));
fileUpButton.addEventListener("click", () => {
  if (!fileCurrentPath) return;
  const parts = fileCurrentPath.split("/").filter(Boolean);
  parts.pop();
  fileCurrentPath = parts.join("/");
  refreshFileList();
});
fileSendButton.addEventListener("click", () => filePicker.click());
filePicker.addEventListener("change", () => {
  const files = Array.from(filePicker.files || []);
  filePicker.value = "";
  for (const file of files) uploadFile(file).catch(() => {});
});

async function refreshFileList() {
  if (!hostStatus?.fileTransfer) {
    fileList.innerHTML = '<div class="file-empty">\u4e3b\u673a\u672a\u542f\u7528\u6587\u4ef6\u4f20\u8f93</div>';
    return;
  }
  const query = new URLSearchParams({ root: fileRoot, path: fileCurrentPath });
  let response;
  try {
    response = await fetch(`/api/files/list?${query}`, { cache: "no-store" });
  } catch (_) {
    fileList.innerHTML = '<div class="file-empty">\u5217\u76ee\u5f55\u5931\u8d25</div>';
    return;
  }
  if (!response.ok) {
    fileList.innerHTML = '<div class="file-empty">\u5217\u76ee\u5f55\u5931\u8d25</div>';
    return;
  }
  const data = await response.json();
  const rootPath = fileRoot === "download" ? hostStatus.downloadRoot : hostStatus.uploadRoot;
  filePath.textContent = `${rootPath || ""}${data.path ? "/" + data.path : ""}`;
  fileList.innerHTML = "";
  if (!data.entries.length) {
    fileList.innerHTML = '<div class="file-empty">\u7a7a\u76ee\u5f55</div>';
    return;
  }
  for (const entry of data.entries) {
    const row = document.createElement("div");
    row.className = "file-row";
    const label = document.createElement("div");
    const name = document.createElement("div");
    name.className = "file-name";
    name.textContent = `${entry.directory ? "\u{1F4C1} " : "\u{1F4C4} "}${entry.name}`;
    const meta = document.createElement("div");
    meta.className = "file-meta";
    meta.textContent = entry.directory ? "\u76ee\u5f55" : formatBytes(entry.size);
    label.append(name, meta);
    row.append(label);
    if (entry.directory) {
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "\u6253\u5f00";
      open.addEventListener("click", () => {
        fileCurrentPath = data.path ? `${data.path}/${entry.name}` : entry.name;
        refreshFileList();
      });
      row.append(open);
    } else if (fileRoot === "download") {
      const link = document.createElement("button");
      link.type = "button";
      link.textContent = "\u4e0b\u8f7d";
      link.addEventListener("click", () => {
        const path = data.path ? `${data.path}/${entry.name}` : entry.name;
        window.location.href = `/api/files/download?path=${encodeURIComponent(path)}`;
      });
      row.append(link);
    } else {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "\u5220\u9664";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`\u5220\u9664 ${entry.name}\uff1f`)) return;
        const path = data.path ? `${data.path}/${entry.name}` : entry.name;
        const query = new URLSearchParams({ root: fileRoot, path });
        try {
          const response = await fetch(`/api/files/delete?${query}`, { method: "DELETE" });
          if (!response.ok) throw new Error(`delete rejected: ${response.status}`);
          await refreshFileList();
        } catch (_) {
          remove.textContent = "\u5220\u9664\u5931\u8d25";
        }
      });
      row.append(remove);
    }
    fileList.append(row);
  }
}

function transferElement(id, name, total) {
  let element = transfers.get(id);
  if (element?.node) return element;
  const node = document.createElement("div");
  node.className = "transfer-item";
  const head = document.createElement("div");
  head.className = "transfer-head";
  const label = document.createElement("span");
  label.className = "transfer-name";
  label.textContent = name;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "\u53d6\u6d88";
  const bar = document.createElement("div");
  bar.className = "transfer-bar";
  const fill = document.createElement("span");
  bar.append(fill);
  const detail = document.createElement("div");
  detail.className = "file-meta";
  detail.textContent = `0 / ${formatBytes(total)}`;
  head.append(label, cancel);
  node.append(head, bar, detail);
  transferList.prepend(node);
  element = { node, fill, detail, cancel, total, name, cancelled: false };
  cancel.addEventListener("click", async () => {
    element.cancelled = true;
    element.detail.textContent = "\u5df2\u53d6\u6d88";
    node.classList.add("error");
    if (element.transferId) {
      sendInput({ type: "transfer_cancel", transferId: element.transferId, removePartial: true }, true);
      await fetch(`/api/files/upload/${element.transferId}?remove=1`, { method: "DELETE" }).catch(() => {});
    }
  });
  transfers.set(id, element);
  return element;
}

function updateTransfer(element, received, total) {
  const ratio = total ? Math.min(1, received / total) : 0;
  element.fill.style.width = `${(ratio * 100).toFixed(2)}%`;
  element.detail.textContent = `${formatBytes(received)} / ${formatBytes(total)} (${Math.round(ratio * 100)}%)`;
}

function markTransferCancelled(transferId) {
  for (const element of transfers.values()) {
    if (element.transferId === transferId) {
      element.cancelled = true;
      element.node.classList.add("error");
      element.detail.textContent = "\u5df2\u53d6\u6d88";
    }
  }
}

async function sha256Hex(buffer) {
  if (!globalThis.crypto?.subtle) throw new Error("SHA-256 is unavailable in this browser");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

async function verifyResumePrefix(file, offset, blockSize, expectedHashes) {
  if (!offset) return true;
  const size = Math.max(1, Number(blockSize) || 1048576);
  const hashes = Array.isArray(expectedHashes) ? expectedHashes : [];
  const blocks = Math.ceil(offset / size);
  if (hashes.length !== blocks) return false;
  for (let index = 0; index < blocks; index += 1) {
    const start = index * size;
    const end = Math.min(offset, start + size);
    const buffer = await file.slice(start, end).arrayBuffer();
    if ((await sha256Hex(buffer)) !== hashes[index]) return false;
  }
  return true;
}

async function requestUploadStart(file, relative) {
  const response = await fetch("/api/files/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: file.name, size: file.size, relative, overwrite: false }),
  });
  if (response.status === 401) {
    showLogin("登录已过期，请重新输入密码");
    throw new Error("session expired");
  }
  if (!response.ok) throw new Error(`upload rejected: ${response.status}`);
  return response.json();
}

async function uploadFile(file, attempt = 0) {
  const key = `up-${++transferCounter}`;
  const element = transferElement(key, file.name, file.size);
  const chunkSize = Number(hostStatus?.fileChunkSize) || 1048576;
  if (Number(hostStatus?.fileMaxSize) && file.size > Number(hostStatus.fileMaxSize)) {
    element.node.classList.add("error");
    element.detail.textContent = "\u6587\u4ef6\u8d85\u8fc7\u4e3b\u673a\u9650\u5236";
    return;
  }
  const relative = fileRoot === "upload" && fileCurrentPath
    ? `${fileCurrentPath}/${file.name}`
    : file.name;
  let begin;
  try {
    begin = await requestUploadStart(file, relative);
  } catch (_) {
    element.node.classList.add("error");
    element.detail.textContent = "\u65e0\u6cd5\u5f00\u59cb\u4f20\u8f93";
    return;
  }
  element.transferId = begin.transferId;
  if (element.cancelled) return;
  let offset = Number(begin.offset) || 0;
  if (offset) {
    element.detail.textContent = "\u6b63\u5728\u6821\u9a8c\u65ad\u70b9\u2026";
    try {
      const valid = await verifyResumePrefix(
        file,
        offset,
        begin.resumeBlockSize,
        begin.resumeHashes,
      );
      if (!valid) {
        await fetch(`/api/files/upload/${begin.transferId}?remove=1`, { method: "DELETE" });
        begin = await requestUploadStart(file, relative);
        element.transferId = begin.transferId;
        offset = Number(begin.offset) || 0;
      }
    } catch (_) {
      element.node.classList.add("error");
      element.detail.textContent = "\u65ad\u70b9\u6821\u9a8c\u5931\u8d25";
      return;
    }
  }
  updateTransfer(element, offset, file.size);
  while (offset < file.size) {
    if (element.cancelled) return;
    const slice = file.slice(offset, Math.min(file.size, offset + chunkSize));
    let body;
    let chunkSha256;
    try {
      body = await slice.arrayBuffer();
      chunkSha256 = await sha256Hex(body);
    } catch (_) {
      element.node.classList.add("error");
      element.detail.textContent = "\u8bfb\u53d6\u6587\u4ef6\u5931\u8d25";
      return;
    }
    let result;
    try {
      const response = await fetch(`/api/files/upload/${begin.transferId}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Transfer-Offset": String(offset),
          "X-Chunk-SHA256": chunkSha256,
        },
        body,
      });
      if (response.status === 401) {
        showLogin("登录已过期，请重新输入密码");
        return;
      }
      if (!response.ok) throw new Error(`chunk rejected: ${response.status}`);
      result = await response.json();
    } catch (_) {
      if (attempt < 4) {
        // Resume: the host reports the authoritative offset it already stored.
        await new Promise((resolve) => window.setTimeout(resolve, 800 * (attempt + 1)));
        return uploadFileAt(file, element, begin.transferId, attempt + 1);
      }
      element.node.classList.add("error");
      element.detail.textContent = "\u4f20\u8f93\u4e2d\u65ad";
      return;
    }
    if (!result.ok) {
      offset = Number(result.expectedOffset) || 0;
      updateTransfer(element, offset, file.size);
      continue;
    }
    offset = Number(result.expectedOffset) || offset + body.byteLength;
    updateTransfer(element, offset, file.size);
  }
  try {
    const response = await fetch(`/api/files/upload/${begin.transferId}/finish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transferId: begin.transferId }),
    });
    if (!response.ok) throw new Error("finish failed");
    const result = await response.json();
    element.node.classList.add("done");
    element.detail.textContent = `\u5b8c\u6210 \u00b7 SHA-256 ${String(result.sha256 || "").slice(0, 12)}\u2026`;
    element.cancel.disabled = true;
  } catch (_) {
    element.node.classList.add("error");
    element.detail.textContent = "\u6821\u9a8c\u5931\u8d25";
  }
}

async function uploadFileAt(file, element, transferId, attempt) {
  const chunkSize = Number(hostStatus?.fileChunkSize) || 1048576;
  let offset = 0;
  try {
    const status = await fetch("/api/files/upload/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: file.name, size: file.size }),
    });
    if (status.ok) {
      const data = await status.json();
      if (data.known) offset = Number(data.received) || 0;
    }
  } catch (_) {}
  while (offset < file.size) {
    if (element.cancelled) return;
    const slice = file.slice(offset, Math.min(file.size, offset + chunkSize));
    let result;
    try {
      const body = await slice.arrayBuffer();
      const chunkSha256 = await sha256Hex(body);
      const response = await fetch(`/api/files/upload/${transferId}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Transfer-Offset": String(offset),
          "X-Chunk-SHA256": chunkSha256,
        },
        body,
      });
      if (!response.ok) throw new Error("chunk rejected");
      result = await response.json();
    } catch (_) {
      if (attempt < 4) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000 * (attempt + 1)));
        return uploadFileAt(file, element, transferId, attempt + 1);
      }
      element.node.classList.add("error");
      element.detail.textContent = "\u4f20\u8f93\u4e2d\u65ad";
      return;
    }
    offset = Number(result.expectedOffset) || offset + chunkSize;
    updateTransfer(element, offset, file.size);
  }
  try {
    const response = await fetch(`/api/files/upload/${transferId}/finish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transferId }),
    });
    if (!response.ok) throw new Error("finish failed");
    const result = await response.json();
    element.node.classList.add("done");
    element.detail.textContent = `\u5b8c\u6210 \u00b7 SHA-256 ${String(result.sha256 || "").slice(0, 12)}\u2026`;
    element.cancel.disabled = true;
  } catch (_) {
    element.node.classList.add("error");
    element.detail.textContent = "\u6821\u9a8c\u5931\u8d25";
  }
}

// ---------------------------------------------------------------- metrics
let metricsTimerRtc = null;

function startMetrics() {
  if (metricsTimer) return;
  metrics.lastBytesAt = performance.now();
  metrics.lastBytes = 0;
  metrics.rtcBytes = 0;
  metrics.rtcAt = performance.now();
  metricsTimer = window.setInterval(() => {
    if (document.hidden) return;
    hideStaleCursor();
    const now = performance.now();
    const elapsed = Math.max(0.25, (now - metrics.lastBytesAt) / 1000);
    const delta = metrics.bytes - metrics.lastBytes;
    metrics.lastBytes = metrics.bytes;
    metrics.lastBytesAt = now;
    if (metrics.mode === "websocket") {
      metrics.bitrate = Math.round((delta * 8) / elapsed);
      if (!metrics.fps && delta) metrics.fps = Math.round(delta / Math.max(1, 40000));
    }
    renderMetrics();
    sendInput(
      {
        type: "stats",
        clientFps: metrics.fps,
        clientRtt: metrics.latency,
        clientLoss: metrics.loss,
        dropped: droppedFrames,
        mode: metrics.mode,
      },
      false,
    );
    sendInput({ type: "ping", at: Date.now() }, false);
  }, 1500);
  metricsTimerRtc = window.setInterval(collectRtcStats, 1000);
}

function stopMetrics() {
  if (metricsTimer) {
    window.clearInterval(metricsTimer);
    metricsTimer = null;
  }
  if (metricsTimerRtc) {
    window.clearInterval(metricsTimerRtc);
    metricsTimerRtc = null;
  }
}

function handleServerMetrics(msg) {
  if (msg.mode === "websocket" && typeof msg.fps === "number") {
    if (typeof msg.serverFpsTarget === "number") metrics.targetFps = msg.serverFpsTarget;
    if (typeof msg.bitrate === "number") metrics.bitrate = msg.bitrate;
    if (typeof msg.width === "number") metrics.resolution = `${msg.width}p`;
  }
  const client = msg.client;
  if (client && typeof client.fps === "number" && client.mode) {
    metrics.mode = client.mode;
  }
  renderMetrics();
}

function renderMetrics() {
  metricFps.textContent = metrics.fps ? String(Math.round(metrics.fps)) : "-";
  metricLatency.textContent = metrics.latency ? `${Math.round(metrics.latency)} ms` : "-";
  metricLoss.textContent = `${(metrics.loss * 100).toFixed(1)} %`;
  metricBitrate.textContent = metrics.bitrate ? `${Math.round(metrics.bitrate / 1000)} kbps` : "-";
  metricMode.textContent = metrics.mode === "webrtc" ? "WebRTC" : "WebSocket";
  metricResolution.textContent = metrics.resolution || "-";
  metricLatency.className = metrics.latency > 400 ? "bad" : metrics.latency > 180 ? "warn" : "";
  metricLoss.className = metrics.loss > 0.08 ? "bad" : metrics.loss > 0.02 ? "warn" : "";
  metricFps.className = metrics.fps && metrics.fps < 8 ? "warn" : "";
}

async function collectRtcStats() {
  const connection = peerConnection;
  if (!connection || !rtcConnected) return;
  let report;
  try {
    report = await connection.getStats();
  } catch (_) {
    return;
  }
  let bitrate = 0;
  let loss = 0;
  let rtt = 0;
  let fps = 0;
  let width = 0;
  let height = 0;
  report.forEach((item) => {
    if (item.type === "inbound-rtp" && item.kind === "video") {
      if (typeof item.framesPerSecond === "number") fps = item.framesPerSecond;
      if (item.frameWidth) width = item.frameWidth;
      if (item.frameHeight) height = item.frameHeight;
      const received = Number(item.bytesReceived || 0);
      const now = performance.now();
      const elapsed = Math.max(0.2, (now - (metrics.rtcAt || now)) / 1000);
      const delta = received - (metrics.rtcBytes || 0);
      metrics.rtcBytes = received;
      metrics.rtcAt = now;
      if (delta > 0) bitrate = (delta * 8) / elapsed;
      const lost = Number(item.packetsLost || 0);
      const total = lost + Number(item.packetsReceived || 0);
      if (total > 0) loss = Math.max(0, lost / total);
    }
    if (item.type === "candidate-pair" && item.state === "succeeded") {
      if (typeof item.currentRoundTripTime === "number") rtt = item.currentRoundTripTime * 1000;
    }
  });
  if (fps) metrics.fps = Math.round(fps);
  if (bitrate) metrics.bitrate = Math.round(bitrate);
  metrics.loss = loss;
  if (rtt) metrics.latency = Math.round(rtt);
  if (width && height) metrics.resolution = `${width}\u00d7${height}`;
  renderMetrics();
}

// ---------------------------------------------------------------- finalize
window.addEventListener("resize", () => {
  layoutStage();
  sendClientInfo(rtcControl && rtcControl.readyState === "open" ? rtcControl : ws);
});

checkStatus().catch(() => {
  showLogin("无法连接到电脑服务");
});
