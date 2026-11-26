import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";

interface PollState {
  running: boolean;
  docker_online: boolean;
  container: string;
  state: string;
  novnc_url: string;
  backup_exists: boolean;
  backup_mb: number;
  geometry?: string;
  image_mime?: string;
  image_b64?: string;
  logs?: string[];
  error?: string;
}

type ViewMode = "stream" | "png" | "vnc";

const app = new App({ name: "Whale Desktop", version: "1.0.0" }, {});

let currentState: PollState | null = null;
let autoRefresh = true;
let inFlight = false;
let streamInFlight = false;
let timerId: number | null = null;
let screenW = 1280;
let screenH = 720;
let zoomSetting = "fit";
let viewMode: ViewMode = "stream";
let activeVncSrc = "";
let streamFailStreak = 0;
let lastMetaPollTs = 0;
let fpsWindowStart = performance.now();
let fpsFrameCount = 0;
let measuredFps = 0;
let wsConn: WebSocket | null = null;
let wsConnected = false;
let wsReconnectAfter = 0;
let wsPendingBlob: Blob | null = null;
let wsDecoding = false;

const STREAM_BASE = "http://127.0.0.1:16080";
const STREAM_WS_URL = "ws://127.0.0.1:16080/ws-stream?q=93&s=444";

const elStatusBadge = document.getElementById("statusBadge") as HTMLElement;
const elMetaInfo = document.getElementById("metaInfo") as HTMLElement;
const elScreenCanvas = document.getElementById("screenCanvas") as HTMLCanvasElement;
const elScreenCtx = elScreenCanvas.getContext("2d");
const elScreenImg = document.getElementById("screenImg") as HTMLImageElement;
const elVncShell = document.getElementById("vncShell") as HTMLElement;
const elVncFrame = document.getElementById("vncFrame") as HTMLIFrameElement;
const elPlaceholder = document.getElementById("placeholder") as HTMLElement;
const elCoords = document.getElementById("coords") as HTMLElement;
const elLogs = document.getElementById("logs") as HTMLElement;
const elTypeInput = document.getElementById("typeInput") as HTMLInputElement;
const elResSelect = document.getElementById("resSelect") as HTMLSelectElement;
const elZoomSelect = document.getElementById("zoomSelect") as HTMLSelectElement;
const btnStart = document.getElementById("btnStart") as HTMLButtonElement;
const btnStop = document.getElementById("btnStop") as HTMLButtonElement;
const btnMode = document.getElementById("btnMode") as HTMLButtonElement;
const btnCmd = document.getElementById("btnCmd") as HTMLButtonElement;
const btnNoVnc = document.getElementById("btnNoVnc") as HTMLButtonElement;
const btnBackup = document.getElementById("btnBackup") as HTMLButtonElement;
const btnLive = document.getElementById("btnLive") as HTMLButtonElement;
const btnRefresh = document.getElementById("btnRefresh") as HTMLButtonElement;
const btnSendText = document.getElementById("btnSendText") as HTMLButtonElement;
const elStage = document.querySelector(".stage") as HTMLElement;

function updateGeometry(geomStr?: string): void {
  if (!geomStr) return;
  const parts = geomStr.split(/[x\s]+/).map((n) => parseInt(n, 10));
  if (parts[0] > 0 && parts[1] > 0 && (parts[0] !== screenW || parts[1] !== screenH)) {
    screenW = parts[0];
    screenH = parts[1];
    const geomKey = `${screenW}x${screenH}`;
    const hasOption = Array.from(elResSelect.options).some((opt) => opt.value === geomKey);
    if (!hasOption) {
      const customOpt = document.createElement("option");
      customOpt.value = geomKey;
      customOpt.textContent = `${screenW}×${screenH}`;
      elResSelect.appendChild(customOpt);
    }
    elResSelect.value = geomKey;
    applyScaleStyles();
  }
}

