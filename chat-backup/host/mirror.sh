#!/bin/sh
# Mirrors the archive folder to Google Drive with rclone. Run by chat-backup.timer.
# Needs APP_DIR (the chat-backup folder) and an rclone remote called "gdrive" (see README).
set -eu

APP_DIR="${APP_DIR:?set APP_DIR to the chat-backup folder}"
REMOTE="${RCLONE_REMOTE:-gdrive:chatgpt-backup}"
ARCHIVE="$APP_DIR/data/archive"
STATE_DIR="$APP_DIR/host-state"
mkdir -p "$STATE_DIR"

if [ ! -f "$ARCHIVE/INDEX.md" ]; then
  echo "archive not ready yet ($ARCHIVE/INDEX.md is missing); skipping"
  exit 0
fi

# --max-delete and --backup-dir protect the Drive copy from a wiped or half-mounted local folder:
# a sync that would delete more than 50 files is refused, and anything replaced is moved to a
# dated trash folder instead of being destroyed. flock stops two mirrors from running at once.
flock -n "$STATE_DIR/mirror.lock" rclone sync "$ARCHIVE" "$REMOTE" \
  --track-renames \
  --max-delete 50 \
  --backup-dir "${REMOTE}-trash/$(date +%F)" \
  --tpslimit 10 --tpslimit-burst 10 \
  --fast-list \
  --exclude '.tmp-*'

date +%s > "$STATE_DIR/mirror-ok"
echo "mirror finished"
