FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:1 \
    SCREEN_WIDTH=1280 \
    SCREEN_HEIGHT=720 \
    SCREEN_DEPTH=24 \
    HOME=/home/whale \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NO_AT_BRIDGE=0 \
    GTK_MODULES=gail:atk-bridge \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    at-spi2-core \
    ca-certificates \
    curl \
    dbus-x11 \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-thai-tlwg \
    gir1.2-atspi-2.0 \
    gir1.2-gtk-3.0 \
    gir1.2-webkit2-4.1 \
    libegl-mesa0 \
    libgl1-mesa-dri \
    libgles2 \
    libturbojpeg0 \
    mousepad \
    novnc \
    procps \
    python3 \
    python3-gi \
    python3-gi-cairo \
    python3-pil \
    python3-pip \
    python3-pyatspi \
    python3-venv \
    scrot \
    supervisor \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-tha \
    thunar \
    wmctrl \
    x11vnc \
    xclip \
    xdotool \
    xfce4 \
    xfce4-terminal \
    xvfb \
    websockify \
    && python3 -m venv --system-site-packages /opt/whale-webview \
    && /opt/whale-webview/bin/pip install --no-cache-dir pywebview \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root desktop user
RUN useradd -m -s /bin/bash whale \
    && mkdir -p /home/whale/Desktop /home/whale/workspace /var/log/whale \
    && chown -R whale:whale /home/whale /var/log/whale

# Configure pywebview desktop browser wrapper and bind noVNC root cleanly
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
    && printf '#!/bin/sh\nexec /usr/local/bin/whale-webview "$@"\n' > /usr/local/bin/chromium-desktop \
    && chmod +x /usr/local/bin/chromium-desktop

# Desktop shortcut for Whale WebView and Terminal
RUN printf '[Desktop Entry]\nVersion=1.0\nType=Application\nName=Whale WebView\nExec=/usr/local/bin/whale-webview %%U\nIcon=web-browser\nTerminal=false\nCategories=Network;WebBrowser;\n' > /home/whale/Desktop/webview.desktop \
    && printf '[Desktop Entry]\nVersion=1.0\nType=Application\nName=Terminal\nExec=xfce4-terminal\nIcon=utilities-terminal\nTerminal=false\nCategories=System;TerminalEmulator;\n' > /home/whale/Desktop/terminal.desktop \
    && chmod +x /home/whale/Desktop/webview.desktop /home/whale/Desktop/terminal.desktop \
    && chown -R whale:whale /home/whale/Desktop

COPY desktop_ctl.py /usr/local/bin/desktop_ctl.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY whale_webview.py /usr/local/bin/whale-webview
COPY whale_stream_proxy.py /usr/local/bin/whale_stream_proxy.py
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh /usr/local/bin/desktop_ctl.py /usr/local/bin/whale-webview /usr/local/bin/whale_stream_proxy.py \
    && chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/desktop_ctl.py /usr/local/bin/whale-webview /usr/local/bin/whale_stream_proxy.py

EXPOSE 6080 5900

USER whale
WORKDIR /home/whale/workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
