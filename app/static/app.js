const loginPanel = document.getElementById("loginPanel");
const remotePanel = document.getElementById("remotePanel");
const loginForm = document.getElementById("loginForm");
const passwordInput = document.getElementById("password");
const loginMessage = document.getElementById("loginMessage");
const viewer = document.getElementById("viewer");
const screen = document.getElementById("screen");
const emptyState = document.getElementById("emptyState");
const connectionState = document.getElementById("connectionState");
const statusDot = document.getElementById("statusDot");
const audioState = document.getElementById("audioState");
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
const binaryDecoder = new TextDecoder();

let ws = null;
let inputWs = null;
let audioWs = null;
let reconnectTimer = null;
let inputReconnectTimer = null;
let audioReconnectTimer = null;
let shouldReconnect = true;
let lastMoveAt = 0;
let audioContext = null;
let nextAudioTime = 0;
let zoom = 1;
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
const KEYBOARD_SENTINEL = "\u200b";
const activeTouches = new Map();
const FRAME_STALL_TIMEOUT_MS = 15000;
let frameWatchdogTimer = null;
let lastMainMessageAt = 0;
let frameLoadTimer = null;

async function checkStatus() {
  const res = await fetch("/api/status");
  const status = await res.json();
  if (status.authenticated) {
    showRemote();
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginMessage.textContent = "";
  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: passwordInput.value }),
  });
  if (!res.ok) {
    loginMessage.textContent = "\u5bc6\u7801\u9519\u8bef";
    return;
  }
  showRemote();
});

document.getElementById("logout").addEventListener("click", async () => {
  shouldReconnect = false;
  if (reconnectTimer) window.clearTimeout(reconnectTimer);
  if (inputReconnectTimer) window.clearTimeout(inputReconnectTimer);
  if (audioReconnectTimer) window.clearTimeout(audioReconnectTimer);
  setMouseDragLocked(false);
  stopFrameWatchdog();
  cancelActiveTouches();
  if (ws) ws.close();
  closeInputWebSocket();
  closeAudioWebSocket();
  await fetch("/api/logout", { method: "POST" });
  loginPanel.classList.remove("hidden");
  remotePanel.classList.add("hidden");
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
  sendInput({ kind: "mouse_button", button, action });
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
    sendInput({ kind: "mouse_delta", dx: x, dy: y });
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
  sendInput({ kind: "wheel", delta: -5 });
});

mouseWheelDown.addEventListener("click", () => {
  sendInput({ kind: "wheel", delta: 5 });
});

mouseDragLock.addEventListener("click", () => {
  setMouseDragLocked(!mouseDragLocked);
});

window.addEventListener("beforeunload", () => {
  if (mouseDragLocked) {
    sendMouseButton(0, "up");
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
  sendInput({ kind: "wheel", delta: steps });
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

function showRemote() {
  shouldReconnect = true;
  loginPanel.classList.add("hidden");
  remotePanel.classList.remove("hidden");
  startFrameWatchdog();
  viewer.focus();
  connectWebSocket();
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
    if (
      ws &&
      ws.readyState === WebSocket.OPEN &&
      lastMainMessageAt > 0 &&
      performance.now() - lastMainMessageAt > FRAME_STALL_TIMEOUT_MS
    ) {
      setConnectionState("画面重连中", "closed");
      ws.close();
    }
  }, 3000);
}

function stopFrameWatchdog() {
  if (!frameWatchdogTimer) return;
  window.clearInterval(frameWatchdogTimer);
  frameWatchdogTimer = null;
}

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  setConnectionState("\u8fde\u63a5\u4e2d", "connecting");
  ws.addEventListener("open", () => {
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    lastMainMessageAt = performance.now();
    setConnectionState("\u5df2\u8fde\u63a5", "connected");
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const viewportWidth = Math.round(Math.max(viewer.clientWidth, screen.clientWidth) * dpr);
    socket.send(JSON.stringify({ type: "client_info", viewportWidth }));
    ensureAudio();
    connectInputWebSocket();
    connectAudioWebSocket();
  });
  ws.addEventListener("close", () => {
    setConnectionState("\u5df2\u65ad\u5f00", "closed");
    closeInputWebSocket();
    closeAudioWebSocket();
    if (!shouldReconnect || remotePanel.classList.contains("hidden")) return;
    reconnectTimer = window.setTimeout(connectWebSocket, 1200);
  });
  ws.addEventListener("message", (event) => {
    lastMainMessageAt = performance.now();
    handleSocketMessage(event, handleMainJsonMessage, handleMainBinaryMessage).catch(() => {
      if (ws === socket && socket.readyState === WebSocket.OPEN) {
        socket.close();
      }
    });
  });
}

