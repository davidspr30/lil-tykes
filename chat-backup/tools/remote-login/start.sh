#!/bin/sh
# Virtual screen -> VNC (password protected, container-local) -> noVNC web viewer on :6080.
set -e

Xvfb :99 -screen 0 1024x1366x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:99
for _ in $(seq 1 50); do
  [ -e /tmp/.X11-unix/X99 ] && break
  sleep 0.1
done

x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null 2>&1
x11vnc -display :99 -rfbauth /tmp/vncpass -localhost -forever -shared -quiet -bg >/dev/null 2>&1
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

exec python /helper/remote_login.py
