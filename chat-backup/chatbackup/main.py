"""The polling loop. Read this file top to bottom to see how the service behaves.

Quick check: look at the most recently updated chats and the Projects sidebar,
and fetch whatever is new or changed. Every 2 minutes while you are using
ChatGPT (a new or changed chat in the last 20 minutes), every 15 minutes otherwise.
Once a day, while you are not using ChatGPT (full check): list everything, so
chats that vanished can be marked as deleted, and save a snapshot of the bookkeeping.
During QUIET_HOURS: no requests at all.

ChatGPT's rate limit is shared with your own browser, so every request here is
one you cannot make yourself. Keep it that way: few, slow, and backing off hard.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .archive import Archive, write_atomic
from .browser import Browser, is_playwright_error
from .chatgpt import (ApiError, ListItem, clean_title, get_conversation, list_conversations,
                      list_project_conversations, list_projects, list_recent_conversations, parse_time,
                      resolve_download_url)
from .config import Config, load_config
from .db import ConversationRow, Database
from .notify import Notifier
from .render import (FileInfo, build_index_csv, build_index_md, build_transcript, extract_canvas_docs,
                     extract_file_pointers, format_time, is_streaming, main_path, message_counts, model_of,
                     node_ids)

log = logging.getLogger("chatbackup")

ACTIVE_POLL_SECONDS = 2 * 60        # quick check interval while you are using ChatGPT...
IDLE_POLL_SECONDS = 15 * 60         # ...and while you are not
ACTIVE_WINDOW_SECONDS = 20 * 60     # "using ChatGPT" = a quick check found a new or changed chat this recently
POLL_JITTER = 0.4                   # +/- 40 %, so the timing is not a metronome
SWEEP_SECONDS = 24 * 3600           # full check interval; quick checks cover every project, so this mostly marks deletions
SWEEP_RETRY_SECONDS = 3 * 3600      # a refused full check waits this long
FIRST_SWEEP_DELAY_SECONDS = 30 * 60 # after a start or a rest, only quick checks for a while
CALL_GAP_SECONDS = (1.0, 1.5)       # minimum pause between two API calls
SWEEP_CALL_GAP_SECONDS = (2.5, 3.5) # slower during a full check: about 55 requests at ~20 a minute
RECENT_CHAT_SECONDS = 2 * 3600      # chats active this recently are fetched first, and only chats created this recently alert
RECENT_PER_ITERATION = 3            # recently active chats fetched per cycle, even while older ones wait
BACKLOG_REQUEST_BUDGET = 8          # API requests per cycle for older chats (files count); well under ChatGPT's limit
FILE_REQUEST_BUDGET = 8             # API requests per cycle for files still waiting (about 2 per file)
INLINE_FILES_PER_CHAT = 4           # files downloaded together with a chat; any more wait for FILE_REQUEST_BUDGET
ONCE_PENDING_LIMIT = 8              # chats fetched by --once
RATE_LIMIT_PAUSE_SECONDS = 60 * 60  # ChatGPT said "too many requests": older chats wait this long
RATE_LIMIT_ALERT_SECONDS = 30 * 60  # ChatGPT has refused lists or downloads for this long: tell the phone
LIST_BACKOFF_SECONDS = (15 * 60, 60 * 60)  # a refused quick check waits 15, 30, then 60 minutes
REST_FILE = "rest-until"            # data/rest-until holds a Unix time; until then ChatGPT gets no requests at all
LAST_SUCCESS_FILE = ".last-success" # data/.last-success: when ChatGPT last answered a check; the host script watches it
RECENT_PROJECT_CHATS = 5            # chats per project the fast poll looks at
STREAMING_RECHECK_SECONDS = 120     # an answer was still being written: look again this much later
BROWSER_RESTART_SECONDS = 6 * 3600  # Chromium leaks memory; a routine restart keeps it in check
STALL_LIMIT_SECONDS = 15 * 60       # no progress for this long means the process is stuck
HEARTBEAT_SECONDS = 30
MAX_FILE_BYTES = 50 * 1024 * 1024
MIN_FREE_BYTES = 2 * 1024 ** 3
LOGIN_RETRY_SECONDS = 10 * 60
CHALLENGE_RETRY_SECONDS = 15 * 60
BLOCKED_RETRY_SECONDS = 60 * 60
MAX_BACKOFF_SECONDS = 30 * 60
PERMANENT_FILE_ERRORS = {"not_found", "invalid", "too_large"}
RETRYABLE_FILE_ERRORS = {"bad_body", "network", "server"}

LOGIN_HELP = ("The ChatGPT login has expired or was never imported. On your laptop run:\n"
              "    python tools/export_session.py\n"
              "then copy storage_state.json into chat-backup/data/ on this machine and try again.")


class Watchdog(threading.Thread):
    """Writes the heartbeat file while the main loop makes progress, and kills the process when it stops.

    Docker only restarts a container whose process exits, so exiting is how we recover from a hang.
    """

    def __init__(self, heartbeat_path: Path, stall_limit: float = STALL_LIMIT_SECONDS,
                 interval: float = HEARTBEAT_SECONDS):
        super().__init__(name="watchdog", daemon=True)
        self.heartbeat_path = heartbeat_path
        self.stall_limit = stall_limit
        self.interval = interval
        self._last_beat = time.monotonic()

    def beat(self) -> None:
        """Called by the main loop whenever it has done something."""
        self._last_beat = time.monotonic()

    def is_stalled(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) - self._last_beat > self.stall_limit

    def run(self) -> None:
        while True:
            if self.is_stalled():
                log.critical("no progress for %d minutes; exiting so Docker restarts the container",
                             self.stall_limit // 60)
                logging.shutdown()
                os._exit(1)
            try:
                write_atomic(self.heartbeat_path, str(int(time.time())).encode("ascii"))
            except OSError as error:
                log.warning("could not write the heartbeat file: %s", error)
            time.sleep(self.interval)


class Poller:
    def __init__(self, config: Config, db: Database, browser: Browser, archive: Archive,
                 notifier: Notifier, watchdog: Watchdog):
        self.config = config
        self.db = db
        self.browser = browser
        self.archive = archive
        self.notifier = notifier
        self.watchdog = watchdog
        self.tz = ZoneInfo(config.timezone)
        self.stop_requested = False
        self.login_dead = False
        self.index_dirty = False
        self.failures = 0
        self.challenge_failures = 0
        self.restart_failures = 0
        self.downloads_refused_since: float | None = None  # first refused download since the last one that worked
        self.lists_refused_since: float | None = None      # first refused quick check since the last one that worked
        self.list_refusals = 0                             # refused quick checks in a row, for the backoff
        self.after_rest = False                            # send a "resumed" note once ChatGPT answers after a rest
        self.last_activity = float("-inf")             # monotonic time a quick check last found a new or changed chat
        self.backlog_paused_until = 0.0                # monotonic time; older chats wait until then
        self.requests_made = 0                         # every API request and file download, for the request budget
        self.next_sweep = 0.0
        self.next_restart = 0.0
        self.call_gap = CALL_GAP_SECONDS
        self.sweep_call_gap = SWEEP_CALL_GAP_SECONDS
        self._last_call = 0.0

    # --- talking to ChatGPT -------------------------------------------------------------

    def api(self, path: str):
        """One API call with pacing. Retries once on an expired token; a rate limit goes to the caller, which backs off."""
        self._pace()
        try:
            return self.browser.api_get(path)
        except ApiError as error:
            if error.kind == "token_expired":
                log.info("token expired; reloading chatgpt.com to get a new one")
                self.browser.reload()
            else:
                raise
        self._pace()
        return self.browser.api_get(path)

    def _pace(self) -> None:
        wait = self._last_call + random.uniform(*self.call_gap) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        self.requests_made += 1

    def sleep(self, seconds: float) -> None:
        """Sleep in short slices so the watchdog sees progress and a stop request is noticed quickly."""
        end = time.monotonic() + seconds
        while not self.stop_requested:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, HEARTBEAT_SECONDS))
            self.watchdog.beat()

    # --- finding chats --------------------------------------------------------------------

    def fast_poll(self) -> None:
        """A few requests: the newest chats outside Projects, and the newest chats of every Project."""
        now = time.time()
        items = list_recent_conversations(self.api)
        # Every sidebar page (5 projects each), so a new chat in any project is seen.
        for project in list_projects(self.api, RECENT_PROJECT_CHATS, max_pages=10):
            self.db.upsert_project(project.id, project.title, now)
            items.extend(project.conversations)
        for item in items:
            self._record(item)

    def sweep(self) -> None:
        """List everything. Only after every list succeeded can a missing chat be called deleted."""
        normal_gap = self.call_gap
        self.call_gap = self.sweep_call_gap   # the full check is the biggest burst of requests, so it goes slower
        try:
            self._sweep()
        finally:
            self.call_gap = normal_gap

    def _sweep(self) -> None:
        started = time.time()
        log.info("full check started")
        items = list_conversations(self.api, archived=False) + list_conversations(self.api, archived=True)
        projects = list_projects(self.api, 1)
        for project in projects:
            self.db.upsert_project(project.id, project.title, started)
            items.extend(list_project_conversations(self.api, project.id))
        for item in items:
            self._record(item)

        missing = self.db.not_seen_since(started)
        for row in missing:
            if self.stop_requested:
                return
            self._archive_final_state(row)
            self.mark_deleted(self.db.get(row.id) or row)

        self.db.set_state("last_complete_sweep_at", str(time.time()))
        self.db.snapshot_to(self.config.archive_dir / "state-snapshot.sqlite3")
        self.browser.save_state()
        log.info("full check done: %d chats in %d projects listed, %d newly deleted",
                 len(items), len(projects), len(missing))

    def _record(self, item: ListItem) -> None:
        result = self.db.upsert_listed(item, time.time())
        if result != "same":
            self.last_activity = time.monotonic()
        is_recent = item.create_time >= time.time() - RECENT_CHAT_SECONDS
        if result == "new" and self.config.notify_new_chats and is_recent:
            self.notifier.new_chat(item.title, item.id)
        if result == "reappeared":
            log.info("reappeared in ChatGPT: %s", item.title)
            row = self.db.get(item.id)
            if row and row.folder:
                self.archive.remove_deleted_marker(row.folder)
        elif result != "same":
            log.info("%s: %s", result, item.title or item.id)

    def _archive_final_state(self, row: ConversationRow) -> None:
        """A chat just vanished from the lists. Fetching by id sometimes still works for a short while."""
        try:
            self.process_conversation(row)
        except ApiError as error:
            if error.kind not in ("not_found", "invalid"):
                raise
            log.info("%s can no longer be fetched (%s)", row.title or row.id, error.kind)
        except Exception:   # noqa: BLE001 - a rendering bug must not stop the deletion bookkeeping
            log.exception("could not archive the final state of %s", row.title or row.id)

    # --- archiving chats ------------------------------------------------------------------

    def process_pending(self, limit: int | None, *, updated_after: float | None = None,
                        updated_before: float | None = None, active_since: float | None = None,
                        request_budget: int | None = None) -> int:
        """Fetch chats that are new or changed, newest first. Returns how many were archived.

        With request_budget, no new chat is started once that many requests were made (files count).
        """
        done = 0
        budget_start = self.requests_made
        rows = self.db.pending_conversations(limit, time.time(), updated_after=updated_after,
                                             updated_before=updated_before, active_since=active_since)
        for row in rows:
            if self.stop_requested:
                break
            if request_budget is not None and self.requests_made - budget_start >= request_budget:
                break
            try:
                self.process_conversation(row)
                done += 1
            except ApiError as error:
                if error.kind not in ("not_found", "invalid"):
                    raise   # login, Cloudflare and repeated rate limits are handled by the main loop
                log.warning("cannot fetch %s: %s", row.title or row.id, error)
                self.db.mark_fetch_failed(row.id, str(error), time.time())
            except Exception as error:  # noqa: BLE001 - keep going; the raw JSON is already on disk
                if is_playwright_error(error):
                    raise
                log.exception("could not archive %s", row.title or row.id)
                self.db.mark_fetch_failed(row.id, str(error), time.time())
            self.watchdog.beat()
        return done

    def process_conversation(self, row: ConversationRow) -> None:
        """Fetch one chat and write everything about it: raw JSON first, then files, canvas, transcript."""
        conversation = get_conversation(self.api, row.id)
        now = time.time()
        title = clean_title(conversation.get("title")) or row.title
        create_time = parse_time(conversation.get("create_time")) or row.create_time
        folder = self.archive.folder_for(row.id, title, create_time, row.folder)
        self.archive.write_conversation(folder, conversation)

        for pointer in extract_file_pointers(conversation):
            self.db.upsert_file(row.id, pointer)
        self.download_files(row.id, folder, INLINE_FILES_PER_CHAT)   # any more wait for download_waiting_files

        docs = self._canvas_docs(conversation)
        self.archive.write_canvas(folder, docs)
        self._write_transcript(row, conversation, folder, docs)

        streaming = is_streaming(conversation)
        message_count, node_count = message_counts(conversation)
        self.db.mark_fetched(
            row.id, title=title,
            fetched_update_time=max(row.update_time, parse_time(conversation.get("update_time")) or 0.0),
            folder=folder, model=model_of(conversation), message_count=message_count, node_count=node_count,
            now=now, requeue_after=now + STREAMING_RECHECK_SECONDS if streaming else None)
        self.index_dirty = True
        log.info("archived: %s%s", title or row.id, " (answer still being written; will look again)" if streaming else "")

    def download_files(self, conversation_id: str, folder: str, max_files: int | None = None) -> int:
        """Download a chat's waiting files, at most max_files. Returns how many were dealt with (saved or given up).

        Files not reached stay pending, also when the service is stopped halfway, and download_waiting_files
        picks them up later.
        """
        handled = 0
        for file in self.db.files_to_download(conversation_id)[:max_files]:
            if self.stop_requested:
                break
            try:
                url = resolve_download_url(self.api, file.pointer, conversation_id)
                self._pace()
                data, content_type = self.browser.download(url, MAX_FILE_BYTES)
            except ApiError as error:
                if error.kind in PERMANENT_FILE_ERRORS or error.kind in RETRYABLE_FILE_ERRORS:
                    log.warning("file %s in %s: %s", file.file_id, conversation_id, error)
                    self.db.mark_file_failed(conversation_id, file.file_id, str(error),
                                             permanent=error.kind in PERMANENT_FILE_ERRORS)
                    handled += 1
                    continue
                raise
            local_name = self.archive.local_name_for(file.file_id, file.kind, file.name, file.mime_type, content_type)
            self.archive.write_file(folder, local_name, data)
            self.db.mark_file_done(conversation_id, file.file_id, local_name, time.time())
            log.info("downloaded %s (%d bytes)", local_name, len(data))
            self.watchdog.beat()
            handled += 1
        return handled

    def download_waiting_files(self, request_budget: int) -> int:
        """Files left waiting (a restart mid-chat, or more than INLINE_FILES_PER_CHAT), a small budget per cycle."""
        started = self.requests_made
        handled = 0
        touched: dict[str, str] = {}
        try:
            for conversation_id, folder in self.db.chats_with_waiting_files(self.download_cutoff()):
                used = self.requests_made - started
                if self.stop_requested or used >= request_budget:
                    break
                count = self.download_files(conversation_id, folder, max(1, (request_budget - used) // 2))
                if count:
                    handled += count
                    touched[conversation_id] = folder
        finally:
            for conversation_id, folder in touched.items():   # link the new files, even if a rate limit cut this short
                self._refresh_transcript(conversation_id, folder)
        return handled

    def _refresh_transcript(self, conversation_id: str, folder: str) -> None:
        """Rewrite a saved chat's transcript so it links files downloaded after the chat itself."""
        row = self.db.get(conversation_id)
        conversation = self.archive.read_conversation(folder)
        if row is None or conversation is None:
            return
        self._write_transcript(row, conversation, folder, self._canvas_docs(conversation))

    def _canvas_docs(self, conversation: dict):
        mapping = conversation.get("mapping") or {}
        return extract_canvas_docs(conversation, node_ids(main_path(mapping, conversation.get("current_node"))))

    def _write_transcript(self, row: ConversationRow, conversation: dict, folder: str, docs) -> None:
        files = {file.file_id: FileInfo(file.local_name, file.status, file.name) for file in self.db.files_for(row.id)}
        project_title = self.db.project_title(conversation.get("gizmo_id") or row.gizmo_id)
        self.archive.write_transcript(
            folder, build_transcript(conversation, files, project_title, row.deleted_at, self.tz, docs))

    def mark_deleted(self, row: ConversationRow) -> None:
        """ChatGPT no longer lists this chat. Keep everything; add a marker and update the transcript header."""
        now = time.time()
        if row.folder:
            self.archive.write_deleted_marker(row.folder, now)
            conversation = self.archive.read_conversation(row.folder)
            if conversation is not None:
                files = {file.file_id: FileInfo(file.local_name, file.status, file.name)
                         for file in self.db.files_for(row.id)}
                project_title = self.db.project_title(row.gizmo_id)
                self.archive.write_transcript(
                    row.folder, build_transcript(conversation, files, project_title, now, self.tz))
        self.db.mark_deleted(row.id, now)
        self.index_dirty = True
        log.info("deleted in ChatGPT, kept in the archive: %s", row.title or row.id)

    def archive_pending(self) -> None:
        """Recently active chats first, every cycle. Older chats get a small request budget and wait while rate limited."""
        recent_since = time.time() - RECENT_CHAT_SECONDS
        try:
            done = self.process_pending(RECENT_PER_ITERATION, updated_after=recent_since)
            if time.monotonic() >= self.backlog_paused_until:
                done += self.process_pending(BACKLOG_REQUEST_BUDGET, updated_before=recent_since,
                                             active_since=self.download_cutoff(),
                                             request_budget=BACKLOG_REQUEST_BUDGET)
                done += self.download_waiting_files(FILE_REQUEST_BUDGET)
        except ApiError as error:
            if error.kind != "rate_limited":
                raise
            self._downloads_refused(error)
            log.warning("rate limited by ChatGPT; older chats wait %d minutes, recent chats are still tried every cycle",
                        RATE_LIMIT_PAUSE_SECONDS // 60)
            return
        if done:
            self.downloads_refused_since = None

    def download_cutoff(self) -> float | None:
        """Chats with no activity since then are listed but not downloaded (DOWNLOAD_DAYS). None means everything."""
        if self.config.download_days <= 0:
            return None
        return time.time() - self.config.download_days * 86400

    def _downloads_refused(self, error: ApiError) -> None:
        """Pause the older chats; recent chats are still tried every cycle."""
        if self.downloads_refused_since is None:
            self.downloads_refused_since = time.time()
        self.backlog_paused_until = time.monotonic() + RATE_LIMIT_PAUSE_SECONDS
        self._alert_if_refused_too_long(error)

    def _lists_answered(self) -> None:
        """A chat list came back: note the time for the host health check, and quick checks go back to normal."""
        write_atomic(self.config.data_dir / LAST_SUCCESS_FILE, str(int(time.time())).encode("ascii"))
        if self.lists_refused_since is not None:
            log.info("ChatGPT answers the chat list again")
        self.lists_refused_since = None
        self.list_refusals = 0
        if self.after_rest:
            self.after_rest = False
            self.notifier.resumed()

    def _alert_if_refused_too_long(self, error: ApiError) -> None:
        started = min(t for t in (self.downloads_refused_since, self.lists_refused_since) if t is not None)
        if time.time() - started >= RATE_LIMIT_ALERT_SECONDS:
            self.notifier.set_problem(
                "rate_limited", True,
                f"ChatGPT has refused requests for over {RATE_LIMIT_ALERT_SECONDS // 60} minutes ({error.detail[:100]}). "
                "The backup keeps retrying gently on its own; nothing new is saved until it lets up.")

    def _rate_limit_over_if_clear(self) -> None:
        if self.downloads_refused_since is None and self.lists_refused_since is None:
            self.notifier.set_problem("rate_limited", False, "ChatGPT accepts requests again")

    def rest_until(self) -> float:
        """The Unix time in data/rest-until, or 0. Until then ChatGPT gets no requests at all."""
        try:
            return float((self.config.data_dir / REST_FILE).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0.0

    def quiet_hours_end(self, now: float) -> float | None:
        """When the current QUIET_HOURS end (a Unix time), or None outside them."""
        if self.config.quiet_hours is None:
            return None
        start, end = self.config.quiet_hours
        local = datetime.fromtimestamp(now, self.tz)
        if start < end:
            inside = start <= local.hour < end
        else:                                   # crosses midnight, like 22-6
            inside = local.hour >= start or local.hour < end
        if not inside:
            return None
        end_time = local.replace(hour=end, minute=0, second=0, microsecond=0)
        if end_time <= local:
            end_time += timedelta(days=1)
        return end_time.timestamp()

    def start_quiet_hours_if_due(self) -> None:
        """Quiet hours are a rest: write their end into data/rest-until, which also tells the host health check."""
        quiet_end = self.quiet_hours_end(time.time())
        if quiet_end is not None and quiet_end > self.rest_until():
            write_atomic(self.config.data_dir / REST_FILE, str(quiet_end).encode("ascii"))

    def rest_if_asked(self) -> bool:
        """Honour data/rest-until: close the browser, send nothing, then start again gently. True if it rested."""
        until = self.rest_until()
        if until <= time.time():
            return False
        until_text = format_time(until, self.tz)
        quiet = until == self.quiet_hours_end(time.time())   # the nightly quiet hours are routine: no phone notes
        log.info("%s until %s: no requests to ChatGPT until then", "quiet hours" if quiet else "resting", until_text)
        if not quiet and self.db.get_state("rest_notice") != str(until):   # one note per rest, even across restarts
            self.db.set_state("rest_notice", str(until))
            self.notifier.resting(until_text)
        self.browser.stop()
        self.sleep(until - time.time())
        if self.stop_requested:
            return True
        log.info("rest over; starting again gently")
        self.after_rest = not quiet
        self.list_refusals = 0
        self._schedule_first_sweep()
        self._start_browser_with_retries()
        return True

    def _schedule_first_sweep(self) -> None:
        """After a start or a rest: quick checks first, and no full check until a day after the last complete one."""
        last_sweep = float(self.db.get_state("last_complete_sweep_at") or 0)
        due_in = last_sweep + SWEEP_SECONDS - time.time()
        self.next_sweep = time.monotonic() + max(FIRST_SWEEP_DELAY_SECONDS, due_in)

    def write_index(self) -> None:
        if not self.index_dirty:
            return
        rows = self.db.all_conversations()
        titles = self.db.project_titles()
        self.archive.write_index(build_index_md(rows, titles, self.tz), build_index_csv(rows, titles, self.tz))
        self.index_dirty = False

    def check_health(self) -> None:
        free = self.archive.free_bytes()
        self.notifier.set_problem("disk", free < MIN_FREE_BYTES,
                                  f"{free / 1024 ** 3:.1f} GB free on the archive disk" +
                                  ("; make room or the backup will stop" if free < MIN_FREE_BYTES else ""))
        now = time.time()
        if self.notifier.digest_due(now, self.tz):
            stats: dict = dict(self.db.stats_since(now - 86400, active_since=self.download_cutoff()))
            last_sweep = self.db.get_state("last_complete_sweep_at")
            if last_sweep:
                stats["last_sweep"] = format_time(float(last_sweep), self.tz)
            self.notifier.send_digest(stats, now, self.tz)

    # --- the loop -----------------------------------------------------------------------------

    def run_forever(self) -> int:
        self.watchdog.start()
        self.notifier.startup_notice(time.time())
        self._schedule_first_sweep()
        self.start_quiet_hours_if_due()
        if not self.rest_if_asked():     # a rest starts before the browser ever opens chatgpt.com
            self._start_browser_with_retries()
        self.next_restart = time.monotonic() + BROWSER_RESTART_SECONDS
        next_poll = time.monotonic()
        while not self.stop_requested:
            self.watchdog.beat()
            self.start_quiet_hours_if_due()
            if self.rest_if_asked():
                next_poll = time.monotonic()
                continue
            wait = next_poll - time.monotonic()
            if wait > 0:
                self.sleep(wait)
                continue
            wait_seconds = self._cycle()
            next_poll = time.monotonic() + wait_seconds   # the rest starts when the cycle ends, however long it took
        log.info("stopping")
        self.browser.stop()
        return 0

    def run_once(self) -> int:
        """One fast poll plus up to ONCE_PENDING_LIMIT chats, then exit. For setup and testing."""
        self.watchdog.start()
        try:
            self._start_browser()
            if self.login_dead:
                print(LOGIN_HELP)
                return 2
            self.fast_poll()
            archived = self.process_pending(ONCE_PENDING_LIMIT, active_since=self.download_cutoff())
            self.write_index()
        except ApiError as error:
            if error.kind == "login_required":
                print(LOGIN_HELP)
                return 2
            log.error("stopped: %s", error)
            return 1
        except Exception:  # noqa: BLE001 - show the problem instead of a bare traceback
            log.exception("stopped with an unexpected error")
            return 1
        finally:
            self.browser.stop()
        print(f"Done. {len(self.db.all_conversations())} chats known, {archived} archived this run. "
              f"Archive: {self.config.archive_dir}")
        return 0

    def _cycle(self) -> float:
        """One iteration of the loop. Returns the number of seconds to wait before the next one."""
        try:
            if self.login_dead:
                self._retry_login()
                if self.login_dead:
                    return LOGIN_RETRY_SECONDS
                self.notifier.set_problem("login", False, "Logged in to ChatGPT again")

            if time.monotonic() >= self.next_sweep and not self.is_active():
                try:
                    self.sweep()
                    self.next_sweep = time.monotonic() + SWEEP_SECONDS
                except ApiError as error:
                    if error.kind != "rate_limited":
                        raise
                    self.backlog_paused_until = time.monotonic() + RATE_LIMIT_PAUSE_SECONDS
                    self.next_sweep = time.monotonic() + SWEEP_RETRY_SECONDS
                    log.warning("full check rate limited by ChatGPT; trying it again in %d minutes, "
                                "quick checks carry on", SWEEP_RETRY_SECONDS // 60)
                    return self._poll_interval()
            else:
                self.fast_poll()
            self._lists_answered()
            self.archive_pending()
            self.write_index()
            self.check_health()

            self.failures = 0
            self.challenge_failures = 0
            self.notifier.set_problem("api_errors", False, "ChatGPT API calls work again")
            self.notifier.set_problem("challenge", False, "Cloudflare check cleared")
            self._rate_limit_over_if_clear()

            if time.monotonic() >= self.next_restart:
                log.info("routine browser restart")
                self._restart_browser()
                self.next_restart = time.monotonic() + BROWSER_RESTART_SECONDS
            return self._poll_interval()

        except ApiError as error:
            if error.kind == "login_required":
                self._login_died()
                return LOGIN_RETRY_SECONDS
            if error.kind in ("challenge", "blocked"):
                return self._handle_challenge(error)
            if error.kind == "rate_limited":
                return self._handle_rate_limit(error)
            return self._handle_failure(error)
        except Exception as error:  # noqa: BLE001 - the loop must survive anything
            return self._handle_failure(error)

    # --- browser and error handling --------------------------------------------------------

    def _start_browser(self) -> None:
        """Start Chromium. A dead login is not an error here: it sets login_dead and we carry on."""
        try:
            self.browser.start()
            self.login_dead = False
        except ApiError as error:
            if error.kind != "login_required":
                raise
            self._login_died()

    def _start_browser_with_retries(self) -> None:
        for attempt in range(1, 4):
            try:
                self._start_browser()
                return
            except Exception:  # noqa: BLE001
                log.exception("browser start failed (attempt %d of 3)", attempt)
                self.browser.stop()
                if attempt == 3:
                    raise SystemExit(1)
                self.sleep(60)

    def _restart_browser(self) -> None:
        self.browser.stop()
        self._start_browser()

    def _retry_login(self) -> None:
        if self.browser.seed_available():
            log.info("a new storage_state.json appeared; restarting the browser to use it")
            self._restart_browser()
        else:
            self.browser.reload()   # raises login_required while the session is still dead
            self.login_dead = False

    def _login_died(self) -> None:
        self.login_dead = True
        self.notifier.set_problem(
            "login", True,
            "The ChatGPT login has expired. On your laptop run: python tools/export_session.py, copy "
            "storage_state.json into chat-backup/data/ on the backup machine, then wait up to 10 minutes "
            "(or run: docker compose restart).")

    def _handle_challenge(self, error: ApiError) -> float:
        self.challenge_failures += 1
        log.warning("%s; reloading chatgpt.com so the browser can pass the check (attempt %d)",
                    error, self.challenge_failures)
        try:
            self.browser.reload()
        except Exception as reload_error:  # noqa: BLE001
            log.warning("reload failed: %s", reload_error)
        if self.challenge_failures >= 2:
            self.notifier.set_problem(
                "challenge", True,
                f"Cloudflare keeps challenging the poller ({error.detail}). It retries on its own. If this "
                "lasts for hours, check that the machine is on your home internet, not a VPN.")
        return CHALLENGE_RETRY_SECONDS if error.kind == "challenge" else BLOCKED_RETRY_SECONDS

    def is_active(self) -> bool:
        """True while you are using ChatGPT: a quick check found a new or changed chat in the last 20 minutes."""
        return time.monotonic() - self.last_activity < ACTIVE_WINDOW_SECONDS

    def _poll_interval(self) -> float:
        interval = ACTIVE_POLL_SECONDS if self.is_active() else IDLE_POLL_SECONDS
        return interval * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER)

    def _handle_rate_limit(self, error: ApiError) -> float:
        """The quick check's chat list was refused. Not a failure: back off 15, 30, then 60 minutes."""
        self.list_refusals += 1
        if self.lists_refused_since is None:
            self.lists_refused_since = time.time()
        self.backlog_paused_until = time.monotonic() + RATE_LIMIT_PAUSE_SECONDS
        self._alert_if_refused_too_long(error)
        first, longest = LIST_BACKOFF_SECONDS
        wait = min(first * 2 ** (self.list_refusals - 1), longest)
        log.warning("chat list rate limited by ChatGPT (%d in a row); next quick check in %d minutes",
                    self.list_refusals, wait // 60)
        return wait

    def _handle_failure(self, error: BaseException) -> float:
        self.failures += 1
        log.exception("cycle failed (%d in a row): %s", self.failures, error)
        if is_playwright_error(error) or self.failures % 3 == 0:
            try:
                self._restart_browser()
                self.restart_failures = 0
            except Exception:  # noqa: BLE001
                self.restart_failures += 1
                log.exception("browser restart failed (%d in a row)", self.restart_failures)
                if self.restart_failures >= 3:
                    log.critical("cannot start the browser; exiting so Docker restarts the container")
                    raise SystemExit(1)
        if self.failures >= 3:
            self.notifier.set_problem(
                "api_errors", True,
                f"{self.failures} cycles in a row failed. Last error: {str(error)[:200]}. "
                "Check: docker compose logs --tail 100")
        return min(60.0 * 2 ** self.failures, MAX_BACKOFF_SECONDS)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chatbackup", description="Keep a local copy of every ChatGPT chat.")
    parser.add_argument("--once", action="store_true",
                        help="check once, archive up to 8 chats, then exit (for setup and testing)")
    args = parser.parse_args(argv)

    config = load_config()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.data_dir.mkdir(parents=True, exist_ok=True)

    db = Database(config.data_dir / "chatbackup.sqlite3")
    archive = Archive(config.archive_dir, ZoneInfo(config.timezone))
    notifier = Notifier(config.ntfy_url, config.ntfy_topic, db)
    watchdog = Watchdog(config.data_dir / ".heartbeat")
    browser = Browser(config)
    poller = Poller(config, db, browser, archive, notifier, watchdog)

    def request_stop(signum, frame) -> None:
        log.info("stop requested (signal %d)", signum)
        poller.stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        code = poller.run_once() if args.once else poller.run_forever()
    finally:
        db.close()
    sys.exit(code)