function connectInputWebSocket() {
  if (inputWs && (inputWs.readyState === WebSocket.OPEN || inputWs.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws/input`);
  inputWs = socket;
  socket.addEventListener("open", () => {
    if (inputReconnectTimer) {
      window.clearTimeout(inputReconnectTimer);
      inputReconnectTimer = null;
    }
  });
  socket.addEventListener("close", () => {
    if (inputWs !== socket) return;
    inputWs = null;
    if (!shouldReconnect || remotePanel.classList.contains("hidden")) return;
    inputReconnectTimer = window.setTimeout(() => {
      inputReconnectTimer = null;
      connectInputWebSocket();
    }, 700);
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

function markScreenReady() {
  screen.classList.add("ready");
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
  if (frameRendering) {
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

function showFrameBase64(jpeg) {
  clearScreenObjectUrl();
  screen.src = `data:image/jpeg;base64,${jpeg}`;
  markScreenReady();
}

function handleMainJsonMessage(msg) {
  if (msg.type === "frame") {
    showFrameBase64(msg.jpeg);
  } else if (msg.type === "clipboard") {
    // Clipboard is kept as a background channel; no foreground panel is shown.
  } else if (msg.type === "text_focus") {
    remoteTextFocusActive = Boolean(msg.active);
    if (remoteTextFocusActive) {
      focusKeyboardSink();
    } else if (document.activeElement === keyboardSink) {
      keyboardSink.blur();
    }
  } else if (msg.type === "audio_status" || msg.type === "audio") {
    handleAudioMessage(msg);
  } else if (msg.type === "error") {
    setConnectionState(msg.message, "closed");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
    }
  }
}

function handleMainBinaryMessage(meta, payload) {
  if (meta.type === "frame") {
    showFrameBlob(payload, meta.format, meta.signature || null);
  }
}

function connectAudioWebSocket() {
  if (audioWs && (audioWs.readyState === WebSocket.OPEN || audioWs.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws/audio`);
  socket.binaryType = "arraybuffer";
  audioWs = socket;
  socket.addEventListener("open", () => {
    if (audioReconnectTimer) {
      window.clearTimeout(audioReconnectTimer);
      audioReconnectTimer = null;
    }
    ensureAudio();
  });
  socket.addEventListener("close", () => {
    if (audioWs !== socket) return;
    audioWs = null;
    if (!shouldReconnect || remotePanel.classList.contains("hidden")) return;
    audioState.textContent = "\u58f0\u97f3\u91cd\u8fde\u4e2d";
    audioReconnectTimer = window.setTimeout(() => {
      audioReconnectTimer = null;
      connectAudioWebSocket();
    }, 1200);
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
      ? "\u58f0\u97f3\u5df2\u5f00\u542f"
      : `\u58f0\u97f3\u4e0d\u53ef\u7528 ${msg.message || ""}`;
  } else if (msg.type === "audio") {
    playPcm(msg);
  }
}

function handleAudioBinaryMessage(meta, payload) {
  if (meta.type !== "audio") return;
  const bytes = new Uint8Array(payload);
  const samples = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
  playPcmSamples(samples, meta.sampleRate, meta.channels || 2);
}

function ensureAudio() {
  if (!audioContext) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) {
      audioContext = new Ctx();
      nextAudioTime = audioContext.currentTime + 0.12;
    }
  }
  if (audioContext && audioContext.state === "suspended") {
    audioContext.resume();
  }
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
  playPcmSamples(samples, msg.sampleRate, msg.channels || 2);
}

