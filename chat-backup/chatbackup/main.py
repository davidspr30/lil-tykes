"""The polling loop. Read this file top to bottom to see how the service behaves.

Every minute (fast poll): look at the most recently updated chats and the
Projects sidebar, and fetch whatever is new or changed.
Every 30 minutes (full check): list everything, so chats that vanished can be
marked as deleted, and save a snapshot of the bookkeeping.
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

POLL_SECONDS = 60                   # fast poll interval
POLL_JITTER = 0.4                   # +/- 40 %, so the timing is not a metronome
SWEEP_SECONDS = 30 * 60             # full check interval
CALL_GAP_SECONDS = (1.0, 1.5)       # minimum pause between two API calls
PENDING_PER_ITERATION = 8           # chats fetched per cycle; about 8 a minute stays under ChatGPT's rate limit
RATE_LIMIT_PAUSE_SECONDS = 10 * 60  # ChatGPT said "too many requests": wait this long, then carry on
RATE_LIMIT_ALERT_AFTER = 6          # rate-limited cycles in a row (over an hour) before the phone hears about it
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
        self.rate_limited_cycles = 0
        self.restart_failures = 0
        self.next_sweep = 0.0
        self.next_restart = 0.0
        self.call_gap = CALL_GAP_SECONDS
        self._last_call = 0.0

    # --- talking to ChatGPT -------------------------------------------------------------

    def api(self, path: str):
        """One API call with pacing. Handles the two errors that a simple retry fixes."""
        self._pace()
        try:
            return self.browser.api_get(path)
        except ApiError as error:
            if error.kind == "token_expired":
                log.info("token expired; reloading chatgpt.com to get a new one")
                self.browser.reload()
            elif error.kind == "rate_limited":
                wait = min(error.retry_after or 30.0, 300.0)
                log.warning("rate limited by ChatGPT; waiting %.0f s", wait)
                self.sleep(wait)
            else:
                raise
        self._pace()
        return self.browser.api_get(path)

    def _pace(self) -> None:
        wait = self._last_call + random.uniform(*self.call_gap) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

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
        """Two requests: the newest chats outside Projects, and each Project's newest chats."""
        now = time.time()
        items = list_recent_conversations(self.api)
        for project in list_projects(self.api, RECENT_PROJECT_CHATS, max_pages=1):
            self.db.upsert_project(project.id, project.title, now)
            items.extend(project.conversations)
        for item in items:
            self._record(item)

    def sweep(self) -> None:
        """List everything. Only after every list succeeded can a missing chat be called deleted."""
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

    def process_pending(self, limit: int | None) -> int:
        """Fetch chats that are new or changed, newest first. Returns how many were archived."""
        done = 0
        for row in self.db.pending_conversations(limit, time.time()):
            if self.stop_requested:
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

        self.download_files(row.id, conversation, folder)

        mapping = conversation.get("mapping") or {}
        docs = extract_canvas_docs(conversation, node_ids(main_path(mapping, conversation.get("current_node"))))
        self.archive.write_canvas(folder, docs)
        files = {file.file_id: FileInfo(file.local_name, file.status, file.name) for file in self.db.files_for(row.id)}
        project_title = self.db.project_title(conversation.get("gizmo_id") or row.gizmo_id)
        self.archive.write_transcript(
            folder, build_transcript(conversation, files, project_title, row.deleted_at, self.tz, docs))

        streaming = is_streaming(conversation)
        message_count, node_count = message_counts(conversation)
        self.db.mark_fetched(
            row.id, title=title,
            fetched_update_time=max(row.update_time, parse_time(conversation.get("update_time")) or 0.0),
            folder=folder, model=model_of(conversation), message_count=message_count, node_count=node_count,
            now=now, requeue_after=now + STREAMING_RECHECK_SECONDS if streaming else None)
        self.index_dirty = True
        log.info("archived: %s%s", title or row.id, " (answer still being written; will look again)" if streaming else "")

    def download_files(self, conversation_id: str, conversation: dict, folder: str) -> None:
        for pointer in extract_file_pointers(conversation):
            self.db.upsert_file(conversation_id, pointer)
        for file in self.db.files_to_download(conversation_id):
            if self.stop_requested:
                return
            try:
                url = resolve_download_url(self.api, file.pointer, conversation_id)
                self._pace()
                data, content_type = self.browser.download(url, MAX_FILE_BYTES)
            except ApiError as error:
                if error.kind in PERMANENT_FILE_ERRORS or error.kind in RETRYABLE_FILE_ERRORS:
                    log.warning("file %s in %s: %s", file.file_id, conversation_id, error)
                    self.db.mark_file_failed(conversation_id, file.file_id, str(error),
                                             permanent=error.kind in PERMANENT_FILE_ERRORS)
                    continue
                raise
            local_name = self.archive.local_name_for(file.file_id, file.kind, file.name, file.mime_type, content_type)
            self.archive.write_file(folder, local_name, data)
            self.db.mark_file_done(conversation_id, file.file_id, local_name, time.time())
            log.info("downloaded %s (%d bytes)", local_name, len(data))
            self.watchdog.beat()

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
            stats: dict = dict(self.db.stats_since(now - 86400))
            last_sweep = self.db.get_state("last_complete_sweep_at")
            if last_sweep:
                stats["last_sweep"] = format_time(float(last_sweep), self.tz)
            self.notifier.send_digest(stats, now, self.tz)

    # --- the loop -----------------------------------------------------------------------------

    def run_forever(self) -> int:
        self.watchdog.start()
        self._start_browser_with_retries()
        self.notifier.startup_notice(time.time())
        self.next_sweep = time.monotonic()
        self.next_restart = time.monotonic() + BROWSER_RESTART_SECONDS
        next_poll = time.monotonic()
        while not self.stop_requested:
            self.watchdog.beat()
            wait = next_poll - time.monotonic()
            if wait > 0:
                self.sleep(wait)
                continue
            next_poll = time.monotonic() + self._cycle()
        log.info("stopping")
        self.browser.stop()
        return 0

    def run_once(self) -> int:
        """One fast poll plus up to PENDING_PER_ITERATION chats, then exit. For setup and testing."""
        self.watchdog.start()
        try:
            self._start_browser()
            if self.login_dead:
                print(LOGIN_HELP)
                return 2
            self.fast_poll()
            archived = self.process_pending(PENDING_PER_ITERATION)
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

            if time.monotonic() >= self.next_sweep:
                self.sweep()
                self.next_sweep = time.monotonic() + SWEEP_SECONDS
            else:
                self.fast_poll()
            self.process_pending(PENDING_PER_ITERATION)
            self.write_index()
            self.check_health()

            self.failures = 0
            self.challenge_failures = 0
            self.rate_limited_cycles = 0
            self.notifier.set_problem("api_errors", False, "ChatGPT API calls work again")
            self.notifier.set_problem("challenge", False, "Cloudflare check cleared")
            self.notifier.set_problem("rate_limited", False, "ChatGPT accepts requests again")

            if time.monotonic() >= self.next_restart:
                log.info("routine browser restart")
                self._restart_browser()
                self.next_restart = time.monotonic() + BROWSER_RESTART_SECONDS
            return POLL_SECONDS * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER)

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

    def _handle_rate_limit(self, error: ApiError) -> float:
        """ChatGPT wants fewer requests. That is not a failure: no browser restart, no error alert, just a pause."""
        self.rate_limited_cycles += 1
        log.warning("rate limited by ChatGPT (%d cycles in a row); pausing %d minutes",
                    self.rate_limited_cycles, RATE_LIMIT_PAUSE_SECONDS // 60)
        if self.rate_limited_cycles >= RATE_LIMIT_ALERT_AFTER:
            self.notifier.set_problem(
                "rate_limited", True,
                f"ChatGPT has refused requests for over an hour ({error.detail[:100]}). The backup keeps "
                "pausing and retrying on its own; chats are saved once it lets up.")
        return RATE_LIMIT_PAUSE_SECONDS

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
