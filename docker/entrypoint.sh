#!/bin/bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:1}"
export NO_AT_BRIDGE=0
export GTK_MODULES=gail:atk-bridge
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1

SCREEN_WIDTH="${SCREEN_WIDTH:-1280}"
SCREEN_HEIGHT="${SCREEN_HEIGHT:-720}"
SCREEN_DEPTH="${SCREEN_DEPTH:-24}"
MAX_WIDTH=$(( SCREEN_WIDTH > 1920 ? SCREEN_WIDTH : 1920 ))
MAX_HEIGHT=$(( SCREEN_HEIGHT > 1080 ? SCREEN_HEIGHT : 1080 ))
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"

mkdir -p /tmp/.X11-unix /var/log/whale "$HOME/.config/xfce4"

# Clean stale X11 lock files if container was restarted after crash
rm -f "/tmp/.X${DISPLAY#:}-lock" "/tmp/.X11-unix/X${DISPLAY#:}" || true

echo "[whale-desktop] Starting Xvfb on ${DISPLAY} (fb ${MAX_WIDTH}x${MAX_HEIGHT} -> target ${SCREEN_WIDTH}x${SCREEN_HEIGHT})..."
Xvfb "${DISPLAY}" -screen 0 "${MAX_WIDTH}x${MAX_HEIGHT}x${SCREEN_DEPTH}" -ac +extension RANDR >/var/log/whale/xvfb.log 2>&1 &
XVFB_PID=$!

for _ in $(seq 1 30); do
    if xdotool getdisplaygeometry >/dev/null 2>&1 || [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; then
        break
    fi
    sleep 0.2
done

echo "[whale-desktop] Starting shared D-Bus + XFCE4 desktop session..."
eval "$(dbus-launch --sh-syntax)"
export DBUS_SESSION_BUS_ADDRESS
printf 'DBUS_SESSION_BUS_ADDRESS=%s\n' "$DBUS_SESSION_BUS_ADDRESS" > /tmp/whale_dbus.env
startxfce4 >/var/log/whale/xfce4.log 2>&1 &
XFCE_PID=$!
sleep 2

if [ "${SCREEN_WIDTH}" != "${MAX_WIDTH}" ] || [ "${SCREEN_HEIGHT}" != "${MAX_HEIGHT}" ]; then
    MODE_NAME="${SCREEN_WIDTH}x${SCREEN_HEIGHT}"
    xrandr --newmode "${MODE_NAME}" 60.00 "${SCREEN_WIDTH}" $((SCREEN_WIDTH + 16)) $((SCREEN_WIDTH + 48)) $((SCREEN_WIDTH + 96)) "${SCREEN_HEIGHT}" $((SCREEN_HEIGHT + 3)) $((SCREEN_HEIGHT + 8)) $((SCREEN_HEIGHT + 18)) -hsync +vsync 2>/dev/null || true
    xrandr --addmode screen "${MODE_NAME}" 2>/dev/null || true
    xrandr --output screen --mode "${MODE_NAME}" 2>/dev/null || true
fi

# Enable accessibility bus + solid dark wallpaper + auto-hide top panel + disable idle blanking
xfconf-query -c xfce4-session -p /general/Accessibility -n -t bool -s true 2>/dev/null || true
xfconf-query -c xfce4-desktop -p /backdrop/screen0/monitor0/workspace0/color-style -n -t int -s 0 2>/dev/null || true
xfconf-query -c xfce4-panel -p /panels/panel-1/autohide-behavior -n -t int -s 2 2>/dev/null || true
xset s off 2>/dev/null || true
xset -dpms 2>/dev/null || true

echo "[whale-desktop] Starting x11vnc on port ${VNC_PORT} (XRandR-safe single-thread supervisor)..."
(
    while kill -0 "${XVFB_PID}" 2>/dev/null; do
        x11vnc -display "${DISPLAY}" -forever -shared -nopw -rfbport "${VNC_PORT}" -xkb -wait 10 -defer 10 -noxdamage -ncache 0 -xrandr resize >>/var/log/whale/x11vnc.log 2>&1 || true
        sleep 0.5
    done
) &
VNC_SUP_PID=$!

echo "[whale-desktop] Starting noVNC websockify on port 6081 + fast stream & control daemon on port ${NOVNC_PORT}..."
websockify --web=/usr/share/novnc 6081 "127.0.0.1:${VNC_PORT}" >/var/log/whale/novnc.log 2>&1 &
WS_PID=$!
python3 /usr/local/bin/whale_stream_proxy.py >/var/log/whale/stream.log 2>&1 &
NOVNC_PID=$!

echo "[whale-desktop] READY: noVNC at http://127.0.0.1:${NOVNC_PORT}/vnc.html?autoconnect=1&resize=off"

cleanup() {
    echo "[whale-desktop] Shutting down..."
    kill "${NOVNC_PID}" "${WS_PID}" "${VNC_SUP_PID}" "${XFCE_PID}" "${XVFB_PID}" 2>/dev/null || true
    pkill -TERM x11vnc 2>/dev/null || true
    wait || true
}
trap cleanup TERM INT

wait -n "${XVFB_PID}" "${WS_PID}" "${NOVNC_PID}"
