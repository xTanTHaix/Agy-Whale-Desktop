---
name: whale-desktop
description: Operate the isolated Docker Linux virtual desktop (Xvfb + XFCE + noVNC) with screenshot perception, AT-SPI + Thai/English OCR UI inspection, smooth cursor, key-hold, batch macros, visible Whale CMD monitor window, and offline plugin-stored image backup/restore. Use when the user asks to open or operate GUI apps inside the isolated Docker desktop or view/save the container desktop image.
---

# Whale Desktop Operator

You have an isolated Linux graphical computer running inside Docker (`whale-desktop:v1`), exposed locally via **noVNC** (`http://127.0.0.1:16080/vnc.html?autoconnect=1&resize=off`), the embedded **Whale Screen** pane, and a live **Whale CMD Status HUD** window on Windows, using **`pywebview`** (`whale-webview` inside container & `host_webview.py` on Windows) without Chrome browser clutter.

## Recommended Human-Like GUI Workflow (Inspect -> Act -> Verify)

1. **Ensure Desktop is Running:** Call `desktop_start()` (pass `open_console=True` so the user's Whale CMD monitor pops up; optionally `open_browser=True` to open a native `pywebview` window).
2. **Understand Screen Semantically First:**
   - Prefer `desktop_inspect_ui()` when clicking menus, buttons, links, or Thai/English text — it combines Linux **AT-SPI2** accessible nodes + **Tesseract (`tha+eng`) OCR** into numbered **`#N` Set-of-Mark** bounding boxes and exact `(x, y)` centers.
   - Use `desktop_screenshot()` for pure visual pixel inspection.
3. **Act Efficiently Like a Human:**
   - `desktop_click_text(query)` — locate and smoothly click any visible button/label without guessing pixel coordinates.
   - `desktop_act(steps, return_screenshot=True)` — chain multiple actions in one turn (`click`, `click_text`, `move`, `drag`, `scroll`, `type`, `key`, `key_hold`, `wait`, `wait_stable`) and receive pixel-diff telemetry plus the settled screenshot.
   - `desktop_click` / `desktop_move` / `desktop_drag` — smooth ease-out cursor motion (`smooth=True` by default) so hover menus and Canvas drags trigger naturally.
   - `desktop_key_hold(key, duration_ms)` — hold keys cleanly (games, selection, charging).
   - `desktop_clipboard_get()` / `desktop_clipboard_set(text)` — read or write X11 clipboard text directly.
   - `desktop_wait_stable()` — wait until screen stops loading/animating instead of guessing fixed sleeps.
   - `desktop_open_app(command)` — launch GUI apps directly inside `:1` (e.g., `whale-webview https://example.com`, `xfce4-terminal`, `mousepad`, `thunar`).
4. **Persist Custom Software & Clean Shutdown:**
   - Call `desktop_save_image()` to commit running container software into `whale-desktop:v1` and export `images/whale-desktop-v1.tar.gz`.
   - Call `desktop_stop()` when finished.
