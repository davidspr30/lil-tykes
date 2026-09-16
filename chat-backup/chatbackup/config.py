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

    return Config(
        data_dir=Path(env.get("DATA_DIR", "/data")),
        timezone=timezone,
        locale=env.get("LOCALE", "").strip() or "en-US",
        ntfy_topic=env.get("NTFY_TOPIC", "").strip(),
        ntfy_url=env.get("NTFY_URL", "").strip().rstrip("/") or "https://ntfy.sh",
        log_level=env.get("LOG_LEVEL", "").strip().upper() or "INFO",
        notify_new_chats=env.get("NOTIFY_NEW_CHATS", "").strip().lower() in ("1", "true", "yes", "on"),
        download_days=max(0, download_days),
    )
