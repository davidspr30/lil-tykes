"""The archive folder: one folder per chat plus INDEX.md and index.csv at the top.

    archive/
      INDEX.md, index.csv, state-snapshot.sqlite3
      2026-09/2026-09-13_planning-a-trip_a1b2c3d4/
        transcript.md, conversation.json, files/, canvas/, history/, DELETED.txt

Every write goes to a temporary file first and is then renamed into place, so a
crash or a running rclone mirror never sees a half-written file. Nothing in
this module ever deletes archive content.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import tempfile
from datetime import datetime, tzinfo
from pathlib import Path

from .render import CanvasDoc, safe_filename, slugify

log = logging.getLogger(__name__)

KNOWN_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
}


def write_atomic(path: Path, data: bytes) -> None:
    """Write bytes to path so that readers see either the old file or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def write_text(path: Path, text: str) -> None:
    write_atomic(path, text.encode("utf-8"))


class Archive:
    def __init__(self, root: Path, tz: tzinfo):
        self.root = root
        self.tz = tz

    # --- folders --------------------------------------------------------------------------

    def folder_for(self, conversation_id: str, title: str, create_time: float, current_folder: str | None) -> str:
        """The chat's folder (relative to the archive root), renaming an existing one when the title changed."""
        created = datetime.fromtimestamp(create_time, self.tz)
        short_id = conversation_id.replace("-", "")[:8]
        wanted = f"{created:%Y-%m}/{created:%Y-%m-%d}_{slugify(title)}_{short_id}"
        if current_folder and current_folder != wanted:
            old_path = self.root / current_folder
            new_path = self.root / wanted
            if old_path.is_dir() and new_path.exists():
                return current_folder     # never merge two folders; keep the old name
            if old_path.is_dir():
                new_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.rename(new_path)
                log.info("renamed folder %s -> %s", current_folder, wanted)
        return wanted

    def path(self, folder: str, *parts: str) -> Path:
        return self.root.joinpath(folder, *parts)

    # --- per-chat files -------------------------------------------------------------------

    def read_conversation(self, folder: str) -> dict | None:
        path = self.path(folder, "conversation.json")
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("could not read %s", path)
            return None

    def write_conversation(self, folder: str, conversation: dict) -> None:
        """Save the raw JSON. If the new copy lost any message nodes, keep the old copy under history/."""
        path = self.path(folder, "conversation.json")
        old = self.read_conversation(folder)
        if old is not None:
            old_nodes = set((old.get("mapping") or {}).keys())
            new_nodes = set((conversation.get("mapping") or {}).keys())
            if old_nodes - new_nodes:
                self._keep_history(folder, old, path)
        data = json.dumps(conversation, indent=1, ensure_ascii=False).encode("utf-8")
        write_atomic(path, data)

    def _keep_history(self, folder: str, old: dict, path: Path) -> None:
        stamp = datetime.fromtimestamp(float(old.get("update_time") or path.stat().st_mtime), self.tz)
        history_dir = self.path(folder, "history")
        history_dir.mkdir(parents=True, exist_ok=True)
        target = history_dir / f"conversation.{stamp:%Y-%m-%dT%H-%M-%S}.json"
        counter = 2
        while target.exists():
            target = history_dir / f"conversation.{stamp:%Y-%m-%dT%H-%M-%S}-{counter}.json"
            counter += 1
        shutil.copy2(path, target)
        log.info("kept previous copy as %s (some messages disappeared from the new one)", target.name)

    def write_transcript(self, folder: str, text: str) -> None:
        write_text(self.path(folder, "transcript.md"), text)

    def write_file(self, folder: str, local_name: str, data: bytes) -> None:
        write_atomic(self.path(folder, "files", local_name), data)

    def local_name_for(self, file_id: str, kind: str, name: str | None, mime_type: str | None,
                       content_type: str | None) -> str:
        """Name inside files/: files with a known name keep it, generated images get an extension from the type."""
        if name:
            return f"{safe_filename(file_id)}_{safe_filename(name)}"
        extension = extension_for(content_type) or extension_for(mime_type) or ".bin"
        return f"{safe_filename(file_id)}{extension}"

    def write_canvas(self, folder: str, docs: list[CanvasDoc]) -> None:
        for doc in docs:
            write_text(self.path(folder, "canvas", doc.filename), doc.content)
            if not doc.replay_ok:
                warning = (f"The edits to this canvas could not all be replayed, so the text here may be out of date.\n"
                           f"The edit that failed: {doc.failed_pattern}\n"
                           f"The complete edit history is in conversation.json.\n")
                write_text(self.path(folder, "canvas", doc.filename + ".replay-warning.txt"), warning)

    def write_deleted_marker(self, folder: str, deleted_at: float) -> None:
        when = datetime.fromtimestamp(deleted_at, self.tz).strftime("%Y-%m-%d %H:%M %Z")
        write_text(self.path(folder, "DELETED.txt"),
                   f"This chat was deleted in ChatGPT (noticed {when}).\nThis backup is kept as it was.\n")

    def remove_deleted_marker(self, folder: str) -> None:
        marker = self.path(folder, "DELETED.txt")
        if marker.exists():
            marker.unlink()

    # --- top level ----------------------------------------------------------------------------

    def write_index(self, index_md: str, index_csv: str) -> None:
        write_text(self.root / "INDEX.md", index_md)
        write_text(self.root / "index.csv", index_csv)

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free


def extension_for(mime_type: str | None) -> str | None:
    if not mime_type:
        return None
    plain = mime_type.split(";", 1)[0].strip().lower()
    if plain in KNOWN_EXTENSIONS:
        return KNOWN_EXTENSIONS[plain]
    return mimetypes.guess_extension(plain)
