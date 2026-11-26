"""Standalone pywebview (Edge WebView2) window launcher for Whale Desktop on Windows.

Replaces external system browser (Chrome) launches with a lightweight GPU-accelerated
native WebView2 window scoped to the plugin's own `.venv`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import webview


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open a clean pywebview window on Windows.")
    parser.add_argument("url", help="Target URL to open.")
    parser.add_argument("--title", default="Whale Desktop — Live View", help="Window title.")
    parser.add_argument("--width", type=int, default=1280, help="Initial window width.")
    parser.add_argument("--height", type=int, default=740, help="Initial window height.")
    parser.add_argument("--frameless", action="store_true", help="Open borderless window.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    default_data = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "whale-desktop"
    data_dir = Path(os.environ.get("WHALE_DESKTOP_DATA") or default_data) / "webview_storage"
    data_dir.mkdir(parents=True, exist_ok=True)
    webview.create_window(
        args.title,
        args.url,
        width=max(360, args.width),
        height=max(240, args.height),
        frameless=args.frameless,
        resizable=True,
    )
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(data_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
