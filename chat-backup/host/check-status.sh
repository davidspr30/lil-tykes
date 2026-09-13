#!/bin/sh
# Sends one phone notification when the poller stops beating or the mirror stops
# succeeding, and one more when it recovers. Run by chat-backup.timer every 15 minutes.
# This runs outside Docker on purpose: a dead container cannot report itself.
set -u

APP_DIR="${APP_DIR:?set APP_DIR to the chat-backup folder}"
STATE_DIR="$APP_DIR/host-state"
mkdir -p "$STATE_DIR"
if [ -f "$APP_DIR/.env" ]; then
  # .env holds KEY=value lines (no spaces around the =), so the shell can read it directly.
  set -a
  . "$APP_DIR/.env"
  set +a
fi
now=$(date +%s)

age_of() {   # seconds since the time stored in file $1; a huge number when the file is missing
  if [ -f "$1" ]; then echo $(( now - $(cat "$1") )); else echo 9999999; fi
}

notify() {   # $1 title, $2 priority, $3 message
  [ -n "${NTFY_TOPIC:-}" ] || { echo "no NTFY_TOPIC in .env; would have sent: $1"; return 0; }
  curl -fsS -o /dev/null -H "Title: $1" -H "Priority: $2" -d "$3" "${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC" \
    || echo "could not send notification: $1"
}

report() {   # $1 name, $2 new state (ok or down), $3 message to send when it goes down
  state_file="$STATE_DIR/$1.state"
  previous=$(cat "$state_file" 2>/dev/null || echo ok)
  [ "$2" = "$previous" ] && return 0
  echo "$2" > "$state_file"
  if [ "$2" = down ]; then
    notify "chat-backup: $1 down" 4 "$3"
  else
    notify "chat-backup: $1 ok" 2 "$1 is working again"
  fi
}

if [ "$(age_of "$APP_DIR/data/.heartbeat")" -gt 600 ]; then state=down; else state=ok; fi
report poller "$state" "No heartbeat from the poller for over 10 minutes. On the backup machine run: docker compose ps; docker compose logs --tail 100"

# Only judge the mirror once it has succeeded at least once (so setup is not noisy).
if [ -f "$STATE_DIR/mirror-ok" ]; then
  if [ "$(age_of "$STATE_DIR/mirror-ok")" -gt 7200 ]; then state=down; else state=ok; fi
  report mirror "$state" "The Google Drive mirror has not succeeded for over 2 hours. Run: journalctl -u chat-backup --since -3h"
fi
exit 0
