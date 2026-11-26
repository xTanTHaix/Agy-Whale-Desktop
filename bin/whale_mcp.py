"""Whale Desktop MCP Server for Antigravity on Windows.

Manages an isolated Docker Linux virtual desktop (Xvfb + XFCE4 + x11vnc + noVNC)
with screenshot vision, mouse/keyboard automation, visible Whale CMD status HUD,
and offline plugin-local image archive backup and auto-recovery.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import TextContent

PLUGIN_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DOCKER_DIR: Final[Path] = PLUGIN_ROOT / "docker"
IMAGES_DIR: Final[Path] = PLUGIN_ROOT / "images"
IMAGE_ARCHIVE: Final[Path] = IMAGES_DIR / "whale-desktop-v1.tar.gz"
MANIFEST_PATH: Final[Path] = IMAGES_DIR / "manifest.json"
LAUNCH_HUD_CMD: Final[Path] = PLUGIN_ROOT / "bin" / "launch_hud.cmd"

DEFAULT_DATA_DIR: Final[Path] = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "whale-desktop"
DATA_DIR: Final[Path] = Path(os.environ.get("WHALE_DESKTOP_DATA") or DEFAULT_DATA_DIR)
SANDBOX_WORKSPACE: Final[Path] = DATA_DIR / "workspace"
ACTIVITY_LOG: Final[Path] = DATA_DIR / "activity.log"
HUD_PID_FILE: Final[Path] = DATA_DIR / "hud.pid"
CONFIG_FILE: Final[Path] = DATA_DIR / "desktop_config.json"

DEFAULT_WIDTH: Final[int] = 1280
DEFAULT_HEIGHT: Final[int] = 720
DEFAULT_CPUS: Final[float] = 6.0
DEFAULT_MEMORY: Final[str] = "6g"

CONTAINER_NAME: Final[str] = os.environ.get("WHALE_DESKTOP_CONTAINER", "whale-desktop")
IMAGE_TAG: Final[str] = os.environ.get("WHALE_DESKTOP_IMAGE", "whale-desktop:v1")
NOVNC_PORT: Final[int] = int(os.environ.get("WHALE_DESKTOP_NOVNC_PORT", "16080"))
NOVNC_URL: Final[str] = f"http://127.0.0.1:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=off"

VIEWER_URI: Final[str] = "ui://whale-desktop/viewer.html"
VIEWER_HTML_PATH: Final[Path] = PLUGIN_ROOT / "bin" / "viewer.html"
VIEWER_CSP: Final[dict[str, Any]] = {
    "frameDomains": [
        f"http://127.0.0.1:{NOVNC_PORT}",
        f"http://localhost:{NOVNC_PORT}",
    ],
    "connectDomains": [
        f"http://127.0.0.1:{NOVNC_PORT}",
        f"http://localhost:{NOVNC_PORT}",
        f"ws://127.0.0.1:{NOVNC_PORT}",
        f"ws://localhost:{NOVNC_PORT}",
    ],
}
PANE_META: Final[dict[str, Any]] = {
    "ui": {"resourceUri": VIEWER_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Whale Screen"},
            {"type": "global", "title": "Whale Screen"},
        ]
    },
}
APP_ONLY_META: Final[dict[str, Any]] = {"ui": {"visibility": ["app"]}}

DOCKER_CANDIDATES: Final[list[Path]] = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "Docker Desktop.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
    Path(os.environ.get("ProgramW6432", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe",
]


def _find_docker_desktop_exe() -> Path | None:
    """Locate Docker Desktop across per-user and machine-wide Windows installs."""
    for candidate in DOCKER_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None

CREATE_NO_WINDOW: Final[int] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_CONSOLE: Final[int] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

mcp = MCPServer(
    name="whale-desktop",
    instructions=(
        "Isolated Docker virtual desktop with noVNC, mouse/keyboard GUI control, "
        "visible Whale CMD status window, and offline plugin-local image backups. "
        "Always start with desktop_start(), observe via desktop_screenshot(), "
        "act via mouse/keyboard tools, and verify via desktop_screenshot()."
    ),
)


def _log_activity(event: str) -> None:
    """Append a timestamped event line to the HUD activity log (keeps last 200 lines)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H:%M:%S")
    lines: list[str] = []
    if ACTIVITY_LOG.exists():
        lines = ACTIVITY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-199:]
    lines.append(f"  [{ts}] {event}")
    ACTIVITY_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[bytes]:
    """Run a host CLI command suppressing console window flashes."""
    return subprocess.run(
        args,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def _docker_ok() -> bool:
    """Return True if Docker daemon responds cleanly."""
    try:
        res = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=5.0)
        return res.returncode == 0
    except Exception:
        return False


