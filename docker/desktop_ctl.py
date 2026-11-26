#!/usr/bin/env python3
"""In-container X11 desktop automation controller (CLI stdin + in-process daemon).

Provides human-like smooth cursor gliding, real key-hold, AT-SPI2 UI inspection,
Thai/English OCR target detection with Set-of-Mark annotation, two-way clipboard
access, multi-step macro batches, and clean screen capture.
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

def _ensure_dbus_env() -> None:
    """Propagate container XFCE session D-Bus address into os.environ so AT-SPI2 sees live apps."""
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    env_file = "/tmp/whale_dbus.env"
    if os.path.exists(env_file):
        try:
            for line in open(env_file, encoding="utf-8", errors="replace"):
                if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        os.environ["DBUS_SESSION_BUS_ADDRESS"] = val
                        BASE_ENV["DBUS_SESSION_BUS_ADDRESS"] = val
                        return
        except Exception:
            pass


_ensure_dbus_env()

try:
    import pyatspi  # type: ignore[import-not-found]
except Exception:
    pyatspi = None  # type: ignore[assignment]

DISPLAY = os.environ.get("DISPLAY", ":1")
BASE_ENV = {**os.environ, "DISPLAY": DISPLAY}
BUTTON_MAP = {"left": "1", "middle": "2", "right": "3"}
_ACTION_LOCK = threading.Lock()
_FAST_FRAME_PROVIDER: Optional[Callable[[], tuple[bytes, str]]] = None

# Interactive AT-SPI roles kept compact so menus and dialogs stay crisp and scannable
INTERACTIVE_ROLES = {
    "push button",
    "toggle button",
    "check box",
    "radio button",
    "menu item",
    "check menu item",
    "radio menu item",
    "combo box",
    "page tab",
    "entry",
    "password text",
    "spin button",
    "slider",
    "link",
    "icon",
    "list item",
    "table cell",
    "tree item",
}


def set_fast_frame_provider(fn: Optional[Callable[[], tuple[bytes, str]]]) -> None:
    """Allow whale_stream_proxy's in-memory X11 cache to supply zero-copy PNG frames."""
    global _FAST_FRAME_PROVIDER
    _FAST_FRAME_PROVIDER = fn


