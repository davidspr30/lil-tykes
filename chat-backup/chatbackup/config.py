"""Settings, read once from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Config:
    data_dir: Path      # everything the service writes lives under here
    timezone: str       # IANA name, e.g. America/New_York; used by the browser and for all timestamps
    locale: str         # browser language, e.g. en-US
    ntfy_topic: str     # empty means "log alerts instead of pushing them"
    ntfy_url: str
    log_level: str
    notify_new_chats: bool = False  # push a phone alert for every newly created chat
    download_days: int = 0          # only download chats created or used in the last N days; 0 means all history
    quiet_hours: tuple[int, int] | None = None  # (start hour, end hour) in local time: no requests to ChatGPT then

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from environment variables, failing early on bad values."""
    if env is None:
        env = os.environ

    timezone = env.get("TZ", "").strip()
    if not timezone:
        raise SystemExit("TZ is not set. Put your time zone in .env, for example TZ=America/New_York")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"TZ={timezone!r} is not a valid time zone name (expected something like America/New_York)")

    days_text = env.get("DOWNLOAD_DAYS", "").strip() or "0"
    try:
        download_days = int(days_text)
    except ValueError:
        raise SystemExit(f"DOWNLOAD_DAYS={days_text!r} is not a whole number of days (use 0 for your whole history)")

    quiet_hours = parse_quiet_hours(env.get("QUIET_HOURS", "").strip())

    return Config(
        data_dir=Path(env.get("DATA_DIR", "/data")),
        timezone=timezone,
        locale=env.get("LOCALE", "").strip() or "en-US",
        ntfy_topic=env.get("NTFY_TOPIC", "").strip(),
        ntfy_url=env.get("NTFY_URL", "").strip().rstrip("/") or "https://ntfy.sh",
        log_level=env.get("LOG_LEVEL", "").strip().upper() or "INFO",
        notify_new_chats=env.get("NOTIFY_NEW_CHATS", "").strip().lower() in ("1", "true", "yes", "on"),
        download_days=max(0, download_days),
        quiet_hours=quiet_hours,
    )


def parse_quiet_hours(text: str) -> tuple[int, int] | None:
    """'1-8' -> (1, 8): from 1:00 until 8:00. '22-6' crosses midnight. Empty means no quiet hours."""
    if not text:
        return None
    start_text, _, end_text = text.partition("-")
    try:
        start, end = int(start_text), int(end_text)
    except ValueError:
        raise SystemExit(f"QUIET_HOURS={text!r} should look like 1-8 (from 1:00 until 8:00), or be empty")
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        raise SystemExit(f"QUIET_HOURS={text!r} needs two different hours from 0 to 23, like 1-8")
    return start, end
