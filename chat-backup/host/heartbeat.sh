#!/bin/sh
# Tells an outside service "this machine is up". Run by chat-backup-heartbeat.timer every 5 minutes.
# When the pings stop (crash, power cut, no internet), that service alerts you. Nothing on this machine
# can do that job, because a machine that is down cannot send anything.
set -eu

APP_DIR="${APP_DIR:?set APP_DIR to the chat-backup folder}"
if [ -f "$APP_DIR/.env" ]; then
  set -a
  . "$APP_DIR/.env"
  set +a
fi

if [ -z "${HC_MACHINE_URL:-}" ]; then
  echo "no HC_MACHINE_URL in .env; nothing to ping"
  exit 0
fi
# The uptime line (how long it has been up, load averages) appears in the outside service's log.
curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "$(uptime)" "$HC_MACHINE_URL"