def run_cmd(
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[bytes]:
    """Execute a command against the virtual X11 display."""
    return subprocess.run(
        args,
        input=input_bytes,
        env=BASE_ENV,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _get_display_and_mouse() -> tuple[str, int | None, int | None]:
    """Read current X11 display WxH and pointer coordinates."""
    geom_raw = run_cmd(["xdotool", "getdisplaygeometry"]).stdout.decode("utf-8", "replace").strip().split()
    geom = "x".join(geom_raw) if len(geom_raw) == 2 else "1280x720"
    loc = run_cmd(["xdotool", "getmouselocation", "--shell"]).stdout.decode("utf-8", "replace")
    mx, my = None, None
    for line in loc.splitlines():
        if line.startswith("X="):
            mx = int(line.split("=", 1)[1])
        elif line.startswith("Y="):
            my = int(line.split("=", 1)[1])
    return geom, mx, my


def _capture_png_bytes() -> tuple[bytes, str, int | None, int | None]:
    """Grab current screen as PNG bytes plus geometry and pointer coordinates."""
    geom, mx, my = _get_display_and_mouse()
    if _FAST_FRAME_PROVIDER is not None:
        try:
            raw_png, fast_geom = _FAST_FRAME_PROVIDER()
            if raw_png:
                return raw_png, (fast_geom or geom), mx, my
        except Exception:
            pass

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        res = run_cmd(["scrot", "-p", "-o", tmp_path], timeout=10.0)
        if res.returncode != 0:
            res = run_cmd(["scrot", "-o", tmp_path], timeout=10.0)
        if res.returncode != 0:
            raise RuntimeError(f"scrot failed: {res.stderr.decode('utf-8', 'replace')}")
        with open(tmp_path, "rb") as fh:
            raw = fh.read()
        return raw, geom, mx, my
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _glide_mouse(target_x: int, target_y: int, *, smooth: bool = True, duration_ms: int = 120) -> bool:
    """Move cursor smoothly along an ease-out trajectory (falls back to direct move when short)."""
    if not smooth:
        return run_cmd(["xdotool", "mousemove", str(target_x), str(target_y)]).returncode == 0

    _, cur_x, cur_y = _get_display_and_mouse()
    if cur_x is None or cur_y is None:
        return run_cmd(["xdotool", "mousemove", str(target_x), str(target_y)]).returncode == 0

    dx = target_x - cur_x
    dy = target_y - cur_y
    dist = (dx * dx + dy * dy) ** 0.5
    if dist < 12.0:
        res = run_cmd(["xdotool", "mousemove", str(target_x), str(target_y)])
        time.sleep(0.02)
        return res.returncode == 0

    steps = max(6, min(18, int(dist / 45.0) + 6))
    step_delay = max(0.005, min(0.025, (duration_ms / 1000.0) / steps))
    for i in range(1, steps + 1):
        t = i / steps
        eased = 1.0 - (1.0 - t) ** 3
        px = int(round(cur_x + dx * eased))
        py = int(round(cur_y + dy * eased))
        res = run_cmd(["xdotool", "mousemove", str(px), str(py)])
        if res.returncode != 0:
            return False
        if i < steps:
            time.sleep(step_delay)
    return True


def handle_screenshot(_params: dict[str, Any]) -> dict[str, Any]:
    """Capture the virtual desktop as PNG (with mouse pointer metadata)."""
    raw, geom, mx, my = _capture_png_bytes()
    return {
        "ok": True,
        "mime": "image/png",
        "bytes": len(raw),
        "geometry": geom,
        "mouse": {"x": mx, "y": my},
        "image_b64": base64.b64encode(raw).decode("ascii"),
    }


def handle_move(params: dict[str, Any]) -> dict[str, Any]:
    """Move cursor to exact (x, y) pixel coordinates (smooth ease-out by default)."""
    x = int(params["x"])
    y = int(params["y"])
    smooth = bool(params.get("smooth", True))
    duration_ms = max(30, min(int(params.get("duration_ms", 120)), 1500))
    ok = _glide_mouse(x, y, smooth=smooth, duration_ms=duration_ms)
    return {"ok": ok, "x": x, "y": y, "smooth": smooth}


def handle_click(params: dict[str, Any]) -> dict[str, Any]:
    """Move cursor to (x, y) smoothly and click button N times."""
    x = params.get("x")
    y = params.get("y")
    button_name = str(params.get("button", "left")).lower()
    button = BUTTON_MAP.get(button_name)
    if button is None:
        return {"ok": False, "error": f"Invalid button: {button_name}"}
    clicks = max(1, min(int(params.get("clicks", 1)), 4))
    smooth = bool(params.get("smooth", True))

    if x is not None and y is not None:
        if not _glide_mouse(int(x), int(y), smooth=smooth, duration_ms=110):
            return {"ok": False, "error": "Failed to move cursor"}
        time.sleep(0.03)

    res = run_cmd(["xdotool", "click", "--repeat", str(clicks), "--delay", "70", button])
    return {"ok": res.returncode == 0, "x": x, "y": y, "button": button_name, "clicks": clicks}


def handle_drag(params: dict[str, Any]) -> dict[str, Any]:
    """Press, smoothly drag across intermediate points, and release mouse button."""
    x1, y1 = int(params["x1"]), int(params["y1"])
    x2, y2 = int(params["x2"]), int(params["y2"])
    button = BUTTON_MAP.get(str(params.get("button", "left")).lower(), "1")
    smooth = bool(params.get("smooth", True))
    duration_ms = max(80, min(int(params.get("duration_ms", 260)), 2500))

    if not _glide_mouse(x1, y1, smooth=smooth, duration_ms=90):
        return {"ok": False, "error": "Failed to reach drag origin"}
    time.sleep(0.04)
    run_cmd(["xdotool", "mousedown", button])
    time.sleep(0.05)
    try:
        _glide_mouse(x2, y2, smooth=True, duration_ms=duration_ms)
        time.sleep(0.05)
    finally:
        res = run_cmd(["xdotool", "mouseup", button])
    return {"ok": res.returncode == 0, "from": [x1, y1], "to": [x2, y2]}


def handle_scroll(params: dict[str, Any]) -> dict[str, Any]:
    """Scroll wheel up/down/left/right at optional (x, y)."""
    x = params.get("x")
    y = params.get("y")
    if x is not None and y is not None:
        _glide_mouse(int(x), int(y), smooth=True, duration_ms=80)
        time.sleep(0.03)
    direction = str(params.get("direction", "down")).lower()
    dir_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
    btn = dir_map.get(direction)
    if btn is None:
        return {"ok": False, "error": f"Invalid scroll direction: {direction}"}
    clicks = max(1, min(int(params.get("clicks", 3)), 30))
    res = run_cmd(["xdotool", "click", "--repeat", str(clicks), "--delay", "35", btn])
    return {"ok": res.returncode == 0, "direction": direction, "clicks": clicks}


def _set_clipboard(text: str) -> bool:
    """Write UTF-8 text into the X11 clipboard and reap xclip cleanly."""
    proc = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-in"],
        stdin=subprocess.PIPE,
        env=BASE_ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin is not None
    proc.stdin.write(text.encode("utf-8"))
    proc.stdin.close()
    try:
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        pass
    return True


def handle_type(params: dict[str, Any]) -> dict[str, Any]:
    """Type UTF-8 text reliably using xdotool for short ASCII or xclip+Ctrl+V for Unicode."""
    text = str(params.get("text", ""))
    enter = bool(params.get("enter", False))
    use_clipboard = any(ord(ch) > 127 for ch in text) or "\n" in text

    if use_clipboard:
        _set_clipboard(text)
        time.sleep(0.07)
        res = run_cmd(["xdotool", "key", "--clearmodifiers", "ctrl+v"])
        mode = "clipboard"
    else:
        res = run_cmd(["xdotool", "type", "--clearmodifiers", "--delay", "12", "--", text], timeout=30.0)
        mode = "xdotool"

    if enter and res.returncode == 0:
        time.sleep(0.05)
        run_cmd(["xdotool", "key", "--clearmodifiers", "Return"])

    return {"ok": res.returncode == 0, "mode": mode, "chars": len(text), "enter": enter}


def handle_key(params: dict[str, Any]) -> dict[str, Any]:
    """Press a key or hotkey combo (e.g. Return, ctrl+l, alt+F4)."""
    combo = str(params["combo"]).strip()
    if not combo:
        return {"ok": False, "error": "Empty key combo"}
    res = run_cmd(["xdotool", "key", "--clearmodifiers", combo])
    return {"ok": res.returncode == 0, "combo": combo, "stderr": res.stderr.decode("utf-8", "replace")}


def handle_key_hold(params: dict[str, Any]) -> dict[str, Any]:
    """Hold a key down for duration_ms milliseconds and guarantee release cleanly."""
    combo = str(params.get("key") or params.get("combo") or "").strip()
    if not combo:
        return {"ok": False, "error": "Empty key name"}
    duration_ms = max(30, min(int(params.get("duration_ms", 400)), 10000))
    res_down = run_cmd(["xdotool", "keydown", "--clearmodifiers", combo])
    if res_down.returncode != 0:
        return {"ok": False, "error": res_down.stderr.decode("utf-8", "replace") or "keydown failed"}
    try:
        time.sleep(duration_ms / 1000.0)
    finally:
        res_up = run_cmd(["xdotool", "keyup", combo])
    return {"ok": res_up.returncode == 0, "key": combo, "duration_ms": duration_ms}


def handle_clipboard_get(_params: dict[str, Any]) -> dict[str, Any]:
    """Read current UTF-8 text from the X11 clipboard (falling back to primary selection)."""
    res = run_cmd(["xclip", "-o", "-selection", "clipboard"], timeout=4.0)
    sel = "clipboard"
    if res.returncode != 0 or not res.stdout:
        res2 = run_cmd(["xclip", "-o", "-selection", "primary"], timeout=4.0)
        if res2.returncode == 0 and res2.stdout:
            res = res2
            sel = "primary"
    txt = res.stdout.decode("utf-8", "replace") if res.stdout else ""
    return {"ok": True, "selection": sel, "text": txt, "chars": len(txt)}


def handle_clipboard_set(params: dict[str, Any]) -> dict[str, Any]:
    """Set UTF-8 text into the X11 clipboard cleanly."""
    text = str(params.get("text", ""))
    _set_clipboard(text)
    return {"ok": True, "chars": len(text)}


def handle_windows(_params: dict[str, Any]) -> dict[str, Any]:
    """List open GUI windows with geometry plus active window ID."""
    res = run_cmd(["wmctrl", "-l", "-G"])
    lines = [line for line in res.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    active_title = ""
    act = run_cmd(["xdotool", "getactivewindow", "getwindowname"], timeout=3.0)
    if act.returncode == 0:
        active_title = act.stdout.decode("utf-8", "replace").strip()
    return {"ok": res.returncode == 0, "active_window": active_title, "windows": lines}


def handle_open_app(params: dict[str, Any]) -> dict[str, Any]:
    """Launch a graphical application detached on DISPLAY=:1."""
    cmd = str(params["command"]).strip()
    if not cmd:
        return {"ok": False, "error": "Empty command"}
    with open("/var/log/whale/apps.log", "ab") as log_f:
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", f"nohup {cmd} >/dev/null 2>&1 &"],
            env=BASE_ENV,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
        proc.wait(timeout=5.0)
    time.sleep(0.5)
    return {"ok": True, "launched": cmd}


def _collect_atspi_elements(screen_w: int, screen_h: int, max_nodes: int = 600) -> list[dict[str, Any]]:
    """Walk accessible UI tree via AT-SPI2, recursing past non-visible layout wrappers."""
    _ensure_dbus_env()
    if pyatspi is None:
        return []
    elements: list[dict[str, Any]] = []
    seen_rects: set[tuple[int, int, str]] = set()
    visited = 0

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal visited
        if depth > 22 or visited >= max_nodes or node is None:
            return
        visited += 1
        try:
            role_name = node.getRoleName()
            states = node.getState()
            showing = states.contains(pyatspi.STATE_VISIBLE) and states.contains(pyatspi.STATE_SHOWING)
        except Exception:
            return

        if showing and role_name in INTERACTIVE_ROLES:
            try:
                comp = node.queryComponent()
                ext = comp.getExtents(pyatspi.DESKTOP_COORDS)
                x, y, w, h = int(ext.x), int(ext.y), int(ext.width), int(ext.height)
                if w > 4 and h > 4 and 0 <= x < screen_w and 0 <= y < screen_h:
                    label = (node.name or "").strip()
                    if not label:
                        try:
                            ti = node.queryText()
                            label = ti.getText(0, min(80, ti.characterCount)).strip()
                        except Exception:
                            label = ""
                    if not label:
                        label = (node.description or "").strip()
                    if label:
                        cx = max(0, min(screen_w - 1, x + w // 2))
                        cy = max(0, min(screen_h - 1, y + h // 2))
                        dedup_key = (round(cx / 10), round(cy / 10), label.lower()[:40])
                        if dedup_key not in seen_rects:
                            seen_rects.add(dedup_key)
                            elements.append(
                                {
                                    "source": "atspi",
                                    "role": role_name,
                                    "text": label[:120],
                                    "bbox": [x, y, w, h],
                                    "center": [cx, cy],
                                }
                            )
            except Exception:
                pass

        try:
            child_count = min(80, int(node.childCount))
        except Exception:
            child_count = 0
        for idx in range(child_count):
            if visited >= max_nodes:
                break
            try:
                walk(node.getChildAtIndex(idx), depth + 1)
            except Exception:
                continue

    try:
        desktop = pyatspi.Registry.getDesktop(0)
        for app_idx in range(min(32, desktop.childCount)):
            try:
                walk(desktop.getChildAtIndex(app_idx), 0)
            except Exception:
                continue
    except Exception:
        pass
    return elements


def _is_thai_char(ch: str) -> bool:
    return "\u0E00" <= ch <= "\u0E7F"


def _join_ocr_tokens(words: list[tuple[int, int, int, int, str, float]]) -> str:
    """Join TSV words without inserting bogus spaces between adjacent Thai glyphs."""
    out = ""
    for _, _, _, _, token, _ in words:
        if not out:
            out = token
        elif _is_thai_char(out[-1]) and _is_thai_char(token[0]):
            out += token
        elif _is_thai_char(out[-1]) or _is_thai_char(token[0]):
            # Preserve space when transitioning between Thai and Latin/digits
            out += token if out[-1] in "\u0E31\u0E34\u0E35\u0E36\u0E37\u0E38\u0E39\u0E47\u0E48\u0E49\u0E4A\u0E4B\u0E4C" else (" " + token)
        else:
            out += " " + token
    return out.strip()


def _run_tesseract_tsv(img_obj: Image.Image, screen_w: int, screen_h: int) -> list[dict[str, Any]]:
    """Run Tesseract tha+eng TSV on an image variant and extract clickable line targets."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        img_obj.save(tmp_path, format="PNG")
        res = run_cmd(
            ["tesseract", tmp_path, "stdout", "-l", "tha+eng", "--psm", "11", "tsv"],
            timeout=12.0,
        )
        if res.returncode != 0:
            res = run_cmd(
                ["tesseract", tmp_path, "stdout", "-l", "eng", "--psm", "11", "tsv"],
                timeout=12.0,
            )
        if res.returncode != 0:
            return []
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    lines_map: dict[tuple[int, int, int], list[tuple[int, int, int, int, str, float]]] = {}
    tsv = res.stdout.decode("utf-8", "replace")
    for idx, row in enumerate(tsv.splitlines()):
        if idx == 0 or not row.strip():
            continue
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        try:
            blk, par, ln = int(cols[2]), int(cols[3]), int(cols[4])
            x, y, w, h = int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9])
            conf = float(cols[10])
            word = cols[11].strip()
        except ValueError:
            continue
        if conf < 25.0 or not word or w < 3 or h < 6 or w > screen_w or h > screen_h // 2:
            continue
        lines_map.setdefault((blk, par, ln), []).append((x, y, w, h, word, conf))

    results: list[dict[str, Any]] = []
    for words in lines_map.values():
        words.sort(key=lambda item: item[0])
        min_x = min(w[0] for w in words)
        min_y = min(w[1] for w in words)
        max_x = max(w[0] + w[2] for w in words)
        max_y = max(w[1] + w[3] for w in words)
        bw, bh = max_x - min_x, max_y - min_y
        text = _join_ocr_tokens(words)
        if len(text) < 2 or bw < 6 or bh < 6:
            continue
        cx = max(0, min(screen_w - 1, min_x + bw // 2))
        cy = max(0, min(screen_h - 1, min_y + bh // 2))
        avg_conf = round(sum(w[5] for w in words) / len(words), 1)
        results.append(
            {
                "source": "ocr",
                "role": "text",
                "text": text[:120],
                "conf": avg_conf,
                "bbox": [min_x, min_y, bw, bh],
                "center": [cx, cy],
            }
        )
    return results


def _collect_ocr_elements(raw_png: bytes, screen_w: int, screen_h: int) -> list[dict[str, Any]]:
    """Run light + dark-mode inverted Thai/English Tesseract OCR and merge results."""
    img = Image.open(io.BytesIO(raw_png)).convert("L")
    mean_luma = ImageStat.Stat(img).mean[0]

    passes: list[dict[str, Any]] = _run_tesseract_tsv(img, screen_w, screen_h)
    if mean_luma < 140.0 or len(passes) < 5:
        inv_img = ImageOps.invert(img)
        passes.extend(_run_tesseract_tsv(inv_img, screen_w, screen_h))

    deduped: list[dict[str, Any]] = []
    for cand in passes:
        cx, cy = cand["center"]
        c_txt = cand["text"].lower()
        duplicate = False
        for existing in deduped:
            ex, ey = existing["center"]
            if abs(cx - ex) <= 24 and abs(cy - ey) <= 18:
                if cand.get("conf", 0) <= existing.get("conf", 0) and (c_txt in existing["text"].lower() or len(cand["text"]) <= len(existing["text"])):
                    duplicate = True
                    break
        if not duplicate:
            deduped.append(cand)
    return deduped


def _merge_ui_elements(atspi_elems: list[dict[str, Any]], ocr_elems: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Combine AT-SPI semantic nodes and OCR targets without duplicate overlap."""
    merged: list[dict[str, Any]] = list(atspi_elems)
    for ocr_el in ocr_elems:
        ox, oy = ocr_el["center"]
        o_txt = ocr_el["text"].lower()
        duplicate = False
        for existing in merged:
            ex, ey = existing["center"]
            if abs(ox - ex) <= 22 and abs(oy - ey) <= 16:
                e_txt = existing["text"].lower()
                if o_txt in e_txt or e_txt in o_txt:
                    duplicate = True
                    break
        if not duplicate:
            merged.append(ocr_el)
        if len(merged) >= limit:
            break

    numbered: list[dict[str, Any]] = []
    for idx, elem in enumerate(merged[:limit], start=1):
        numbered.append({"id": idx, **elem})
    return numbered


def _annotate_png(raw_png: bytes, elements: list[dict[str, Any]]) -> bytes:
    """Draw crisp Set-of-Mark numbered badges over detected interactive/text boxes."""
    img = Image.open(io.BytesIO(raw_png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for elem in elements:
        idx = elem["id"]
        x, y, w, h = elem["bbox"]
        color = (56, 189, 248) if elem["source"] == "atspi" else (250, 204, 21)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        tag = f"#{idx}"
        tx = max(0, x)
        ty = max(0, y - 14) if y >= 14 else y + 1
        tw = len(tag) * 7 + 6
        draw.rectangle([tx, ty, tx + tw, ty + 13], fill=(15, 23, 42), outline=color, width=1)
        draw.text((tx + 3, ty + 1), tag, fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="PNG", compress_level=3)
    return out.getvalue()


def handle_inspect_ui(params: dict[str, Any]) -> dict[str, Any]:
    """Extract clickable AT-SPI UI tree + Thai/English OCR text targets with optional SoM overlay."""
    include_ocr = bool(params.get("include_ocr", True))
    annotate_image = bool(params.get("annotate_image", True))
    max_elements = max(10, min(int(params.get("max_elements", 70)), 150))

    raw_png, geom, mx, my = _capture_png_bytes()
    parts = geom.split("x")
    sw = int(parts[0]) if len(parts) == 2 else 1280
    sh = int(parts[1]) if len(parts) == 2 else 720

    atspi_elems = _collect_atspi_elements(sw, sh)
    ocr_elems = _collect_ocr_elements(raw_png, sw, sh) if include_ocr else []
    elements = _merge_ui_elements(atspi_elems, ocr_elems, max_elements)
    win_info = handle_windows({})

    out_png = _annotate_png(raw_png, elements) if annotate_image and elements else raw_png
    return {
        "ok": True,
        "geometry": geom,
        "mouse": {"x": mx, "y": my},
        "active_window": win_info.get("active_window", ""),
        "windows": win_info.get("windows", []),
        "atspi_count": len(atspi_elems),
        "ocr_count": len(ocr_elems),
        "elements": elements,
        "bytes": len(out_png),
        "image_b64": base64.b64encode(out_png).decode("ascii"),
    }


def _find_matching_element(elements: list[dict[str, Any]], query: str, match_index: int) -> Optional[dict[str, Any]]:
    """Rank text matches cleanly: exact -> prefix -> substring."""
    q = " ".join(query.strip().lower().split())
    if not q:
        return None
    exact: list[dict[str, Any]] = []
    prefix: list[dict[str, Any]] = []
    substr: list[dict[str, Any]] = []
    for el in elements:
        t = " ".join(str(el.get("text", "")).lower().split())
        if t == q:
            exact.append(el)
        elif t.startswith(q):
            prefix.append(el)
        elif q in t:
            substr.append(el)
    ranked = exact + prefix + substr
    if not ranked:
        return None
    idx = max(0, min(match_index, len(ranked) - 1))
    return ranked[idx]


def handle_click_text(params: dict[str, Any]) -> dict[str, Any]:
    """Find a visible button/label via AT-SPI or Thai/English OCR and click its center."""
    query = str(params.get("query", "")).strip()
    if not query:
        return {"ok": False, "error": "Empty query string"}
    button = str(params.get("button", "left")).lower()
    clicks = int(params.get("clicks", 1))
    match_index = int(params.get("match_index", 0))
    smooth = bool(params.get("smooth", True))

    raw_png, geom, _, _ = _capture_png_bytes()
    parts = geom.split("x")
    sw = int(parts[0]) if len(parts) == 2 else 1280
    sh = int(parts[1]) if len(parts) == 2 else 720

    atspi_elems = _collect_atspi_elements(sw, sh)
    target = _find_matching_element(atspi_elems, query, match_index)
    if target is None:
        ocr_elems = _collect_ocr_elements(raw_png, sw, sh)
        elements = _merge_ui_elements(atspi_elems, ocr_elems, 120)
        target = _find_matching_element(elements, query, match_index)
    else:
        elements = _merge_ui_elements(atspi_elems, [], 120)

    if target is None:
        sample = [el["text"] for el in elements[:12]]
        return {"ok": False, "error": f"No visible UI text matching '{query}'", "visible_sample": sample}

    cx, cy = target["center"]
    click_res = handle_click({"x": cx, "y": cy, "button": button, "clicks": clicks, "smooth": smooth})
    return {**click_res, "matched": target}


def _diff_summary(before_png: bytes, after_png: bytes) -> tuple[bool, float]:
    """Return whether visible pixels changed and changed area percentage (fast thumbnails)."""
    try:
        img_a = Image.open(io.BytesIO(before_png)).convert("RGB").resize((320, 180))
        img_b = Image.open(io.BytesIO(after_png)).convert("RGB").resize((320, 180))
        diff = ImageChops.difference(img_a, img_b)
        bbox = diff.getbbox()
        if bbox is None:
            return False, 0.0
        hist = diff.convert("L").histogram()
        changed_px = sum(hist[12:])
        total_px = 320 * 180
        pct = round((changed_px * 100.0) / total_px, 2)
        return changed_px > 4, pct
    except Exception:
        return True, 100.0


def handle_wait_stable(params: dict[str, Any]) -> dict[str, Any]:
    """Wait until display pixels remain unchanged for stable_ms (or timeout_sec expires)."""
    timeout_sec = max(0.3, min(float(params.get("timeout_sec", 4.0)), 15.0))
    stable_ms = max(120, min(int(params.get("stable_ms", 300)), 3000))
    include_screenshot = bool(params.get("return_screenshot", True))

    deadline = time.monotonic() + timeout_sec
    prev_png, geom, mx, my = _capture_png_bytes()
    stable_since = time.monotonic()

    while time.monotonic() < deadline:
        time.sleep(0.09)
        cur_png, geom, mx, my = _capture_png_bytes()
        if cur_png != prev_png:
            changed, _ = _diff_summary(prev_png, cur_png)
            prev_png = cur_png
            if changed:
                stable_since = time.monotonic()
                continue
        if (time.monotonic() - stable_since) * 1000.0 >= stable_ms:
            break

    res: dict[str, Any] = {
        "ok": True,
        "geometry": geom,
        "mouse": {"x": mx, "y": my},
        "settled": (time.monotonic() - stable_since) * 1000.0 >= stable_ms,
    }
    if include_screenshot:
        res["bytes"] = len(prev_png)
        res["image_b64"] = base64.b64encode(prev_png).decode("ascii")
    return res


def handle_act(params: dict[str, Any]) -> dict[str, Any]:
    """Execute a human-style batch of GUI steps and report visual diff + settled screenshot."""
    steps = params.get("steps")
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "steps must be a non-empty list"}
    if len(steps) > 40:
        return {"ok": False, "error": "Maximum 40 steps per desktop_act batch"}

    wait_after_ms = max(0, min(int(params.get("wait_after_ms", 200)), 5000))
    return_screenshot = bool(params.get("return_screenshot", True))

    before_png, _, _, _ = _capture_png_bytes()
    step_results: list[dict[str, Any]] = []

    op_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "move": handle_move,
        "click": handle_click,
        "click_text": handle_click_text,
        "drag": handle_drag,
        "scroll": handle_scroll,
        "type": handle_type,
        "key": handle_key,
        "key_hold": handle_key_hold,
        "open_app": handle_open_app,
        "clipboard_set": handle_clipboard_set,
    }

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            return {"ok": False, "error": f"Step {idx} is not an object", "completed": step_results}
        op = str(step.get("op", "")).strip()
        if op == "wait":
            ms = max(10, min(int(step.get("ms", 200)), 10000))
            time.sleep(ms / 1000.0)
            step_results.append({"step": idx, "op": "wait", "ok": True, "ms": ms})
            continue
        if op == "wait_stable":
            wres = handle_wait_stable({**step, "return_screenshot": False})
            step_results.append({"step": idx, "op": "wait_stable", "ok": wres.get("ok", True), "settled": wres.get("settled")})
            continue
        fn = op_handlers.get(op)
        if fn is None:
            return {"ok": False, "error": f"Unsupported op '{op}' at step {idx}", "completed": step_results}
        res = fn(step)
        step_results.append({"step": idx, "op": op, "ok": bool(res.get("ok")), "detail": {k: v for k, v in res.items() if k != "image_b64"}})
        if not res.get("ok"):
            return {"ok": False, "error": f"Step {idx} ({op}) failed: {res.get('error')}", "completed": step_results}

    if wait_after_ms > 0:
        time.sleep(wait_after_ms / 1000.0)

    after_png, geom, mx, my = _capture_png_bytes()
    changed, changed_pct = _diff_summary(before_png, after_png)
    win_info = handle_windows({})

    out: dict[str, Any] = {
        "ok": True,
        "steps_executed": len(step_results),
        "results": step_results,
        "screen_changed": changed,
        "changed_pct": changed_pct,
        "geometry": geom,
        "mouse": {"x": mx, "y": my},
        "active_window": win_info.get("active_window", ""),
    }
    if return_screenshot:
        out["bytes"] = len(after_png)
        out["image_b64"] = base64.b64encode(after_png).decode("ascii")
    return out


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "screenshot": handle_screenshot,
    "move": handle_move,
    "click": handle_click,
    "drag": handle_drag,
    "scroll": handle_scroll,
    "type": handle_type,
    "key": handle_key,
    "key_hold": handle_key_hold,
    "clipboard_get": handle_clipboard_get,
    "clipboard_set": handle_clipboard_set,
    "windows": handle_windows,
    "open_app": handle_open_app,
    "inspect_ui": handle_inspect_ui,
    "click_text": handle_click_text,
    "wait_stable": handle_wait_stable,
    "act": handle_act,
}


def dispatch(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Thread-safe entrypoint used both by standalone CLI mode and persistent /ctl HTTP daemon."""
    handler = HANDLERS.get(action.strip())
    if handler is None:
        return {"ok": False, "error": f"Unknown action: {action}"}
    with _ACTION_LOCK:
        return handler(params or {})


def main() -> int:
    raw = sys.stdin.read()
    req = json.loads(raw)
    result = dispatch(str(req.get("action", "")), req.get("params") or {})
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
