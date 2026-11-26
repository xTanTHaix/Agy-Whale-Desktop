"""Whale Desktop live console status HUD for Windows.

Displays a Docker-whale ASCII banner, real-time container & backup archive status,
noVNC URL, and live action logs, plus keyboard shortcuts to open noVNC in browser.
"""

from __future__ import annotations

import ctypes
import json
import msvcrt
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PLUGIN_ROOT / "images" / "manifest.json"
IMAGE_ARCHIVE = PLUGIN_ROOT / "images" / "whale-desktop-v1.tar.gz"
DEFAULT_DATA_DIR = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "whale-desktop"
DATA_DIR = Path(os.environ.get("WHALE_DESKTOP_DATA") or DEFAULT_DATA_DIR)
ACTIVITY_LOG = DATA_DIR / "activity.log"

CONTAINER_NAME = os.environ.get("WHALE_DESKTOP_CONTAINER", "whale-desktop")
IMAGE_TAG = os.environ.get("WHALE_DESKTOP_IMAGE", "whale-desktop:v1")
NOVNC_PORT = int(os.environ.get("WHALE_DESKTOP_NOVNC_PORT", "16080"))
NOVNC_URL = f"http://127.0.0.1:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=off"

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[96m"
BLUE = "\x1b[94m"
GREEN = "\x1b[92m"
YELLOW = "\x1b[93m"
RED = "\x1b[91m"
WHITE = "\x1b[97m"

WHALE_ART = rf"""{CYAN}{BOLD}
                    ##         .
              ## ## ##        ==
           ## ## ## ##       ===
       /\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"\"___/ ===
  ~~~ {{  ~~ ~~~~ ~~~ ~~~~ ~~ ~ /  ===- ~~~
       \______ o           __/
         \    \         __/
          \____\_______/      {WHITE}WHALE DESKTOP{CYAN} (Docker + noVNC)
{RESET}"""


def enable_windows_ansi() -> None:
    """Enable virtual terminal ANSI escape processing on Windows consoles."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        kernel32.SetConsoleTitleW("Whale Desktop HUD - Docker Sandbox Monitor")
    except Exception:
        pass


def run_docker(args: list[str], timeout: float = 4.0) -> tuple[int, str]:
    """Run a quick docker CLI query suppressing console popups."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
        )
        return res.returncode, res.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def collect_snapshot() -> dict[str, str]:
    """Gather current Docker daemon, container, backup, and log state."""
    rc_info, _ = run_docker(["info", "--format", "{{.ServerVersion}}"])
    daemon_ok = rc_info == 0

    container_state = "STOPPED"
    uptime = "-"
    cpu_mem = "-"
    image_present = False

    if daemon_ok:
        rc_img, _ = run_docker(["image", "inspect", IMAGE_TAG, "--format", "{{.Id}}"])
        image_present = rc_img == 0

        rc_ps, ps_out = run_docker(
            ["ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$", "--format", "{{.State}}|{{.Status}}"]
        )
        if rc_ps == 0 and ps_out:
            parts = ps_out.splitlines()[0].split("|", 1)
            container_state = parts[0].upper()
            uptime = parts[1] if len(parts) > 1 else "-"

        if container_state == "RUNNING":
            rc_st, st_out = run_docker(
                ["stats", "--no-stream", "--format", "{{.CPUPerc}} CPU | {{.MemUsage}}", CONTAINER_NAME],
                timeout=6.0,
            )
            if rc_st == 0 and st_out:
                cpu_mem = st_out.splitlines()[0]

    backup_info = "Not saved yet"
    if IMAGE_ARCHIVE.exists():
        size_mb = IMAGE_ARCHIVE.stat().st_size / (1024 * 1024)
        saved_at = ""
        if MANIFEST_PATH.exists():
            try:
                manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
                saved_at = f" ({manifest.get('saved_at', '')})"
            except Exception:
                pass
        backup_info = f"READY - {size_mb:.1f} MB{saved_at}"

    logs: list[str] = []
    if ACTIVITY_LOG.exists():
        try:
            lines = ACTIVITY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            logs = lines[-8:]
        except Exception:
            pass

    return {
        "daemon": "ONLINE" if daemon_ok else "OFFLINE",
        "image": "LOADED" if image_present else "MISSING (backup available)" if IMAGE_ARCHIVE.exists() else "NOT BUILT",
        "state": container_state,
        "uptime": uptime,
        "stats": cpu_mem,
        "backup": backup_info,
        "logs": "\n".join(logs) if logs else "  (no activity recorded yet)",
    }


def render(snap: dict[str, str]) -> None:
    """Clear screen and render the live status HUD."""
    state = snap["state"]
    if state == "RUNNING":
        badge = f"{GREEN}{BOLD}[*] RUNNING{RESET}"
    elif state in ("CREATED", "RESTARTING"):
        badge = f"{YELLOW}{BOLD}[~] {state}{RESET}"
    else:
        badge = f"{RED}{BOLD}[ ] {state}{RESET}"

    daemon_badge = f"{GREEN}ONLINE{RESET}" if snap["daemon"] == "ONLINE" else f"{RED}OFFLINE{RESET}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    frame = [
        "\x1b[H\x1b[J",
        WHALE_ART,
        f" {BLUE}{'=' * 74}{RESET}",
        f"  {BOLD}Time:{RESET} {now}   {BOLD}Docker:{RESET} {daemon_badge}   {BOLD}Desktop:{RESET} {badge}",
        f"  {BOLD}Container:{RESET} {CONTAINER_NAME} ({snap['uptime']})",
        f"  {BOLD}Resources:{RESET} {snap['stats']}",
        f"  {BOLD}Image:{RESET}     {IMAGE_TAG} -> {snap['image']}",
        f"  {BOLD}Backup:{RESET}    {snap['backup']}",
        f"  {BOLD}noVNC URL:{RESET} {CYAN}{NOVNC_URL}{RESET}",
        f" {BLUE}{'=' * 74}{RESET}",
        f"  {BOLD}Recent GUI Activity:{RESET}",
        f"{DIM}{snap['logs']}{RESET}",
        f" {BLUE}{'-' * 74}{RESET}",
        f"  {YELLOW}[O]{RESET} Open noVNC in Browser   {YELLOW}[R]{RESET} Refresh Now   {YELLOW}[Q]{RESET} Close Window",
    ]
    sys.stdout.write("\n".join(frame) + "\n")
    sys.stdout.flush()


def main() -> int:
    """Run live console monitor loop until user quits."""
    enable_windows_ansi()
    last_draw = 0.0
    while True:
        now = time.monotonic()
        if now - last_draw >= 2.0:
            render(collect_snapshot())
            last_draw = now
        if os.name == "nt" and msvcrt.kbhit():
            ch = msvcrt.getwch().lower()
            if ch == "q":
                return 0
            if ch == "o":
                venv_pyw = PLUGIN_ROOT / ".venv" / "Scripts" / "pythonw.exe"
                venv_py = PLUGIN_ROOT / ".venv" / "Scripts" / "python.exe"
                py_exe = venv_pyw if venv_pyw.exists() else venv_py if venv_py.exists() else None
                script = PLUGIN_ROOT / "bin" / "host_webview.py"
                if py_exe is not None and script.exists():
                    subprocess.Popen(
                        [str(py_exe), str(script), NOVNC_URL],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    webbrowser.open(NOVNC_URL)
                last_draw = 0.0
            elif ch == "r":
                last_draw = 0.0
        time.sleep(0.15)


if __name__ == "__main__":
    raise SystemExit(main())
