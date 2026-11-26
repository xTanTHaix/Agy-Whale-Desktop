#!/usr/bin/env python3
"""High-FPS X11 frame server, HTTP/WebSocket proxy, and persistent /ctl daemon on port 6080.

Serves `/frame.jpg`, `/frame.png`, and `/ws-stream` (4:4:4 TurboJPEG + lossless PNG settle),
routes `/ctl` automation commands directly in-process to `desktop_ctl` without `docker exec`
cold starts, and proxies remaining HTTP + WebSocket traffic to `websockify` on 127.0.0.1:6081.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import io
import json
import os
import struct
import sys
import threading
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

from PIL import Image

sys.path.insert(0, "/usr/local/bin")
import desktop_ctl  # noqa: E402

DISPLAY_NAME = os.environ.get("DISPLAY", ":1").encode("ascii")
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("NOVNC_PORT", "6080"))
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 6081
ALL_PLANES = 0xFFFFFFFF
ZPIXMAP = 2
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TARGET_INTERVAL_SEC = 0.0175
PNG_SETTLE_SEC = 0.25
TJPF_BGRX = 3
TJSAMP_444 = 0
TJSAMP_422 = 1
TJSAMP_420 = 2
SUBSAMP_MAP = {"444": TJSAMP_444, "422": TJSAMP_422, "420": TJSAMP_420}
TJFLAG_ACCURATE = 0
MAX_CTL_BODY_BYTES = 2_000_000


class _TurboJpeg:
    """SIMD libturbojpeg.so.0 wrapper for fast BGRX -> JPEG encoding."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lib = None
        self._handle = None
        try:
            lib = ctypes.CDLL("libturbojpeg.so.0")
            lib.tjInitCompress.restype = ctypes.c_void_p
            lib.tjCompress2.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
            ]
            lib.tjCompress2.restype = ctypes.c_int
            lib.tjFree.argtypes = [ctypes.c_void_p]
            lib.tjFree.restype = None
            handle = lib.tjInitCompress()
            if handle:
                self._lib = lib
                self._handle = handle
        except Exception:
            self._lib = None
            self._handle = None

    def encode_bgrx(
        self,
        raw_bytes: bytes,
        w: int,
        stride: int,
        h: int,
        quality: int,
        subsamp: int = TJSAMP_444,
    ) -> Optional[bytes]:
        if not self._lib or not self._handle:
            return None
        with self._lock:
            out_buf = ctypes.c_void_p()
            out_size = ctypes.c_ulong(0)
            rc = self._lib.tjCompress2(
                self._handle,
                ctypes.c_char_p(raw_bytes),
                w,
                stride,
                h,
                TJPF_BGRX,
                ctypes.byref(out_buf),
                ctypes.byref(out_size),
                subsamp,
                quality,
                TJFLAG_ACCURATE,
            )
            if rc != 0 or not out_buf.value or out_size.value == 0:
                if out_buf.value:
                    self._lib.tjFree(out_buf)
                return None
            try:
                return ctypes.string_at(out_buf.value, out_size.value)
            finally:
                self._lib.tjFree(out_buf)


class XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("c_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
    ]


XERROR_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _ignore_x11_error(_dpy, _err) -> int:
    return 0


