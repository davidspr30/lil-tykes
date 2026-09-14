#!/bin/sh
# Log in to ChatGPT again from any device on your Tailscale network, for example a
# phone connected over SSH, when there is no screen for tools/export_session.py.
#
# It opens ChatGPT in a browser on a virtual screen that you view in your phone's
# web browser. Once you are logged in, the login is saved to data/storage_state.json
# and the helper stops by itself. The service picks it up within 10 minutes, or
# right away after: docker compose restart
#
# Needs the service image to be built first (docker compose build).
set -eu

cd "$(dirname "$0")"
APP_DIR=$(cd ../.. && pwd)
IP=$(tailscale ip -4 | head -1)
PASSWORD=$(tr -dc 'a-km-np-z2-9' </dev/urandom | head -c 8)

docker build -q -t chat-backup-login-helper . >/dev/null
docker rm -f chat-backup-login >/dev/null 2>&1 || true
# Only reachable on the Tailscale address. Runs as you, so the saved file stays yours.
docker run -d --rm --name chat-backup-login --init --ipc=host \
  -u "$(id -u):$(id -g)" -e HOME=/tmp -e VNC_PASSWORD="$PASSWORD" \
  -v "$APP_DIR/data:/data" \
  -p "$IP:6080:6080" \
  chat-backup-login-helper >/dev/null

echo "On your phone open:  http://$IP:6080/vnc.html"
echo "Tap Connect, password: $PASSWORD"
echo "Log in to ChatGPT. About 15 seconds after your chat list shows, it saves and stops."
echo "Progress: docker logs -f chat-backup-login"
