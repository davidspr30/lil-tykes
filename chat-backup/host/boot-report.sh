#!/bin/sh
# Tells the phone how the previous run of this machine ended. Run once per boot by
# chat-backup-boot-report.service. Without it a crash or power cut leaves no trace on the phone:
# the machine just comes back (or stays off) and nobody knows it was down.
# The previous boot's journal ends with "Journal stopped" only after a clean shutdown or restart.
# Reading the system journal needs a user in the adm or systemd-journal group.
set -u

APP_DIR="${APP_DIR:?set APP_DIR to the chat-backup folder}"
if [ -f "$APP_DIR/.env" ]; then
  set -a
  . "$APP_DIR/.env"
  set +a
fi
now=$(date +%s)

notify() {   # $1 title, $2 priority, $3 message
  [ -n "${NTFY_TOPIC:-}" ] || { echo "no NTFY_TOPIC in .env; would have sent: $1: $3"; return 0; }
  # The network may still be settling this soon after boot, so keep trying for about a minute.
  curl -fsS -m 10 --retry 6 --retry-delay 10 --retry-all-errors -o /dev/null -H "Title: $1" -H "Priority: $2" \
    -d "$3" "${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC" || echo "could not send notification: $1"
}

duration() {   # $1 seconds, as "2 d 15 h 12 min", "10 h 22 min" or "4 min"
  days=$(( $1 / 86400 )); hours=$(( $1 % 86400 / 3600 )); minutes=$(( $1 % 3600 / 60 ))
  if [ "$days" -gt 0 ]; then echo "$days d $hours h $minutes min"
  elif [ "$hours" -gt 0 ]; then echo "$hours h $minutes min"
  else echo "$minutes min"
  fi
}

last_entry=$(journalctl -b -1 -n 1 -o short-unix --no-pager -q 2>/dev/null | awk '{print int($1)}')
if [ -z "$last_entry" ]; then
  echo "no previous boot in the journal; nothing to report"
  exit 0
fi
booted_at=$(( now - $(cut -d. -f1 /proc/uptime) ))
went_down=$(date -d "@$last_entry" '+%a %b %d %H:%M')
came_up=$(date -d "@$booted_at" '+%a %b %d %H:%M')
down_for=$(duration $(( booted_at - last_entry )))

if journalctl -b -1 -u systemd-journald -o cat --no-pager -q 2>/dev/null | tail -n 3 | grep -q 'Journal stopped'; then
  notify "$(hostname) restarted" 2 "Clean restart: went down $went_down, back up $came_up (down $down_for)."
else
  notify "$(hostname) crashed or lost power" 5 "The last run ended without a shutdown at about $went_down. Back up $came_up, so it was down $down_for. Check that chat-backup recovered: docker compose ps; docker compose logs --tail 50"
fi
exit 0