class FrameCache:
    """Thread-safe X11 capture cache active only while clients poll or stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cap_lock = threading.Lock()
        self._last_request_ts = 0.0
        self._active_ws_clients = 0
        self._ws_lock = threading.Lock()
        self._last_jpg: bytes = b""
        self._last_ws_frame: bytes = b""
        self._prev_raw: bytes = b""
        self._last_change_ts = 0.0
        self._png_settled = False
        self._frame_seq: int = 0
        self._last_geom: str = "1280x720"
        self._last_frame_ts = 0.0
        self._quality = 93
        self._subsamp = TJSAMP_444
        self._cached_quality = 93
        self._cached_subsamp = TJSAMP_444
        self._tj = _TurboJpeg()

        self._libx11 = ctypes.CDLL("libX11.so.6")
        self._libx11.XInitThreads()
        self._err_cb = XERROR_CB(_ignore_x11_error)
        self._libx11.XSetErrorHandler(self._err_cb)
        self._libx11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._libx11.XOpenDisplay.restype = ctypes.c_void_p
        self._libx11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._libx11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._libx11.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWindowAttributes)]
        self._libx11.XGetWindowAttributes.restype = ctypes.c_int
        self._libx11.XGetImage.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
        ]
        self._libx11.XGetImage.restype = ctypes.POINTER(XImage)
        self._libx11.XDestroyImage.argtypes = [ctypes.POINTER(XImage)]
        self._libx11.XDestroyImage.restype = ctypes.c_int

        self._dpy = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _capture_bgrx(self) -> Optional[tuple[int, int, int, str, bytes]]:
        with self._cap_lock:
            if not self._dpy:
                self._dpy = self._libx11.XOpenDisplay(DISPLAY_NAME)
            if not self._dpy:
                return None

            root = self._libx11.XDefaultRootWindow(self._dpy)
            attrs = XWindowAttributes()
            if not self._libx11.XGetWindowAttributes(self._dpy, root, ctypes.byref(attrs)):
                return None
            w, h = int(attrs.width), int(attrs.height)
            if w <= 0 or h <= 0:
                return None

            ximg_ptr = self._libx11.XGetImage(self._dpy, root, 0, 0, w, h, ALL_PLANES, ZPIXMAP)
            if not ximg_ptr:
                return None
            try:
                ximg = ximg_ptr.contents
                stride = int(ximg.bytes_per_line)
                raw = ctypes.string_at(ximg.data, stride * h)
                return w, h, stride, f"{w}x{h}", raw
            finally:
                self._libx11.XDestroyImage(ximg_ptr)

    def add_ws_client(self, delta: int) -> None:
        with self._ws_lock:
            self._active_ws_clients = max(0, self._active_ws_clients + delta)

    def set_quality(self, quality: int, subsamp_name: str = "444") -> None:
        self._quality = max(60, min(98, quality))
        self._subsamp = SUBSAMP_MAP.get(subsamp_name, TJSAMP_444)

    def snapshot_seq(self) -> tuple[int, bytes, str]:
        with self._lock:
            return self._frame_seq, (self._last_ws_frame or self._last_jpg), self._last_geom

    def _encode_jpeg(self, w: int, h: int, stride: int, raw: bytes, quality: int, subsamp: int) -> bytes:
        jpg = self._tj.encode_bgrx(raw, w, stride, h, quality, subsamp)
        if jpg is not None:
            return jpg
        img = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", stride, 1)
        buf = io.BytesIO()
        pil_sub = 0 if subsamp == TJSAMP_444 else 1 if subsamp == TJSAMP_422 else 2
        img.save(buf, format="JPEG", quality=quality, subsampling=pil_sub)
        return buf.getvalue()

    def _encode_png(self, w: int, h: int, stride: int, raw: bytes) -> bytes:
        img = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", stride, 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=1)
        return buf.getvalue()

    def _loop(self) -> None:
        while True:
            now = time.monotonic()
            with self._ws_lock:
                ws_active = self._active_ws_clients > 0
            if not ws_active and (now - self._last_request_ts > 5.0):
                time.sleep(0.05)
                continue

            t0 = time.monotonic()
            captured = self._capture_bgrx()
            if captured is not None:
                w, h, stride, geom, raw = captured
                q = self._quality
                ss = self._subsamp
                if raw != self._prev_raw or not self._last_jpg:
                    jpg = self._encode_jpeg(w, h, stride, raw, q, ss)
                    self._prev_raw = raw
                    self._last_change_ts = time.monotonic()
                    self._png_settled = False
                    with self._lock:
                        self._last_jpg = jpg
                        self._last_ws_frame = jpg
                        self._cached_quality = q
                        self._cached_subsamp = ss
                        self._last_geom = geom
                        self._last_frame_ts = self._last_change_ts
                        self._frame_seq += 1
                elif ws_active and not self._png_settled and (time.monotonic() - self._last_change_ts) >= PNG_SETTLE_SEC:
                    png = self._encode_png(w, h, stride, raw)
                    self._png_settled = True
                    with self._lock:
                        self._last_ws_frame = png
                        self._last_geom = geom
                        self._last_frame_ts = time.monotonic()
                        self._frame_seq += 1
                else:
                    with self._lock:
                        self._last_frame_ts = time.monotonic()

            elapsed = time.monotonic() - t0
            sleep_for = max(0.001, TARGET_INTERVAL_SEC - elapsed)
            time.sleep(sleep_for)

    def get_jpeg(self, quality: int = 93, subsamp_name: str = "444") -> tuple[bytes, str]:
        self.set_quality(quality, subsamp_name)
        now = time.monotonic()
        self._last_request_ts = now
        with self._lock:
            if (
                self._last_jpg
                and (now - self._last_frame_ts) < 0.06
                and self._cached_quality == self._quality
                and self._cached_subsamp == self._subsamp
            ):
                return self._last_jpg, self._last_geom

        captured = self._capture_bgrx()
        if captured is not None:
            w, h, stride, geom, raw = captured
            q = self._quality
            ss = self._subsamp
            jpg = self._encode_jpeg(w, h, stride, raw, q, ss)
            self._prev_raw = raw
            self._last_change_ts = time.monotonic()
            self._png_settled = False
            with self._lock:
                self._last_jpg = jpg
                self._last_ws_frame = jpg
                self._cached_quality = q
                self._cached_subsamp = ss
                self._last_geom = geom
                self._last_frame_ts = self._last_change_ts
                self._frame_seq += 1
            return jpg, geom
        with self._lock:
            return self._last_jpg, self._last_geom

    def get_png(self) -> tuple[bytes, str]:
        self._last_request_ts = time.monotonic()
        captured = self._capture_bgrx()
        if captured is None:
            return b"", self._last_geom
        w, h, stride, geom, raw = captured
        return self._encode_png(w, h, stride, raw), geom


CACHE = FrameCache()
desktop_ctl.set_fast_frame_provider(CACHE.get_png)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _encode_ws_binary(payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x82, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x82, 126, n)
    else:
        header = struct.pack("!BBQ", 0x82, 127, n)
    return header + payload


async def _serve_ws_stream(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    headers_map: dict[str, str],
    qs: dict[str, list[str]],
) -> None:
    ws_key = headers_map.get("sec-websocket-key", "")
    if not ws_key:
        client_writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    accept = base64.b64encode(hashlib.sha1(ws_key.strip().encode("ascii") + WS_GUID).digest()).decode("ascii")
    handshake = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "Access-Control-Allow-Origin: *\r\n\r\n"
    ).encode("ascii")
    client_writer.write(handshake)
    await client_writer.drain()

    if qs.get("q") or qs.get("s"):
        try:
            q_val = int(qs["q"][0]) if qs.get("q") else 93
        except ValueError:
            q_val = 93
        s_val = qs.get("s", ["444"])[0]
        CACHE.set_quality(q_val, s_val)

    CACHE.add_ws_client(1)
    closed = asyncio.Event()

    async def _watch_close() -> None:
        try:
            while True:
                chunk = await client_reader.read(512)
                if not chunk or (chunk[0] & 0x0F) == 0x08:
                    break
        except Exception:
            pass
        finally:
            closed.set()

    reader_task = asyncio.create_task(_watch_close())
    last_seq = -1
    last_push_ts = 0.0
    try:
        while not closed.is_set() and not client_writer.is_closing():
            seq, frame_bytes, _geom = CACHE.snapshot_seq()
            now = time.monotonic()
            if not frame_bytes:
                frame_bytes, _geom = await asyncio.get_running_loop().run_in_executor(None, CACHE.get_jpeg, 93, "444")
                seq, _, _ = CACHE.snapshot_seq()
            if frame_bytes and (seq != last_seq or (now - last_push_ts) >= 0.2):
                client_writer.write(_encode_ws_binary(frame_bytes))
                await client_writer.drain()
                last_seq = seq
                last_push_ts = now
            await asyncio.sleep(0.006)
    except Exception:
        pass
    finally:
        reader_task.cancel()
        CACHE.add_ws_client(-1)
        try:
            client_writer.close()
        except Exception:
            pass


async def _send_json_response(
    client_writer: asyncio.StreamWriter, status_line: str, payload: dict[str, object]
) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status_line}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    client_writer.write(headers)
    client_writer.write(body)
    await client_writer.drain()
    client_writer.close()


async def _handle_ctl(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    headers_map: dict[str, str],
    leftover: bytes,
) -> None:
    # Block cross-site browser requests: /ctl accepts loopback Host calls with no foreign Origin.
    host_hdr = headers_map.get("host", "").split(":", 1)[0].lower()
    origin_hdr = headers_map.get("origin", "").lower()
    if origin_hdr or (host_hdr and host_hdr not in ("127.0.0.1", "localhost")):
        await _send_json_response(client_writer, "403 Forbidden", {"ok": False, "error": "Forbidden"})
        return

    try:
        content_len = int(headers_map.get("content-length", "0"))
    except ValueError:
        content_len = -1
    if content_len <= 0 or content_len > MAX_CTL_BODY_BYTES:
        await _send_json_response(client_writer, "400 Bad Request", {"ok": False, "error": "Invalid Content-Length"})
        return

    body_chunks = [leftover]
    received = len(leftover)
    try:
        while received < content_len:
            chunk = await asyncio.wait_for(client_reader.read(min(65536, content_len - received)), timeout=10.0)
            if not chunk:
                break
            body_chunks.append(chunk)
            received += len(chunk)
    except Exception:
        await _send_json_response(client_writer, "408 Request Timeout", {"ok": False, "error": "Incomplete body"})
        return

    raw_body = b"".join(body_chunks)[:content_len]
    try:
        req = json.loads(raw_body.decode("utf-8"))
        action = str(req.get("action", "")).strip()
        params = req.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
    except Exception as exc:
        await _send_json_response(client_writer, "400 Bad Request", {"ok": False, "error": str(exc)})
        return

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, desktop_ctl.dispatch, action, params)
    await _send_json_response(client_writer, "200 OK", result)


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        raw_head_and_rest = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=10.0)
    except Exception:
        client_writer.close()
        return

    sep_idx = raw_head_and_rest.index(b"\r\n\r\n") + 4
    raw_head = raw_head_and_rest[:sep_idx]
    leftover = raw_head_and_rest[sep_idx:]

    lines = raw_head.split(b"\r\n")
    request_line = lines[0].decode("latin1", "replace")
    parts = request_line.split(" ", 2)
    if len(parts) < 2:
        client_writer.close()
        return

    headers_map: dict[str, str] = {}
    for raw_line in lines[1:]:
        if b":" in raw_line:
            k, v = raw_line.split(b":", 1)
            headers_map[k.decode("latin1", "replace").strip().lower()] = v.decode("latin1", "replace").strip()

    method, target = parts[0].upper(), parts[1]
    parsed = urlparse(target)

    if method == "POST" and parsed.path == "/ctl":
        await _handle_ctl(client_reader, client_writer, headers_map, leftover)
        return

    if method == "GET" and parsed.path == "/ws-stream":
        await _serve_ws_stream(client_reader, client_writer, headers_map, parse_qs(parsed.query))
        return

    if method in ("GET", "HEAD") and parsed.path in ("/frame.jpg", "/frame.png"):
        qs = parse_qs(parsed.query)
        loop = asyncio.get_running_loop()
        if parsed.path == "/frame.png":
            body, geom = await loop.run_in_executor(None, CACHE.get_png)
            mime = "image/png"
        else:
            q = int(qs.get("q", ["93"])[0]) if qs.get("q") else 93
            s = qs.get("s", ["444"])[0]
            body, geom = await loop.run_in_executor(None, CACHE.get_jpeg, q, s)
            mime = "image/jpeg"

        status = b"200 OK" if body else b"503 Service Unavailable"
        headers = (
            f"HTTP/1.1 {status.decode('ascii')}\r\n"
            f"Content-Type: {mime}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"X-Whale-Geometry: {geom}\r\n"
            "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Expose-Headers: X-Whale-Geometry\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        client_writer.write(headers)
        if method == "GET" and body:
            client_writer.write(body)
        await client_writer.drain()
        client_writer.close()
        return

    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except Exception:
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    upstream_writer.write(raw_head + leftover)
    await upstream_writer.drain()
    await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))


async def main() -> None:
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT, reuse_address=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
