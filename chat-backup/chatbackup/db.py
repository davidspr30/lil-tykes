"""SQLite bookkeeping: which chats exist, which still need fetching, which files
we have, which chats ChatGPT deleted, plus a few state values (alert states,
last sweep). The archive folder holds the content; this file only holds the
plan for keeping it up to date, so losing it costs nothing but a re-scan.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .chatgpt import ListItem
from .render import FilePointer

UPDATE_TOLERANCE = 1.0      # seconds; list and detail timestamps differ slightly in precision
MAX_FILE_ATTEMPTS = 3
RETRY_DELAY = 600           # seconds per failed fetch attempt
DAILY_RETRY = 86400         # after repeated failures, try once a day

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id                  TEXT PRIMARY KEY,
  title               TEXT NOT NULL DEFAULT '',
  create_time         REAL NOT NULL,
  update_time         REAL NOT NULL,          -- newest value seen in any list
  fetched_update_time REAL,                   -- update_time of the copy on disk; NULL = never fetched
  is_archived         INTEGER NOT NULL DEFAULT 0,
  gizmo_id            TEXT,                   -- project id, or NULL
  folder              TEXT,                   -- relative path inside archive/, NULL until first write
  model               TEXT,
  message_count       INTEGER,
  node_count          INTEGER,
  pending             INTEGER NOT NULL DEFAULT 1,
  not_before          REAL,                   -- do not fetch before this time (retry backoff)
  fetch_failures      INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,
  first_seen_at       REAL NOT NULL,
  last_seen_at        REAL NOT NULL,          -- last time any list contained it
  last_fetched_at     REAL,
  deleted_at          REAL                    -- NULL = still present in ChatGPT
);
CREATE INDEX IF NOT EXISTS conversations_pending ON conversations (pending, update_time);

CREATE TABLE IF NOT EXISTS projects (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL DEFAULT '',
  last_seen_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  conversation_id TEXT NOT NULL,
  file_id         TEXT NOT NULL,
  pointer         TEXT NOT NULL,
  kind            TEXT NOT NULL,              -- image or attachment
  name            TEXT,
  mime_type       TEXT,
  size            INTEGER,
  local_name      TEXT,                       -- name inside the chat's files/ folder once downloaded
  status          TEXT NOT NULL,              -- pending, done, failed or gone
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  downloaded_at   REAL,
  PRIMARY KEY (conversation_id, file_id)
);

CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@dataclass
class ConversationRow:
    id: str
    title: str
    create_time: float
    update_time: float
    fetched_update_time: float | None
    is_archived: bool
    gizmo_id: str | None
    folder: str | None
    model: str | None
    message_count: int | None
    node_count: int | None
    pending: bool
    not_before: float | None
    fetch_failures: int
    last_error: str | None
    first_seen_at: float
    last_seen_at: float
    last_fetched_at: float | None
    deleted_at: float | None


@dataclass
class FileRow:
    conversation_id: str
    file_id: str
    pointer: str
    kind: str
    name: str | None
    mime_type: str | None
    size: int | None
    local_name: str | None
    status: str
    attempts: int
    last_error: str | None
    downloaded_at: float | None


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit: every statement is saved immediately, which is what we want here.
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- conversations -------------------------------------------------------------

    def get(self, conversation_id: str) -> ConversationRow | None:
        row = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return _conversation(row) if row else None

    def all_conversations(self) -> list[ConversationRow]:
        rows = self.conn.execute("SELECT * FROM conversations ORDER BY update_time DESC")
        return [_conversation(row) for row in rows]

    def upsert_listed(self, item: ListItem, seen_at: float) -> str:
        """Record that a list contained this chat. Returns new, changed, reappeared or same."""
        row = self.get(item.id)
        if row is None:
            self.conn.execute(
                "INSERT INTO conversations (id, title, create_time, update_time, is_archived, gizmo_id,"
                " pending, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (item.id, item.title, item.create_time, item.update_time, int(item.is_archived),
                 item.gizmo_id, seen_at, seen_at))
            return "new"

        needs_fetch = (
            row.fetched_update_time is None
            or item.update_time > row.fetched_update_time + UPDATE_TOLERANCE
            or item.title != row.title
            or item.is_archived != row.is_archived
            or item.gizmo_id != row.gizmo_id
            or row.deleted_at is not None
        )
        self.conn.execute(
            "UPDATE conversations SET title = ?, update_time = MAX(update_time, ?), is_archived = ?,"
            " gizmo_id = ?, last_seen_at = ?, pending = ?, deleted_at = NULL WHERE id = ?",
            (item.title, item.update_time, int(item.is_archived), item.gizmo_id, seen_at,
             1 if (needs_fetch or row.pending) else 0, item.id))
        if row.deleted_at is not None:
            return "reappeared"
        return "changed" if needs_fetch else "same"

    def pending_conversations(self, limit: int | None, now: float) -> list[ConversationRow]:
        """Chats that need (re)fetching, newest first, skipping ones in retry backoff."""
        sql = ("SELECT * FROM conversations WHERE pending = 1 AND deleted_at IS NULL"
               " AND (not_before IS NULL OR not_before <= ?) ORDER BY update_time DESC")
        params: list = [now]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_conversation(row) for row in self.conn.execute(sql, params)]

    def mark_fetched(self, conversation_id: str, *, title: str, fetched_update_time: float, folder: str,
                     model: str | None, message_count: int, node_count: int, now: float,
                     requeue_after: float | None = None) -> None:
        """The copy on disk is current. requeue_after schedules another fetch (answer still streaming)."""
        self.conn.execute(
            "UPDATE conversations SET title = ?, fetched_update_time = ?, update_time = MAX(update_time, ?),"
            " folder = ?, model = ?, message_count = ?, node_count = ?, last_fetched_at = ?,"
            " fetch_failures = 0, last_error = NULL, pending = ?, not_before = ? WHERE id = ?",
            (title, fetched_update_time, fetched_update_time, folder, model, message_count, node_count, now,
             1 if requeue_after else 0, requeue_after, conversation_id))

    def mark_fetch_failed(self, conversation_id: str, error: str, now: float) -> None:
        """Keep the chat pending but wait longer before each retry; after three failures, once a day."""
        row = self.get(conversation_id)
        failures = (row.fetch_failures if row else 0) + 1
        delay = DAILY_RETRY if failures >= 3 else RETRY_DELAY * failures
        self.conn.execute(
            "UPDATE conversations SET fetch_failures = ?, last_error = ?, not_before = ? WHERE id = ?",
            (failures, error[:500], now + delay, conversation_id))

    def not_seen_since(self, since: float) -> list[ConversationRow]:
        """Chats that no list has mentioned since the given time: candidates for 'deleted'."""
        rows = self.conn.execute(
            "SELECT * FROM conversations WHERE deleted_at IS NULL AND last_seen_at < ?", (since,))
        return [_conversation(row) for row in rows]

    def mark_deleted(self, conversation_id: str, at: float) -> None:
        self.conn.execute(
            "UPDATE conversations SET deleted_at = ?, pending = 0, not_before = NULL WHERE id = ?",
            (at, conversation_id))

    # --- projects ------------------------------------------------------------------------

    def upsert_project(self, project_id: str, title: str, seen_at: float) -> None:
        self.conn.execute(
            "INSERT INTO projects (id, title, last_seen_at) VALUES (?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET title = excluded.title, last_seen_at = excluded.last_seen_at",
            (project_id, title, seen_at))

    def project_titles(self) -> dict[str, str]:
        return {row["id"]: row["title"] for row in self.conn.execute("SELECT id, title FROM projects")}

    def project_title(self, project_id: str | None) -> str | None:
        if not project_id:
            return None
        row = self.conn.execute("SELECT title FROM projects WHERE id = ?", (project_id,)).fetchone()
        return row["title"] if row else None

    # --- files ------------------------------------------------------------------------------

    def upsert_file(self, conversation_id: str, pointer: FilePointer) -> None:
        """Remember a file the chat refers to. Existing rows keep their status; missing details get filled in."""
        self.conn.execute(
            "INSERT OR IGNORE INTO files (conversation_id, file_id, pointer, kind, name, mime_type, size, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (conversation_id, pointer.file_id, pointer.pointer, pointer.kind, pointer.name,
             pointer.mime_type, pointer.size))
        self.conn.execute(
            "UPDATE files SET name = COALESCE(name, ?), mime_type = COALESCE(mime_type, ?), size = COALESCE(size, ?)"
            " WHERE conversation_id = ? AND file_id = ?",
            (pointer.name, pointer.mime_type, pointer.size, conversation_id, pointer.file_id))

    def files_for(self, conversation_id: str) -> list[FileRow]:
        rows = self.conn.execute("SELECT * FROM files WHERE conversation_id = ?", (conversation_id,))
        return [_file(row) for row in rows]

    def files_to_download(self, conversation_id: str) -> list[FileRow]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE conversation_id = ? AND"
            " (status = 'pending' OR (status = 'failed' AND attempts < ?))",
            (conversation_id, MAX_FILE_ATTEMPTS))
        return [_file(row) for row in rows]

    def mark_file_done(self, conversation_id: str, file_id: str, local_name: str, now: float) -> None:
        self.conn.execute(
            "UPDATE files SET status = 'done', local_name = ?, downloaded_at = ?, last_error = NULL"
            " WHERE conversation_id = ? AND file_id = ?",
            (local_name, now, conversation_id, file_id))

    def mark_file_failed(self, conversation_id: str, file_id: str, error: str, permanent: bool) -> None:
        """A failed download is retried a few times; a permanent failure (or the last retry) marks it gone."""
        row = self.conn.execute(
            "SELECT attempts FROM files WHERE conversation_id = ? AND file_id = ?",
            (conversation_id, file_id)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        status = "gone" if permanent or attempts >= MAX_FILE_ATTEMPTS else "failed"
        self.conn.execute(
            "UPDATE files SET status = ?, attempts = ?, last_error = ? WHERE conversation_id = ? AND file_id = ?",
            (status, attempts, error[:500], conversation_id, file_id))

    # --- state and stats ------------------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))

    def stats_since(self, since: float) -> dict[str, int]:
        def count(sql: str, *params) -> int:
            return int(self.conn.execute(sql, params).fetchone()[0])

        return {
            "total": count("SELECT COUNT(*) FROM conversations"),
            "deleted_total": count("SELECT COUNT(*) FROM conversations WHERE deleted_at IS NOT NULL"),
            "new": count("SELECT COUNT(*) FROM conversations WHERE first_seen_at >= ?", since),
            "fetched": count("SELECT COUNT(*) FROM conversations WHERE last_fetched_at >= ?", since),
            "deleted": count("SELECT COUNT(*) FROM conversations WHERE deleted_at >= ?", since),
            "files": count("SELECT COUNT(*) FROM files WHERE downloaded_at >= ?", since),
            "pending": count("SELECT COUNT(*) FROM conversations WHERE pending = 1 AND deleted_at IS NULL"),
            "failing": count("SELECT COUNT(*) FROM conversations WHERE fetch_failures > 0 AND deleted_at IS NULL"),
        }

    def snapshot_to(self, path: Path) -> None:
        """Write a consistent copy of the database (safe to mirror, unlike the live file)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".sqlite3")
        os.close(handle)
        try:
            backup = sqlite3.connect(temp_path)
            try:
                self.conn.backup(backup)
            finally:
                backup.close()
            os.replace(temp_path, path)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise


def _conversation(row: sqlite3.Row) -> ConversationRow:
    values = {key: row[key] for key in row.keys()}
    values["is_archived"] = bool(values["is_archived"])
    values["pending"] = bool(values["pending"])
    return ConversationRow(**values)


def _file(row: sqlite3.Row) -> FileRow:
    return FileRow(**{key: row[key] for key in row.keys()})