function updateMetaBar(mimeTag: string): void {
  const backupTxt = currentState?.backup_exists ? `Backup: ${currentState.backup_mb} MB` : "No backup yet";
  const zoomLabel = zoomSetting === "fit" ? "Fit" : `${Math.round(parseFloat(zoomSetting) * 100)}%`;
  const fpsTxt = measuredFps > 0 ? ` • ${measuredFps} FPS` : "";
  elMetaInfo.textContent = `${screenW}×${screenH} @ ${zoomLabel} (${mimeTag}${fpsTxt})  |  ${backupTxt}`;
}

function recordFrameTick(): void {
  fpsFrameCount += 1;
  const now = performance.now();
  const elapsed = now - fpsWindowStart;
  if (elapsed >= 1000) {
    measuredFps = Math.round((fpsFrameCount * 1000) / elapsed);
    fpsFrameCount = 0;
    fpsWindowStart = now;
  }
}

let hasDrawnCanvasFrame = false;

function applyScaleStyles(): void {
  const availW = Math.max(200, (elStage?.clientWidth || 720) - 10);
  const availH = Math.max(120, (elStage?.clientHeight || 420) - 10);
  const fitFactor = Math.min(availW / screenW, availH / screenH);

  let factor = fitFactor;
  if (zoomSetting !== "fit") {
    const requested = parseFloat(zoomSetting) || 0.5;
    factor = requested < 1 ? Math.min(requested, fitFactor) : requested;
  }

  const boxW = Math.max(160, Math.round(screenW * factor));
  const boxH = Math.max(100, Math.round(screenH * factor));

  elStage.style.overflow = zoomSetting === "fit" ? "hidden" : "auto";
  elScreenCanvas.style.width = `${boxW}px`;
  elScreenCanvas.style.height = `${boxH}px`;
  elScreenImg.style.width = `${boxW}px`;
  elScreenImg.style.height = `${boxH}px`;
  const nearNative = Math.abs(boxW - screenW) <= 4 && Math.abs(boxH - screenH) <= 4;
  elScreenCanvas.style.imageRendering = nearNative ? "pixelated" : "auto";

  if (viewMode === "vnc") {
    elVncShell.style.width = `${boxW}px`;
    elVncShell.style.height = `${boxH}px`;
    elVncFrame.style.width = `${screenW}px`;
    elVncFrame.style.height = `${screenH}px`;
    elVncFrame.style.transform = `scale(${factor})`;
  }
}

if (typeof ResizeObserver !== "undefined" && elStage) {
  new ResizeObserver(() => applyScaleStyles()).observe(elStage);
}

function syncStageVisibility(): void {
  const running = Boolean(currentState?.running);
  if (!running) {
    closeStreamSocket();
    elScreenCanvas.style.display = "none";
    elScreenImg.style.display = "none";
    elVncShell.style.display = "none";
    if (activeVncSrc) {
      elVncFrame.src = "about:blank";
      activeVncSrc = "";
    }
    elPlaceholder.style.display = "flex";
    return;
  }

  if (viewMode === "vnc") {
    closeStreamSocket();
    const baseUrl = currentState?.novnc_url || `${STREAM_BASE}/vnc_lite.html?autoconnect=1&resize=off`;
    const desired = baseUrl.replace("vnc.html", "vnc_lite.html").replace(/resize=\w+/g, "resize=off");
    if (activeVncSrc !== desired) {
      activeVncSrc = desired;
      elVncFrame.src = desired;
    }
    elScreenCanvas.style.display = "none";
    elScreenImg.style.display = "none";
    elPlaceholder.style.display = "none";
    elVncShell.style.display = "block";
    applyScaleStyles();
    return;
  }

  if (viewMode !== "stream") closeStreamSocket();
  if (activeVncSrc) {
    elVncFrame.src = "about:blank";
    activeVncSrc = "";
  }
  elVncShell.style.display = "none";
  if (hasDrawnCanvasFrame) {
    elScreenCanvas.style.display = "block";
    elScreenImg.style.display = "none";
    elPlaceholder.style.display = "none";
    applyScaleStyles();
  } else if (elScreenImg.src) {
    elScreenCanvas.style.display = "none";
    elScreenImg.style.display = "block";
    elPlaceholder.style.display = "none";
    applyScaleStyles();
  } else {
    elScreenCanvas.style.display = "none";
    elScreenImg.style.display = "none";
    elPlaceholder.style.display = "flex";
  }
}

