"""Phone alerts through ntfy (https://ntfy.sh).

Two rules keep the alerts useful:
- A problem is announced once when it starts and once when it ends, not on every check.
- Nothing here can crash the poller: sending is best-effort.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from datetime import datetime, tzinfo

from .db import Database

log = logging.getLogger(__name__)

PRIORITY_LOW, PRIORITY_DEFAULT, PRIORITY_HIGH = 2, 3, 4
PROBLEM_PRIORITY = {
    "login": PRIORITY_HIGH,
    "challenge": PRIORITY_HIGH,
    "api_errors": PRIORITY_DEFAULT,
    "rate_limited": PRIORITY_DEFAULT,
    "disk": PRIORITY_HIGH,
}
STARTUP_NOTICE_INTERVAL = 3600      # seconds; a crash loop must not spam the phone
DIGEST_HOUR = 9                     # local time


class Notifier:
    def __init__(self, url: str, topic: str, db: Database):
        self.url = url
        self.topic = topic
        self.db = db

    def send(self, title: str, message: str, priority: int = PRIORITY_DEFAULT, tags: str = "") -> bool:
        """Push one notification. Returns False when it could not be sent (and logs why)."""
        if not self.topic:
            log.info("alert (no NTFY_TOPIC set): %s: %s", title, message)
            return False
        # ntfy reads the title from an HTTP header, which must stay ASCII.
        headers = {
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": str(priority),
            "Content-Type": "text/plain; charset=utf-8",
        }
        if tags:
            headers["Tags"] = tags
        request = urllib.request.Request(f"{self.url}/{self.topic}", data=message.encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except (urllib.error.URLError, OSError, ValueError) as error:
            log.warning("could not send notification %r: %s", title, error)
            return False
        log.info("sent notification: %s", title)
        return True

    def set_problem(self, name: str, is_problem: bool, message: str) -> None:
        """Announce a problem when it appears and when it clears; stay quiet in between."""
        key = f"alert:{name}"
        current = self.db.get_state(key, "ok")
        new = "problem" if is_problem else "ok"
        if new == current:
            return
        self.db.set_state(key, new)
        if is_problem:
            log.warning("problem: %s: %s", name, message)
            self.send(f"chat-backup: {name}", message, PROBLEM_PRIORITY.get(name, PRIORITY_DEFAULT), "warning")
        else:
            log.info("resolved: %s: %s", name, message)
            self.send(f"chat-backup: {name} ok", message, PRIORITY_LOW, "white_check_mark")

    def startup_notice(self, now: float) -> None:
        last = self.db.get_state("last_startup_notice")
        if last is not None and now - float(last) < STARTUP_NOTICE_INTERVAL:
            return
        self.db.set_state("last_startup_notice", str(now))
        self.send("chat-backup started", "The poller is running.", PRIORITY_LOW, "rocket")

    def digest_due(self, now: float, tz: tzinfo) -> bool:
        local = datetime.fromtimestamp(now, tz)
        return local.hour >= DIGEST_HOUR and self.db.get_state("last_digest_date") != local.date().isoformat()

    def send_digest(self, stats: dict, now: float, tz: tzinfo) -> None:
        """The daily "still alive" summary. Its absence is the signal that the whole chain is broken."""
        local = datetime.fromtimestamp(now, tz)
        lines = [
            f"Last 24 h: {stats.get('new', 0)} new chats, {stats.get('fetched', 0)} updated, "
            f"{stats.get('deleted', 0)} deleted in ChatGPT (kept), {stats.get('files', 0)} files downloaded.",
            f"Archive: {stats.get('total', 0)} chats, {stats.get('deleted_total', 0)} deleted in ChatGPT.",
            f"Waiting to fetch: {stats.get('pending', 0)}. Failing: {stats.get('failing', 0)}.",
        ]
        if stats.get("last_sweep"):
            lines.append(f"Last full check: {stats['last_sweep']}.")
        self.db.set_state("last_digest_date", local.date().isoformat())
        self.send("chat-backup daily summary", "\n".join(lines), PRIORITY_LOW, "page_facing_up")
