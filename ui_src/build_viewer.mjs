import fs from "node:fs";
import path from "node:path";
import { buildSync } from "esbuild";

const root = path.resolve(import.meta.dirname, "..");
const outHtml = path.join(root, "bin", "viewer.html");

const bundled = buildSync({
  entryPoints: [path.join(import.meta.dirname, "viewer_app.ts")],
  bundle: true,
  write: false,
  format: "iife",
  minify: true,
  target: "es2022",
});

const js = bundled.outputFiles[0].text;

const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Whale Desktop Viewer</title>
  <style>
    :root {
      color-scheme: light dark;
    }
    * { box-sizing: border-box; }
    html, body {
      height: 100%;
      margin: 0;
    }
    body {
      padding: 8px;
      background: var(--color-background-primary, #0d1117);
      color: var(--color-text-primary, #e6edf3);
      font-family: var(--font-sans, system-ui, -apple-system, sans-serif);
      font-size: 12px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      height: 100vh;
      overflow: hidden;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 5px;
      padding: 6px 8px;
      border-radius: 8px;
      background: var(--color-background-secondary, #161b22);
      border: 1px solid var(--color-border-primary, #30363d);
      flex-shrink: 0;
    }
    .title {
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 6px;
      margin-right: auto;
    }
    .badge {
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }
    .badge.ok { background: #14532d; color: #86efac; }
    .badge.off { background: #7f1d1d; color: #fca5a5; }
    button, select {
      padding: 4px 8px;
      border-radius: 6px;
      border: 1px solid var(--color-border-primary, #30363d);
      background: var(--color-background-tertiary, #21262d);
      color: var(--color-text-primary, #e6edf3);
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
    }
    button:hover:not(:disabled), select:hover:not(:disabled) { border-color: #2496ed; }
    button:disabled, select:disabled { opacity: 0.45; cursor: default; }
    button.primary { background: #1d4ed8; border-color: #2496ed; color: #fff; }
    button.active { border-color: #22c55e; color: #86efac; }
    .subbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: var(--color-text-secondary, #8b949e);
      font-size: 11px;
      padding: 0 2px;
      flex-shrink: 0;
    }
    .stage {
      position: relative;
      width: 100%;
      flex: 1 1 0;
      min-height: 120px;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--color-border-primary, #30363d);
      background: #05080d;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 4px;
    }
    .stage img, .stage canvas {
      display: none;
      flex-shrink: 0;
      cursor: crosshair;
      user-select: none;
      image-rendering: auto;
      border-radius: 4px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
    }
    .vnc-shell {
      display: none;
      position: relative;
      overflow: hidden;
      flex-shrink: 0;
      border-radius: 4px;
      background: #05080d;
    }
    .stage iframe {
      display: block;
      border: 0;
      background: #05080d;
      transform-origin: top left;
    }
    .placeholder {
      padding: 28px 16px;
      text-align: center;
      color: var(--color-text-secondary, #8b949e);
    }
    .inputrow {
      display: flex;
      gap: 6px;
      flex-shrink: 0;
    }
    .inputrow input {
      flex: 1;
      padding: 5px 9px;
      border-radius: 6px;
      border: 1px solid var(--color-border-primary, #30363d);
      background: var(--color-background-secondary, #161b22);
      color: var(--color-text-primary, #e6edf3);
      font-size: 12px;
    }
    pre.logs {
      margin: 0;
      padding: 7px 9px;
      border-radius: 6px;
      background: var(--color-background-secondary, #161b22);
      border: 1px solid var(--color-border-primary, #30363d);
      color: var(--color-text-secondary, #8b949e);
      font-size: 11px;
      line-height: 1.35;
      max-height: 96px;
      min-height: 74px;
      overflow: auto;
      white-space: pre-wrap;
      flex-shrink: 0;
    }
  </style>
</head>
<body>
  <div class="toolbar">
    <div class="title">
      <span>Whale Desktop</span>
      <span id="statusBadge" class="badge off">INIT</span>
    </div>
    <button id="btnStart" class="primary">Start</button>
    <button id="btnStop">Stop</button>
    <select id="resSelect" title="Virtual Display Resolution">
      <option value="1920x1080">1920×1080</option>
      <option value="1600x900">1600×900</option>
      <option value="1280x720" selected>1280×720</option>
      <option value="854x480">854×480</option>
      <option value="720x480">720×480</option>
      <option value="680x480">680×480</option>
    </select>
    <select id="zoomSelect" title="Viewer Zoom Level inside Antigravity">
      <option value="fit" selected>Zoom: Fit</option>
      <option value="1">Zoom: 100%</option>
      <option value="0.75">Zoom: 75%</option>
      <option value="0.5">Zoom: 50%</option>
      <option value="0.35">Zoom: 35%</option>
    </select>
    <button id="btnMode" class="active" title="Cycle between Crisp 4:4:4+PNG Stream, Lossless PNG, and Live VNC">View: Stream</button>
    <button id="btnCmd">Whale CMD</button>
    <button id="btnNoVnc" class="primary">Open noVNC</button>
    <button id="btnBackup">Save Backup</button>
    <button id="btnLive" class="active">Live: ON</button>
    <button id="btnRefresh">Refresh</button>
  </div>
  <div class="subbar">
    <span id="metaInfo">Connecting...</span>
    <span id="coords">(x, y)</span>
  </div>
  <div class="stage">
    <canvas id="screenCanvas" width="1280" height="720"></canvas>
    <img id="screenImg" alt="Whale Desktop Screen" />
    <div id="vncShell" class="vnc-shell">
      <iframe id="vncFrame" title="Whale Live noVNC" allowfullscreen></iframe>
    </div>
    <div id="placeholder" class="placeholder">Loading Whale Desktop...</div>
  </div>
  <div class="inputrow">
    <input id="typeInput" type="text" placeholder="Type text into focused window inside Docker and press Enter..." />
    <button id="btnSendText">Type</button>
  </div>
  <pre id="logs" class="logs">Waiting for events...</pre>
  <script>${js}</script>
</body>
</html>
`;

fs.writeFileSync(outHtml, html, "utf8");
console.log(`Built ${outHtml} (${Buffer.byteLength(html, "utf8")} bytes)`);