async function drawBlobToCanvas(blob: Blob): Promise<void> {
  if (!elScreenCtx) return;
  const bitmap = await createImageBitmap(blob);
  const sizeChanged = elScreenCanvas.width !== bitmap.width || elScreenCanvas.height !== bitmap.height;
  if (sizeChanged) {
    elScreenCanvas.width = bitmap.width;
    elScreenCanvas.height = bitmap.height;
    screenW = bitmap.width;
    screenH = bitmap.height;
    applyScaleStyles();
  }
  elScreenCtx.drawImage(bitmap, 0, 0);
  bitmap.close();
  hasDrawnCanvasFrame = true;
}

function applyState(state: PollState | undefined): void {
  if (!state) return;
  currentState = state;

  elStatusBadge.textContent = state.running ? "RUNNING" : state.state.toUpperCase();
  elStatusBadge.className = "badge " + (state.running ? "ok" : "off");
  updateGeometry(state.geometry);

  if (state.image_b64 && (viewMode !== "stream" || streamFailStreak > 0 || !hasDrawnCanvasFrame)) {
    const mime = state.image_mime || "image/png";
    const img = new Image();
    img.onload = () => {
      if (elScreenCtx) {
        if (elScreenCanvas.width !== img.naturalWidth || elScreenCanvas.height !== img.naturalHeight) {
          elScreenCanvas.width = img.naturalWidth;
          elScreenCanvas.height = img.naturalHeight;
        }
        elScreenCtx.drawImage(img, 0, 0);
        hasDrawnCanvasFrame = true;
        syncStageVisibility();
      }
    };
    img.src = `data:${mime};base64,${state.image_b64}`;
    elScreenImg.src = img.src;
    recordFrameTick();
    updateMetaBar(mime === "image/jpeg" ? "JPEG-444" : "PNG");
  } else if (!state.running) {
    hasDrawnCanvasFrame = false;
    elPlaceholder.textContent = state.error
      ? state.error
      : "Whale Desktop is stopped. Click Start to boot container.";
    updateMetaBar("OFFLINE");
  } else {
    updateMetaBar(viewMode === "stream" ? (wsConnected ? "WS-444" : "STREAM") : viewMode === "vnc" ? "VNC" : "PNG");
  }

  syncStageVisibility();

  btnStart.disabled = state.running;
  btnStop.disabled = !state.running;
  elResSelect.disabled = !state.running;

  if (Array.isArray(state.logs) && state.logs.length > 0) {
    elLogs.textContent = state.logs.slice(-6).join("\n");
  } else {
    elLogs.textContent = "No activity yet.";
  }
}

async function callAppTool(name: string, args: Record<string, unknown> = {}): Promise<PollState | undefined> {
  if (inFlight && name === "app_poll_screen") return undefined;
  inFlight = true;
  try {
    const res = await app.callServerTool({ name, arguments: args });
    const sc = res?.structuredContent as PollState | undefined;
    if (sc) applyState(sc);
    return sc;
  } catch (err) {
    elMetaInfo.textContent = `Error: ${err instanceof Error ? err.message : String(err)}`;
    return undefined;
  } finally {
    inFlight = false;
  }
}

function closeStreamSocket(): void {
  wsConnected = false;
  wsPendingBlob = null;
  if (wsConn) {
    try {
      wsConn.close();
    } catch {
      // Ignore socket close error
    }
    wsConn = null;
  }
}

async function drainWsFrames(): Promise<void> {
  if (wsDecoding) return;
  wsDecoding = true;
  try {
    while (wsPendingBlob && viewMode === "stream" && autoRefresh && !document.hidden) {
      const nextBlob = wsPendingBlob;
      wsPendingBlob = null;
      await drawBlobToCanvas(nextBlob);
      streamFailStreak = 0;
      recordFrameTick();
      updateMetaBar("WS-STREAM");
      syncStageVisibility();
    }
  } catch {
    // Next frame overwrites cleanly
  } finally {
    wsDecoding = false;
  }
}

