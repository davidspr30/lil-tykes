#!/bin/sh
# Starts a virtual display for the headed Chromium, then hands the process
# over to Python with "exec" so Docker's stop signal reaches Python directly.
set -e

# A crash or power cut leaves Xvfb's lock and socket behind, and Docker restarts keep /tmp,
# so Xvfb would refuse to start ("Server is already active"). Nothing else runs yet, so they are stale.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:99

# Give Xvfb a moment to create its socket before Chromium tries to use it.
for _ in $(seq 1 50); do
  [ -e /tmp/.X11-unix/X99 ] && break
  sleep 0.1
done

exec python -m chatbackup "$@"
