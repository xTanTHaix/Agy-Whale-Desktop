#!/opt/whale-webview/bin/python
"""Lightweight WebKit2GTK pywebview launcher for Whale Desktop Linux containers.

Replaces bloated Chromium app windows with a clean, bar-free webview surface that
fits the virtual display cleanly and optionally injects a userscript file.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import webview


def _get_display_size() -> tuple[int, int]:
    """Read current X11 screen dimensions so the window fills the virtual desktop cleanly."""
    try:
        out = subprocess.check_output(["xdotool", "getdisplaygeometry"], text=True, timeout=3).strip()
        parts = out.split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1280, 720


def _inject_script_loop(win: webview.Window, inject_paths: list[str]) -> None:
    """Wait for document readiness then inject userscripts cleanly into DOM."""
    scripts: list[str] = []
    for path_str in inject_paths:
        p = Path(path_str)
        if p.is_file():
            scripts.append(p.read_text(encoding="utf-8", errors="replace"))
    if not scripts:
        return

    # Retry briefly until page finishes initial load and exposes body
    for _ in range(40):
        try:
            ready = win.evaluate_js("Boolean(document && document.body)")
            if ready:
                for code in scripts:
                    js_call = (
                        "(() => {"
                        "  const s = document.createElement('script');"
                        f"  s.textContent = {json.dumps(code)};"
                        "  (document.documentElement || document.body).appendChild(s);"
                        "  return true;"
                        "})()"
                    )
                    win.evaluate_js(js_call)
                return
        except Exception:
            pass
        time.sleep(0.25)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Whale Desktop pywebview app launcher.")
    parser.add_argument("url", help="Target URL or local file to load.")
    parser.add_argument("--title", default="Whale WebView", help="Window title.")
    parser.add_argument("--width", type=int, default=0, help="Window width (0 = match display).")
    parser.add_argument("--height", type=int, default=0, help="Window height (0 = match display).")
    parser.add_argument("--windowed", action="store_true", help="Show normal window decorations.")
    parser.add_argument("--inject", action="append", default=[], help="JS file path to inject on load.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Avoid DMA-BUF/compositor stalls under Xvfb and unlock multi-thread software GL cleanly
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    if not os.path.exists("/usr/lib/wsl/lib/libd3d12.so"):
        os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        os.environ.setdefault("LP_NUM_THREADS", "4")

    args = parse_args(argv if argv is not None else sys.argv[1:])
    disp_w, disp_h = _get_display_size()
    full_display = args.width <= 0 and args.height <= 0 and not args.windowed
    width = args.width if args.width > 0 else disp_w
    height = args.height if args.height > 0 else disp_h

    win = webview.create_window(
        args.title,
        args.url,
        x=0,
        y=0,
        width=width,
        height=height,
        frameless=not args.windowed,
        fullscreen=full_display,
        resizable=True,
    )
    webview.start(_inject_script_loop, (win, args.inject), gui="gtk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