def _ensure_docker_ready(wait_sec: float = 60.0) -> str:
    """Start Docker Desktop if stopped and wait until the daemon responds."""
    if _docker_ok():
        return "already_running"
    docker_exe = _find_docker_desktop_exe()
    if docker_exe is None:
        raise RuntimeError(
            "Docker daemon is not responding and Docker Desktop.exe was not found in standard Windows locations."
        )
    _log_activity("Starting Docker Desktop...")
    subprocess.Popen([str(docker_exe)], creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        if _docker_ok():
            _log_activity("Docker Desktop ready.")
            return "started"
        time.sleep(2.0)
    raise RuntimeError(f"Docker daemon did not become ready within {int(wait_sec)}s.")


def _image_exists() -> bool:
    """Return True if the target Docker image exists locally."""
    res = _run(["docker", "image", "inspect", IMAGE_TAG, "--format", "{{.Id}}"], timeout=10.0)
    return res.returncode == 0


def _container_state() -> str:
    """Return lowercase container state ('running', 'exited', 'missing', etc.)."""
    res = _run(
        ["docker", "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$", "--format", "{{.State}}"],
        timeout=10.0,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return "missing"
    return res.stdout.decode("utf-8", "replace").strip().splitlines()[0].lower()


def _export_image_archive() -> dict[str, Any]:
    """Stream `docker save` into compressed plugin archive images/whale-desktop-v1.tar.gz."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = IMAGE_ARCHIVE.with_suffix(".gz.tmp")
    _log_activity(f"Saving image backup -> {IMAGE_ARCHIVE.name}...")

    proc = subprocess.Popen(
        ["docker", "save", IMAGE_TAG],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None
    sha = hashlib.sha256()
    raw_bytes = 0
    with gzip.open(tmp_path, "wb", compresslevel=6) as gz:
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            raw_bytes += len(chunk)
            sha.update(chunk)
            gz.write(chunk)
    stderr_bytes = proc.stderr.read() if proc.stderr else b""
    rc = proc.wait()
    if rc != 0:
        if tmp_path.exists():
            tmp_path.unlink()
        raise RuntimeError(f"docker save failed: {stderr_bytes.decode('utf-8', 'replace')}")

    os.replace(tmp_path, IMAGE_ARCHIVE)
    compressed_bytes = IMAGE_ARCHIVE.stat().st_size
    manifest = {
        "image": IMAGE_TAG,
        "archive": IMAGE_ARCHIVE.name,
        "tar_sha256": sha.hexdigest(),
        "tar_bytes": raw_bytes,
        "compressed_bytes": compressed_bytes,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _log_activity(f"Backup saved ({compressed_bytes / (1024 * 1024):.1f} MB)")
    return manifest


def _restore_image_archive() -> dict[str, Any]:
    """Load the backed-up compressed tar archive back into Docker via `docker load`."""
    if not IMAGE_ARCHIVE.exists():
        raise FileNotFoundError(f"No offline backup archive found at {IMAGE_ARCHIVE}")
    _log_activity(f"Restoring image from backup {IMAGE_ARCHIVE.name}...")
    proc = subprocess.Popen(
        ["docker", "load"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.stdin is not None
    with gzip.open(IMAGE_ARCHIVE, "rb") as gz:
        while True:
            chunk = gz.read(1024 * 1024)
            if not chunk:
                break
            proc.stdin.write(chunk)
    proc.stdin.close()
    out, err = proc.communicate(timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"docker load failed: {err.decode('utf-8', 'replace')}")
    _log_activity("Image restored cleanly from plugin backup.")
    return {"restored": True, "output": out.decode("utf-8", "replace").strip()}


def _ensure_image() -> str:
    """Ensure IMAGE_TAG exists in Docker, restoring from backup or building if needed."""
    if _image_exists():
        return "cached_in_docker"
    if IMAGE_ARCHIVE.exists():
        _restore_image_archive()
        if _image_exists():
            return "restored_from_plugin_backup"
    _log_activity(f"Building Docker image {IMAGE_TAG}...")
    res = _run(["docker", "build", "-t", IMAGE_TAG, str(DOCKER_DIR)], timeout=600.0)
    if res.returncode != 0:
        raise RuntimeError(f"docker build failed:\n{res.stderr.decode('utf-8', 'replace')[-1500:]}")
    _export_image_archive()
    return "built_and_backed_up"


def _is_pid_running(pid: int) -> bool:
    """Check if a Windows process ID is alive."""
    if os.name != "nt" or pid <= 0:
        return False
    res = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=5.0)
    return str(pid) in res.stdout.decode("utf-8", "replace")


def _open_host_webview(url: str, title: str = "Whale Desktop — Live View") -> None:
    """Launch the scoped Edge WebView2 pywebview window instead of opening Chrome."""
    venv_pyw = PLUGIN_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    venv_py = PLUGIN_ROOT / ".venv" / "Scripts" / "python.exe"
    py_exe = venv_pyw if venv_pyw.exists() else venv_py if venv_py.exists() else None
    script = PLUGIN_ROOT / "bin" / "host_webview.py"
    if py_exe is not None and script.exists():
        subprocess.Popen(
            [str(py_exe), str(script), url, "--title", title],
            creationflags=CREATE_NO_WINDOW,
        )
        return
    webbrowser.open(url)


def _launch_hud_window(*, force: bool = False) -> bool:
    """Launch the visible Whale CMD Status HUD console window on Windows."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not force and HUD_PID_FILE.exists():
        try:
            old_pid = int(HUD_PID_FILE.read_text(encoding="utf-8").strip())
            if _is_pid_running(old_pid):
                return False
        except Exception:
            pass

    venv_py = PLUGIN_ROOT / ".venv" / "Scripts" / "python.exe"
    py_cmd = (
        str(venv_py)
        if venv_py.exists()
        else sys.executable
        if Path(sys.executable).exists()
        else (shutil.which("python") or "python")
    )
    proc = subprocess.Popen(
        [py_cmd, str(PLUGIN_ROOT / "bin" / "whale_hud.py")],
        creationflags=CREATE_NEW_CONSOLE,
    )
    HUD_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    return True


def _ctl_via_http(action: str, params: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """Fast persistent /ctl HTTP call (~5ms) avoiding docker exec Python cold-start."""
    import urllib.error
    import urllib.request

    payload = json.dumps({"action": action, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{NOVNC_PORT}/ctl",
        data=payload,
        headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{NOVNC_PORT}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return None


def _ctl(action: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    """Dispatch JSON control action via persistent HTTP /ctl daemon with docker-exec fallback."""
    if _container_state() != "running":
        raise RuntimeError("Whale Desktop container is not running. Call desktop_start() first.")

    safe_params = params or {}
    data = _ctl_via_http(action, safe_params, timeout=timeout)
    if data is None:
        payload = json.dumps({"action": action, "params": safe_params}).encode("utf-8")
        res = _run(
            ["docker", "exec", "-i", CONTAINER_NAME, "python3", "/usr/local/bin/desktop_ctl.py"],
            input_bytes=payload,
            timeout=timeout,
        )
        stdout = res.stdout.decode("utf-8", "replace").strip()
        stderr = res.stderr.decode("utf-8", "replace").strip()
        if not stdout:
            raise RuntimeError(f"desktop_ctl action '{action}' returned empty output (rc={res.returncode}): {stderr}")
        data = json.loads(stdout)

    if not data.get("ok"):
        raise RuntimeError(f"desktop_ctl action '{action}' failed: {data.get('error') or data.get('stderr') or data}")
    return data


@mcp.tool()
def desktop_status() -> dict[str, Any]:
    """Check Docker daemon, Whale Desktop container state, backup archive, and noVNC URL."""
    daemon_ready = _docker_ok()
    state = _container_state() if daemon_ready else "docker_offline"
    img_ok = _image_exists() if daemon_ready else False
    backup_exists = IMAGE_ARCHIVE.exists()
    backup_mb = round(IMAGE_ARCHIVE.stat().st_size / (1024 * 1024), 2) if backup_exists else 0.0

    manifest: dict[str, Any] = {}
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    return {
        "docker_online": daemon_ready,
        "container": CONTAINER_NAME,
        "state": state,
        "image": IMAGE_TAG,
        "image_loaded": img_ok,
        "novnc_url": NOVNC_URL,
        "backup_archive": str(IMAGE_ARCHIVE),
        "backup_exists": backup_exists,
        "backup_mb": backup_mb,
        "backup_saved_at": manifest.get("saved_at"),
        "workspace_dir": str(SANDBOX_WORKSPACE),
    }


def _validate_resolution(width: int, height: int) -> None:
    """Validate requested virtual desktop dimensions (supports mobile & desktop sizes)."""
    if not (360 <= width <= 2560 and 320 <= height <= 1600):
        raise ValueError("Screen resolution must be within 360x320 .. 2560x1600.")


def _load_config() -> dict[str, Any]:
    """Load last saved container resolution and resource configuration."""
    cfg: dict[str, Any] = {
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "cpus": DEFAULT_CPUS,
        "memory": DEFAULT_MEMORY,
    }
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                w = int(loaded.get("width", cfg["width"]))
                h = int(loaded.get("height", cfg["height"]))
                _validate_resolution(w, h)
                cfg["width"] = w
                cfg["height"] = h
                cfg["cpus"] = float(loaded.get("cpus", cfg["cpus"]))
                cfg["memory"] = str(loaded.get("memory", cfg["memory"]))
        except Exception:
            pass
    return cfg


def _save_config(width: int, height: int, cpus: float, memory: str) -> None:
    """Persist current container resolution and resource limits for future starts."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({"width": width, "height": height, "cpus": cpus, "memory": memory}, indent=2) + "\n",
        encoding="utf-8",
    )


def _get_xrandr_bounds() -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Return ((cur_w, cur_h), (max_w, max_h)) from DISPLAY=:1 xrandr inside container."""
    res = _run(["docker", "exec", CONTAINER_NAME, "bash", "-lc", "DISPLAY=:1 xrandr 2>&1"], timeout=8.0)
    if res.returncode != 0:
        return None, None
    out = res.stdout.decode("utf-8", "replace")
    m_cur = re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
    m_max = re.search(r"maximum\s+(\d+)\s*x\s*(\d+)", out)
    cur = (int(m_cur.group(1)), int(m_cur.group(2))) if m_cur else None
    mx = (int(m_max.group(1)), int(m_max.group(2))) if m_max else None
    return cur, mx


def _try_live_xrandr_resize(width: int, height: int) -> bool:
    """Apply live XRandR resolution change when target fits inside Xvfb framebuffer limits."""
    cur, mx = _get_xrandr_bounds()
    if cur == (width, height):
        return True
    if mx is not None and (width > mx[0] or height > mx[1]):
        return False

    mode_name = f"{width}x{height}"
    htotal = width + 96
    vtotal = height + 18
    pclk = round((htotal * vtotal * 60.0) / 1_000_000.0, 2)
    cmd = (
        f"DISPLAY=:1 xrandr --newmode '{mode_name}' {pclk} "
        f"{width} {width + 16} {width + 48} {htotal} "
        f"{height} {height + 3} {height + 8} {vtotal} -hsync +vsync 2>/dev/null || true; "
        f"DISPLAY=:1 xrandr --addmode screen '{mode_name}' 2>/dev/null || true; "
        f"DISPLAY=:1 xrandr --output screen --mode '{mode_name}'"
    )
    res = _run(["docker", "exec", CONTAINER_NAME, "bash", "-lc", cmd], timeout=8.0)
    if res.returncode != 0:
        return False
    after_cur, _ = _get_xrandr_bounds()
    return after_cur == (width, height)


def _start_new_container(
    width: int,
    height: int,
    cpus: float,
    memory: str,
    mount_path: str | None = None,
    mount_rw: bool = False,
) -> None:
    """Create and boot a fresh Whale Desktop container at the target resolution."""
    SANDBOX_WORKSPACE.mkdir(parents=True, exist_ok=True)
    if _container_state() != "missing":
        _run(["docker", "rm", "-f", CONTAINER_NAME], timeout=15.0)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "--label",
        "managed-by=whale-desktop",
        "--cpus",
        str(cpus),
        "--memory",
        memory.strip().lower(),
        "--shm-size",
        "1g",
        "--pids-limit",
        "256",
        "-p",
        f"127.0.0.1:{NOVNC_PORT}:6080",
        "-e",
        f"SCREEN_WIDTH={width}",
        "-e",
        f"SCREEN_HEIGHT={height}",
        "-v",
        f"{SANDBOX_WORKSPACE}:/home/whale/workspace",
    ]
    if mount_path:
        host_path = Path(mount_path).resolve()
        if not host_path.exists():
            raise FileNotFoundError(f"mount_path does not exist: {host_path}")
        mode = "rw" if mount_rw else "ro"
        cmd.extend(["-v", f"{host_path}:/home/whale/host:{mode}"])
    cmd.append(IMAGE_TAG)

    res = _run(cmd, timeout=30.0)
    if res.returncode != 0:
        raise RuntimeError(f"docker run failed: {res.stderr.decode('utf-8', 'replace')}")
    _log_activity(f"Started container ({width}x{height}, {cpus} CPU, {memory})")

    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        try:
            _ctl("windows", timeout=4.0)
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise RuntimeError("Container started, but X11 desktop did not become responsive within 25s.")


@mcp.tool()
def desktop_start(
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    cpus: float = DEFAULT_CPUS,
    memory: str = DEFAULT_MEMORY,
    open_console: bool = True,
    open_browser: bool = False,
    mount_path: str | None = None,
    mount_rw: bool = False,
) -> dict[str, Any]:
    """Start the isolated Docker virtual desktop, restoring its image from backup if needed."""
    _validate_resolution(width, height)
    if not re.fullmatch(r"\d+[mg]", memory.strip().lower()):
        raise ValueError("memory must look like '2g' or '2048m'.")

    daemon_action = _ensure_docker_ready()
    image_source = _ensure_image()
    _save_config(width, height, cpus, memory.strip().lower())

    current = _container_state()
    if current == "running":
        cur_dim, _ = _get_xrandr_bounds()
        if cur_dim != (width, height):
            if _try_live_xrandr_resize(width, height):
                _log_activity(f"Resized running container live -> {width}x{height}")
            else:
                _start_new_container(width, height, cpus, memory, mount_path, mount_rw)
        else:
            _log_activity("desktop_start requested (already running)")
    else:
        _start_new_container(width, height, cpus, memory, mount_path, mount_rw)

    hud_opened = _launch_hud_window(force=False) if open_console else False
    if open_browser:
        _open_host_webview(NOVNC_URL)

    return {
        "ok": True,
        "docker_daemon": daemon_action,
        "image_source": image_source,
        "container": CONTAINER_NAME,
        "state": _container_state(),
        "resolution": f"{width}x{height}",
        "novnc_url": NOVNC_URL,
        "hud_window_launched": hud_opened,
        "workspace_dir": str(SANDBOX_WORKSPACE),
    }


@mcp.tool()
def desktop_resize(width: int, height: int) -> dict[str, Any]:
    """Resize the running Whale Desktop display (live via XRandR when fitting framebuffer, otherwise restarts cleanly)."""
    _validate_resolution(width, height)
    cfg = _load_config()
    _save_config(width, height, cfg["cpus"], cfg["memory"])
    if _container_state() != "running":
        return desktop_start(width=width, height=height, cpus=cfg["cpus"], memory=cfg["memory"], open_console=False)
    if _try_live_xrandr_resize(width, height):
        _log_activity(f"Live resized -> {width}x{height}")
        return {"ok": True, "mode": "xrandr_live", "resolution": f"{width}x{height}"}
    _start_new_container(width, height, cfg["cpus"], cfg["memory"])
    return {"ok": True, "mode": "container_recreated", "resolution": f"{width}x{height}"}


@mcp.tool()
def desktop_stop(remove: bool = True) -> dict[str, Any]:
    """Stop (and optionally remove) the running Whale Desktop container."""
    if not _docker_ok():
        return {"ok": True, "state": "docker_offline"}
    state = _container_state()
    if state == "missing":
        return {"ok": True, "state": "already_removed"}
    if remove:
        _run(["docker", "rm", "-f", CONTAINER_NAME], timeout=20.0)
        _log_activity("Container stopped and removed.")
        return {"ok": True, "state": "removed"}
    _run(["docker", "stop", CONTAINER_NAME], timeout=20.0)
    _log_activity("Container stopped.")
    return {"ok": True, "state": "stopped"}


@mcp.tool()
def desktop_console(open_novnc_browser: bool = False) -> dict[str, Any]:
    """Launch or pop open the visible Whale CMD Status HUD console window on Windows."""
    launched = _launch_hud_window(force=True)
    if open_novnc_browser:
        _open_host_webview(NOVNC_URL)
    _log_activity("Opened Whale CMD HUD window.")
    return {"ok": True, "hud_launched": launched, "novnc_url": NOVNC_URL}


@mcp.tool()
def desktop_screenshot() -> list[Image | TextContent]:
    """Capture the current virtual desktop screen (with pointer) so the agent can inspect it visually."""
    data = _ctl("screenshot", timeout=15.0)
    raw_png = base64.b64decode(data["image_b64"])
    _log_activity(f"screenshot ({data['geometry']}, mouse={data['mouse']})")
    meta = {
        "geometry": data["geometry"],
        "mouse": data["mouse"],
        "bytes": data["bytes"],
        "novnc_url": NOVNC_URL,
    }
    return [
        Image(data=raw_png, format="png"),
        TextContent(type="text", text=json.dumps(meta)),
    ]


@mcp.tool()
def desktop_click(x: int, y: int, button: str = "left", clicks: int = 1, smooth: bool = True) -> dict[str, Any]:
    """Glide mouse to pixel (x, y) inside the virtual desktop and click (1=single, 2=double)."""
    res = _ctl("click", {"x": x, "y": y, "button": button, "clicks": clicks, "smooth": smooth})
    _log_activity(f"click ({x}, {y}) btn={button} x{clicks}")
    return res


@mcp.tool()
def desktop_move(x: int, y: int, smooth: bool = True) -> dict[str, Any]:
    """Move mouse cursor to pixel (x, y) inside the virtual desktop."""
    res = _ctl("move", {"x": x, "y": y, "smooth": smooth})
    _log_activity(f"move ({x}, {y})")
    return res


@mcp.tool()
def desktop_drag(
    x1: int, y1: int, x2: int, y2: int, button: str = "left", duration_ms: int = 260
) -> dict[str, Any]:
    """Smoothly press and drag across intermediate pixels from (x1, y1) to (x2, y2)."""
    res = _ctl("drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button, "duration_ms": duration_ms})
    _log_activity(f"drag ({x1},{y1}) -> ({x2},{y2})")
    return res


@mcp.tool()
def desktop_scroll(
    direction: str = "down",
    clicks: int = 3,
    x: int | None = None,
    y: int | None = None,
) -> dict[str, Any]:
    """Scroll up/down/left/right at optional pixel coordinates (x, y)."""
    res = _ctl("scroll", {"direction": direction, "clicks": clicks, "x": x, "y": y})
    _log_activity(f"scroll {direction} x{clicks}" + (f" at ({x},{y})" if x is not None else ""))
    return res


@mcp.tool()
def desktop_type(text: str, enter: bool = False) -> dict[str, Any]:
    """Type UTF-8 text (English, Thai, URLs, code) into the currently focused window."""
    res = _ctl("type", {"text": text, "enter": enter}, timeout=35.0)
    preview = text.replace("\n", " ")[:36]
    _log_activity(f'type "{preview}" (enter={enter})')
    return res


@mcp.tool()
def desktop_key(combo: str) -> dict[str, Any]:
    """Send a keyboard key or shortcut combination (e.g. 'Return', 'Escape', 'Tab', 'ctrl+l', 'alt+F4')."""
    res = _ctl("key", {"combo": combo})
    _log_activity(f"key {combo}")
    return res


@mcp.tool()
def desktop_key_hold(key: str, duration_ms: int = 400) -> dict[str, Any]:
    """Hold a key down for duration_ms milliseconds then release cleanly (useful for games, sliders, selection)."""
    res = _ctl("key_hold", {"key": key, "duration_ms": duration_ms}, timeout=max(10.0, duration_ms / 1000.0 + 5.0))
    _log_activity(f"key_hold {key} ({duration_ms}ms)")
    return res


@mcp.tool()
def desktop_clipboard_get() -> dict[str, Any]:
    """Read current UTF-8 text from the X11 clipboard inside Whale Desktop."""
    res = _ctl("clipboard_get", timeout=8.0)
    _log_activity(f"clipboard_get ({res.get('chars', 0)} chars)")
    return res


@mcp.tool()
def desktop_clipboard_set(text: str) -> dict[str, Any]:
    """Write UTF-8 text directly into the X11 clipboard inside Whale Desktop."""
    res = _ctl("clipboard_set", {"text": text}, timeout=8.0)
    _log_activity(f"clipboard_set ({len(text)} chars)")
    return res


@mcp.tool()
def desktop_inspect_ui(
    include_ocr: bool = True,
    annotate_image: bool = True,
    max_elements: int = 70,
) -> list[Image | TextContent]:
    """Extract clickable AT-SPI UI controls + Thai/English OCR targets with Set-of-Mark (#N) overlay image."""
    data = _ctl(
        "inspect_ui",
        {"include_ocr": include_ocr, "annotate_image": annotate_image, "max_elements": max_elements},
        timeout=25.0,
    )
    raw_png = base64.b64decode(data["image_b64"])
    _log_activity(
        f"inspect_ui (atspi={data.get('atspi_count', 0)}, ocr={data.get('ocr_count', 0)})"
    )
    meta = {
        "geometry": data["geometry"],
        "mouse": data["mouse"],
        "active_window": data.get("active_window", ""),
        "windows": data.get("windows", []),
        "atspi_count": data.get("atspi_count", 0),
        "ocr_count": data.get("ocr_count", 0),
        "elements": data.get("elements", []),
    }
    return [
        Image(data=raw_png, format="png"),
        TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
    ]


@mcp.tool()
def desktop_click_text(
    query: str,
    button: str = "left",
    clicks: int = 1,
    match_index: int = 0,
    smooth: bool = True,
) -> dict[str, Any]:
    """Locate a visible button or text label via AT-SPI + Thai/English OCR and click its exact center."""
    res = _ctl(
        "click_text",
        {
            "query": query,
            "button": button,
            "clicks": clicks,
            "match_index": match_index,
            "smooth": smooth,
        },
        timeout=25.0,
    )
    _log_activity(f'click_text "{query}" -> {res.get("x")},{res.get("y")}')
    return res


@mcp.tool()
def desktop_wait_stable(timeout_sec: float = 4.0, stable_ms: int = 300) -> list[Image | TextContent]:
    """Wait until screen animations/loading settle for stable_ms, then return a fresh PNG screenshot."""
    data = _ctl(
        "wait_stable",
        {"timeout_sec": timeout_sec, "stable_ms": stable_ms, "return_screenshot": True},
        timeout=max(10.0, timeout_sec + 5.0),
    )
    raw_png = base64.b64decode(data["image_b64"])
    _log_activity(f"wait_stable settled={data.get('settled')}")
    meta = {
        "geometry": data["geometry"],
        "mouse": data["mouse"],
        "settled": data.get("settled", True),
        "bytes": data["bytes"],
    }
    return [
        Image(data=raw_png, format="png"),
        TextContent(type="text", text=json.dumps(meta)),
    ]


@mcp.tool()
def desktop_act(
    steps: list[dict[str, Any]],
    wait_after_ms: int = 200,
    return_screenshot: bool = True,
) -> list[Image | TextContent]:
    """Execute a multi-step GUI batch (click, click_text, move, drag, scroll, type, key, key_hold, wait, wait_stable) in one turn."""
    data = _ctl(
        "act",
        {
            "steps": steps,
            "wait_after_ms": wait_after_ms,
            "return_screenshot": return_screenshot,
        },
        timeout=45.0,
    )
    _log_activity(f"act x{data.get('steps_executed', 0)} changed={data.get('screen_changed')}")
    meta = {
        "ok": True,
        "steps_executed": data.get("steps_executed", 0),
        "screen_changed": data.get("screen_changed", False),
        "changed_pct": data.get("changed_pct", 0.0),
        "geometry": data.get("geometry"),
        "mouse": data.get("mouse"),
        "active_window": data.get("active_window", ""),
        "results": data.get("results", []),
    }
    out: list[Image | TextContent] = []
    if return_screenshot and data.get("image_b64"):
        out.append(Image(data=base64.b64decode(data["image_b64"]), format="png"))
    out.append(TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)))
    return out


@mcp.tool()
def desktop_open_app(command: str) -> dict[str, Any]:
    """Launch a graphical application on the virtual display (e.g. 'whale-webview https://example.com', 'xfce4-terminal', 'mousepad')."""
    res = _ctl("open_app", {"command": command})
    _log_activity(f"open_app: {command[:50]}")
    return res


@mcp.tool()
def desktop_windows() -> dict[str, Any]:
    """List open GUI windows inside the virtual desktop with their coordinates and sizes."""
    return _ctl("windows")


@mcp.tool()
def desktop_exec(command: str, as_root: bool = False, timeout_sec: int = 120) -> dict[str, Any]:
    """Run a shell command inside the Whale Desktop container (set as_root=True to apt-get install new apps)."""
    if _container_state() != "running":
        raise RuntimeError("Whale Desktop container is not running.")
    cmd = ["docker", "exec"]
    if as_root:
        cmd.extend(["-u", "0"])
    cmd.extend([CONTAINER_NAME, "/bin/bash", "-lc", command])
    res = _run(cmd, timeout=float(max(5, min(timeout_sec, 600))))
    _log_activity(f"exec{'(root)' if as_root else ''}: {command[:45]} -> rc={res.returncode}")
    return {
        "ok": res.returncode == 0,
        "returncode": res.returncode,
        "stdout": res.stdout.decode("utf-8", "replace")[-6000:],
        "stderr": res.stderr.decode("utf-8", "replace")[-3000:],
    }


@mcp.tool()
def desktop_save_image() -> dict[str, Any]:
    """Commit any newly installed apps in the container to whale-desktop:v1 and save offline backup archive."""
    _ensure_docker_ready()
    state = _container_state()
    committed = False
    if state == "running":
        _log_activity("Committing running container -> whale-desktop:v1...")
        res = _run(["docker", "commit", CONTAINER_NAME, IMAGE_TAG], timeout=180.0)
        if res.returncode != 0:
            raise RuntimeError(f"docker commit failed: {res.stderr.decode('utf-8', 'replace')}")
        committed = True
    elif not _image_exists():
        raise RuntimeError("Neither a running container nor an image exists to save.")

    manifest = _export_image_archive()
    return {
        "ok": True,
        "committed_running_container": committed,
        "archive_path": str(IMAGE_ARCHIVE),
        **manifest,
    }


@mcp.tool()
def desktop_restore_image() -> dict[str, Any]:
    """Force-restore whale-desktop:v1 into Docker from the plugin's offline backup archive."""
    _ensure_docker_ready()
    info = _restore_image_archive()
    return {"ok": True, "image": IMAGE_TAG, "archive_path": str(IMAGE_ARCHIVE), **info}


def _parse_png_geometry(raw_png: bytes) -> str | None:
    """Extract WxH directly from the PNG IHDR chunk without extra container subprocesses."""
    if len(raw_png) >= 24 and raw_png[:8] == b"\x89PNG\r\n\x1a\n" and raw_png[12:16] == b"IHDR":
        w, h = struct.unpack(">II", raw_png[16:24])
        return f"{w}x{h}"
    return None


def _fast_screenshot_png() -> tuple[bytes, str] | None:
    """Fetch true lossless PNG frame via in-container HTTP localhost X11 cache (falling back to scrot)."""
    try:
        import urllib.request

        req = urllib.request.Request(f"http://127.0.0.1:{NOVNC_PORT}/frame.png")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read()
            geom = resp.headers.get("X-Whale-Geometry") or f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}"
            if raw:
                return raw, geom
    except Exception:
        pass

    res = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "bash",
            "-lc",
            "DISPLAY=:1 scrot -p -o /dev/shm/.whale_poll.png && cat /dev/shm/.whale_poll.png",
        ],
        timeout=8.0,
    )
    if res.returncode != 0 or not res.stdout:
        return None
    raw_png = res.stdout
    geom = _parse_png_geometry(raw_png)
    if geom is None:
        return None
    return raw_png, geom


def _collect_viewer_state(*, with_screen: bool = True) -> dict[str, Any]:
    """Collect compact state + lossless sharp PNG frame for the Antigravity MCP App viewer."""
    logs: list[str] = []
    if ACTIVITY_LOG.exists():
        logs = ACTIVITY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]

    backup_exists = IMAGE_ARCHIVE.exists()
    backup_mb = round(IMAGE_ARCHIVE.stat().st_size / (1024 * 1024), 2) if backup_exists else 0.0

    if with_screen:
        try:
            fast = _fast_screenshot_png()
            if fast is not None:
                raw_bytes, geom = fast
                is_jpg = raw_bytes[:3] == b"\xff\xd8\xff"
                payload: dict[str, Any] = {
                    "running": True,
                    "docker_online": True,
                    "container": CONTAINER_NAME,
                    "state": "running",
                    "geometry": geom,
                    "novnc_url": NOVNC_URL,
                    "backup_exists": backup_exists,
                    "backup_mb": backup_mb,
                    "logs": logs,
                }
                if is_jpg:
                    payload["image_mime"] = "image/jpeg"
                    payload["image_b64"] = base64.b64encode(raw_bytes).decode("ascii")
                elif len(raw_bytes) > 1_200_000:
                    import io
                    from PIL import Image as PilImage

                    buf = io.BytesIO()
                    PilImage.open(io.BytesIO(raw_bytes)).convert("RGB").save(
                        buf, format="JPEG", quality=94, subsampling=0
                    )
                    payload["image_mime"] = "image/jpeg"
                    payload["image_b64"] = base64.b64encode(buf.getvalue()).decode("ascii")
                else:
                    payload["image_mime"] = "image/png"
                    payload["image_b64"] = base64.b64encode(raw_bytes).decode("ascii")
                return payload
        except Exception:
            pass

    status = desktop_status()
    running = status["state"] == "running"
    payload = {
        "running": running,
        "docker_online": status["docker_online"],
        "container": status["container"],
        "state": status["state"],
        "novnc_url": status["novnc_url"],
        "backup_exists": status["backup_exists"],
        "backup_mb": status["backup_mb"],
        "logs": logs,
    }
    if with_screen and running:
        try:
            snap = _ctl("screenshot", timeout=10.0)
            payload["geometry"] = snap["geometry"]
            raw_png = base64.b64decode(snap["image_b64"])
            if len(raw_png) > 850_000:
                import io
                from PIL import Image as PilImage

                buf = io.BytesIO()
                PilImage.open(io.BytesIO(raw_png)).convert("RGB").save(
                    buf, format="JPEG", quality=94, subsampling=0
                )
                payload["image_mime"] = "image/jpeg"
                payload["image_b64"] = base64.b64encode(buf.getvalue()).decode("ascii")
            else:
                payload["image_mime"] = "image/png"
                payload["image_b64"] = snap["image_b64"]
        except Exception as exc:
            payload["error"] = str(exc)
    return payload