function playPcmSamples(samples, sampleRate, channels = 2) {
  ensureAudio();
  if (!audioContext) return;
  const frames = Math.floor(samples.length / channels);
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
  const startAt = Math.max(audioContext.currentTime + 0.04, nextAudioTime);
  src.start(startAt);
  nextAudioTime = startAt + buffer.duration;
  if (nextAudioTime - audioContext.currentTime > 1.2) {
    nextAudioTime = audioContext.currentTime + 0.2;
  }
}

function setZoom(next) {
  zoom = Math.max(0.7, Math.min(2.4, next));
  screen.style.transform = `scale(${zoom})`;
  zoomReset.textContent = `${Math.round(zoom * 100)}%`;
}

function normalizedPoint(event) {
  const rect = screen.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) return null;
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
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

function sendInput(payload) {
  const message = JSON.stringify({ type: "input", ...payload });
  if (inputWs && inputWs.readyState === WebSocket.OPEN) {
    inputWs.send(message);
    return;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(message);
  }
}

function sendText(text) {
  if (!text) return;
  sendInput({ kind: "text", text });
}

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
    sendInput({ kind: "key", key: "Enter" });
    resetKeyboardSink();
    return;
  }
  if (event.inputType === "deleteContentBackward") {
    event.preventDefault();
    sendInput({ kind: "key", key: "Backspace" });
    resetKeyboardSink();
    return;
  }
  if (event.inputType === "deleteContentForward") {
    event.preventDefault();
    sendInput({ kind: "key", key: "Delete" });
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
    sendInput({ kind: "touch", contacts });
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

viewer.addEventListener("contextmenu", (event) => event.preventDefault());

viewer.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  if (event.pointerType === "touch") {
    handleTouchDown(event);
    return;
  }
  ensureAudio();
  viewer.focus();
  const sample = pointerSample(event);
  const p = normalizedPoint(sample);
  if (p) sendInput({ kind: "down", ...p, button: sample.button });
});

viewer.addEventListener("pointerup", (event) => {
  event.preventDefault();
  if (event.pointerType === "touch") {
    handleTouchUp(event);
    return;
  }
  const sample = pointerSample(event);
  const p = normalizedPoint(sample);
  if (p) sendInput({ kind: "up", ...p, button: sample.button });
});

viewer.addEventListener("pointercancel", (event) => {
  if (event.pointerType === "touch" && activeTouches.has(event.pointerId)) {
    handleTouchUp(event, "cancel");
  }
});

viewer.addEventListener("pointermove", (event) => {
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
  if (p) sendInput({ kind: "move", ...p, button: sample.button });
});

viewer.addEventListener("dblclick", (event) => {
  if (event.pointerType === "touch") return;
  const p = normalizedPoint(pointerSample(event));
  if (p) sendInput({ kind: "double", ...p, button: event.button });
});

viewer.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const p = normalizedPoint(event);
    sendInput({
      kind: "wheel",
      ...(p || {}),
      delta: Math.sign(event.deltaY) * 4,
      deltaX: Math.sign(event.deltaX) * 4,
    });
  },
  { passive: false },
);

document.addEventListener("keydown", (event) => {
  if (remotePanel.classList.contains("hidden")) return;
  const active = document.activeElement;
  if (active === keyboardSink || active === passwordInput) return;
  event.preventDefault();
  sendInput({
    kind: "key",
    key: event.key,
    ctrl: event.ctrlKey,
    alt: event.altKey,
    shift: event.shiftKey,
  });
});

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

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    cancelActiveTouches();
    return;
  }
  lastMainMessageAt = performance.now();
  if (!remotePanel.classList.contains("hidden")) {
    connectWebSocket();
  }
});

window.addEventListener("pagehide", cancelActiveTouches);

checkStatus().catch(() => {});