function ensureStreamSocket(): boolean {
  if (wsConn && (wsConn.readyState === WebSocket.OPEN || wsConn.readyState === WebSocket.CONNECTING)) {
    return wsConnected;
  }
  const now = performance.now();
  if (now < wsReconnectAfter) return false;
  try {
    const sock = new WebSocket(STREAM_WS_URL);
    sock.binaryType = "blob";
    wsConn = sock;
    sock.onopen = () => {
      wsConnected = true;
      streamFailStreak = 0;
    };
    sock.onmessage = (ev) => {
      if (!(ev.data instanceof Blob) || viewMode !== "stream" || !autoRefresh || document.hidden) return;
      wsPendingBlob = ev.data;
      void drainWsFrames();
    };
    sock.onerror = () => {
      wsConnected = false;
    };
    sock.onclose = () => {
      if (wsConn === sock) wsConn = null;
      wsConnected = false;
      wsReconnectAfter = performance.now() + 1500;
    };
  } catch {
    wsReconnectAfter = now + 2000;
  }
  return wsConnected;
}

async function fetchStreamFrame(): Promise<boolean> {
  if (streamInFlight || !currentState?.running) return false;
  streamInFlight = true;
  try {
    const resp = await fetch(`${STREAM_BASE}/frame.jpg?q=93&s=444&t=${Date.now()}`, {
      cache: "no-store",
      mode: "cors",
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const geom = resp.headers.get("X-Whale-Geometry") || undefined;
    const blob = await resp.blob();
    await drawBlobToCanvas(blob);

    streamFailStreak = 0;
    updateGeometry(geom);
    recordFrameTick();
    updateMetaBar("STREAM");
    syncStageVisibility();
    return true;
  } catch {
    streamFailStreak += 1;
    return false;
  } finally {
    streamInFlight = false;
  }
}

function scheduleNextPoll(delayMs = 34): void {
  if (timerId !== null) window.clearTimeout(timerId);
  timerId = window.setTimeout(async () => {
    let nextDelay = 1000;
    if (autoRefresh && !document.hidden) {
      if (viewMode === "stream" && currentState?.running) {
        const now = performance.now();
        const wsLive = ensureStreamSocket();
        if (wsLive) {
          if (now - lastMetaPollTs > 2500 && !inFlight) {
            lastMetaPollTs = now;
            void callAppTool("app_poll_screen", {});
          }
          nextDelay = 250;
        } else {
          const ok = await fetchStreamFrame();
          if (!ok && streamFailStreak >= 3 && !inFlight) {
            await callAppTool("app_poll_screen", {});
            nextDelay = 120;
          } else {
            if (now - lastMetaPollTs > 2500 && !inFlight) {
              lastMetaPollTs = now;
              void callAppTool("app_poll_screen", {});
            }
            nextDelay = ok ? 16 : 200;
          }
        }
      } else if (viewMode === "png" && !inFlight) {
        await callAppTool("app_poll_screen", {});
        nextDelay = 60;
      } else if (viewMode === "vnc" && performance.now() - lastMetaPollTs > 2500 && !inFlight) {
        lastMetaPollTs = performance.now();
        await callAppTool("app_poll_screen", {});
        nextDelay = 1200;
      }
    }
    scheduleNextPoll(nextDelay);
  }, delayMs);
}

app.ontoolinput = () => {
  elStatusBadge.textContent = "LOADING";
};

app.ontoolresult = (result) => {
  applyState(result?.structuredContent as PollState | undefined);
};

app.onhostcontextchanged = (ctx) => {
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
};

btnStart.addEventListener("click", () => {
  elStatusBadge.textContent = "STARTING";
  void callAppTool("app_control", { op: "start" });
});

btnStop.addEventListener("click", () => {
  void callAppTool("app_control", { op: "stop" });
});

elResSelect.addEventListener("change", () => {
  const parts = elResSelect.value.split("x").map((n) => parseInt(n, 10));
  if (parts.length === 2 && parts[0] > 0 && parts[1] > 0) {
    elMetaInfo.textContent = `Resizing to ${parts[0]}x${parts[1]}...`;
    void callAppTool("app_resize", { width: parts[0], height: parts[1] });
  }
});

elZoomSelect.addEventListener("change", () => {
  zoomSetting = elZoomSelect.value;
  syncStageVisibility();
  updateMetaBar(viewMode === "stream" ? (wsConnected ? "WS-444" : "STREAM") : viewMode === "vnc" ? "VNC" : "PNG");
});

btnMode.addEventListener("click", () => {
  viewMode = viewMode === "stream" ? "png" : viewMode === "png" ? "vnc" : "stream";
  btnMode.textContent =
    viewMode === "stream" ? "View: Stream" : viewMode === "png" ? "View: PNG" : "View: Live VNC";
  btnMode.classList.toggle("active", viewMode !== "png");
  measuredFps = 0;
  fpsFrameCount = 0;
  fpsWindowStart = performance.now();
  syncStageVisibility();
  updateMetaBar(viewMode === "stream" ? "WS-444" : viewMode === "vnc" ? "VNC" : "PNG");
});

btnCmd.addEventListener("click", () => {
  void callAppTool("app_control", { op: "console" });
});

btnBackup.addEventListener("click", () => {
  elMetaInfo.textContent = "Saving Docker image backup (tar.gz)...";
  void callAppTool("app_control", { op: "save_image" });
});

btnNoVnc.addEventListener("click", () => {
  void callAppTool("app_control", { op: "open_webview" });
});

btnRefresh.addEventListener("click", () => {
  if (viewMode === "stream") {
    if (!ensureStreamSocket()) void fetchStreamFrame();
  }
  void callAppTool("app_poll_screen", {});
});

btnLive.addEventListener("click", () => {
  autoRefresh = !autoRefresh;
  btnLive.textContent = autoRefresh ? "Live: ON" : "Live: OFF";
  btnLive.classList.toggle("active", autoRefresh);
  if (!autoRefresh) closeStreamSocket();
  else if (viewMode === "stream" && currentState?.running) ensureStreamSocket();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    closeStreamSocket();
  } else if (autoRefresh) {
    if (viewMode === "stream" && currentState?.running) {
      if (!ensureStreamSocket()) void fetchStreamFrame();
    } else if (viewMode === "png" && !inFlight) {
      void callAppTool("app_poll_screen", {});
    }
  }
});

function attachPointerHandlers(targetEl: HTMLElement): void {
  targetEl.addEventListener("mousemove", (ev) => {
    const rect = targetEl.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const x = Math.round(((ev.clientX - rect.left) / rect.width) * screenW);
    const y = Math.round(((ev.clientY - rect.top) / rect.height) * screenH);
    elCoords.textContent = `(${x}, ${y})`;
  });

  targetEl.addEventListener("click", (ev) => {
    if (!currentState?.running) return;
    const rect = targetEl.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const x = Math.max(0, Math.min(screenW - 1, Math.round(((ev.clientX - rect.left) / rect.width) * screenW)));
    const y = Math.max(0, Math.min(screenH - 1, Math.round(((ev.clientY - rect.top) / rect.height) * screenH)));
    void callAppTool("app_click", { x, y, button: "left", clicks: 1 });
  });
}

attachPointerHandlers(elScreenCanvas);
attachPointerHandlers(elScreenImg);

function sendText(): void {
  const txt = elTypeInput.value;
  if (!txt || !currentState?.running) return;
  elTypeInput.value = "";
  void callAppTool("app_type", { text: txt, enter: true });
}

btnSendText.addEventListener("click", sendText);
elTypeInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    sendText();
  }
});

app.connect().then(() => {
  scheduleNextPoll(120);
});