@mcp.resource(
    VIEWER_URI,
    name="Whale Desktop Viewer",
    mime_type="text/html;profile=mcp-app",
    meta={"ui": {"prefersBorder": False, "csp": VIEWER_CSP}},
)
def viewer_resource() -> str:
    """Serve the bundled interactive Whale Screen MCP App HTML."""
    return VIEWER_HTML_PATH.read_text(encoding="utf-8")


@mcp.tool(title="Whale Screen", meta=PANE_META)
def desktop_viewer() -> dict[str, Any]:
    """Open the interactive Whale Screen live viewer pane inside Antigravity."""
    return _collect_viewer_state(with_screen=True)


@mcp.tool(meta=APP_ONLY_META)
def app_poll_screen() -> dict[str, Any]:
    """Refresh live Whale Desktop screen frame and telemetry for the MCP App UI."""
    return _collect_viewer_state(with_screen=True)


@mcp.tool(meta=APP_ONLY_META)
def app_click(x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, Any]:
    """Handle direct mouse click from the Whale Screen viewer and return updated frame."""
    desktop_click(x=x, y=y, button=button, clicks=clicks)
    time.sleep(0.15)
    return _collect_viewer_state(with_screen=True)


@mcp.tool(meta=APP_ONLY_META)
def app_type(text: str, enter: bool = True) -> dict[str, Any]:
    """Handle quick text input from the Whale Screen viewer and return updated frame."""
    desktop_type(text=text, enter=enter)
    time.sleep(0.15)
    return _collect_viewer_state(with_screen=True)


@mcp.tool(meta=APP_ONLY_META)
def app_resize(width: int, height: int) -> dict[str, Any]:
    """Resize the desktop display from the Whale Screen toolbar."""
    desktop_resize(width=width, height=height)
    time.sleep(0.15)
    return _collect_viewer_state(with_screen=True)


@mcp.tool(meta=APP_ONLY_META)
def app_control(op: str) -> dict[str, Any]:
    """Handle start/stop/console/save_image actions triggered by buttons in Whale Screen."""
    if op == "start":
        cfg = _load_config()
        desktop_start(
            width=cfg["width"],
            height=cfg["height"],
            cpus=cfg["cpus"],
            memory=cfg["memory"],
            open_console=True,
            open_browser=False,
        )
    elif op == "stop":
        desktop_stop(remove=True)
    elif op == "console":
        desktop_console(open_novnc_browser=False)
    elif op == "open_webview":
        _open_host_webview(NOVNC_URL)
        _log_activity("Opened Live View (pywebview).")
    elif op == "save_image":
        desktop_save_image()
    else:
        raise ValueError(f"Unsupported app_control op: {op}")
    return _collect_viewer_state(with_screen=True)


if __name__ == "__main__":
    mcp.run()
