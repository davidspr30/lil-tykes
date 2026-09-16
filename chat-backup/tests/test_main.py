import dataclasses
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chatbackup.archive import Archive
from chatbackup.chatgpt import ApiError, ListItem
from chatbackup.config import Config
from chatbackup.db import Database
from chatbackup.main import (BACKLOG_REQUEST_BUDGET, FIRST_SWEEP_DELAY_SECONDS, LIST_BACKOFF_SECONDS,
                             RATE_LIMIT_ALERT_SECONDS, RATE_LIMIT_PAUSE_SECONDS, REST_FILE, SWEEP_RETRY_SECONDS,
                             Poller, Watchdog)
from chatbackup.notify import Notifier

FILES_ID = "22222222-2222-3333-4444-555555555555"
BIG_IMAGE = "file_00000000842871f5b6a1bab8e3499232"
ESTUARY = "https://chatgpt.com/backend-api/estuary/content?id="


class FakeBrowser:
    def __init__(self, api, downloads):
        self.api = api
        self.downloads = downloads
        self.reloads = 0
        self.saves = 0
        self.starts = 0

    def api_get(self, path):
        return self.api(path)

    def download(self, url, max_bytes):
        answer = self.downloads[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def reload(self):
        self.reloads += 1

    def save_state(self):
        self.saves += 1

    def seed_available(self):
        return False

    def start(self):
        self.starts += 1

    def stop(self):
        pass


@pytest.fixture
def poller(tmp_path, fake_api):
    def build(routes, downloads=None):
        config = Config(data_dir=tmp_path / "data", timezone="America/New_York", locale="en-US",
                        ntfy_topic="", ntfy_url="https://ntfy.sh", log_level="INFO")
        config.data_dir.mkdir(exist_ok=True)
        db = Database(config.data_dir / "test.sqlite3")
        api = fake_api(routes)
        watchdog = Watchdog(config.data_dir / ".heartbeat")
        watchdog.start = lambda: None           # no background thread in tests
        instance = Poller(config, db, FakeBrowser(api, downloads or {}),
                          Archive(config.archive_dir, ZoneInfo(config.timezone)),
                          Notifier("https://ntfy.sh", "", db), watchdog)
        instance.call_gap = (0, 0)              # no pacing pauses in tests
        instance.sweep_call_gap = (0, 0)
        instance.api_calls = api
        return instance
    return build


def test_watchdog_stall_detection():
    watchdog = Watchdog(Path("/nonexistent/.heartbeat"), stall_limit=100)
    assert not watchdog.is_stalled()
    assert watchdog.is_stalled(time.monotonic() + 101)
    watchdog.beat()
    assert not watchdog.is_stalled(time.monotonic() + 50)


def test_process_conversation_writes_everything(poller, files_conversation):
    routes = {
        f"/backend-api/conversation/{FILES_ID}": files_conversation,
        "/backend-api/files/file-Abc123/download": {"download_url": ESTUARY + "abc"},
        f"/backend-api/conversation/{FILES_ID}/attachment/{BIG_IMAGE}/download": {"download_url": ESTUARY + "big"},
        "/backend-api/files/file-Rep456/download": ApiError("not_found", 404),
        f"/backend-api/conversation/{FILES_ID}/attachment/file-Rep456/download": ApiError("invalid", 422),
    }
    downloads = {ESTUARY + "abc": (b"\x89PNG-photo", "image/png"), ESTUARY + "big": (b"\x89PNG-cat", "image/png")}
    instance = poller(routes, downloads)
    instance.db.upsert_project("g-p-aaaa", "Work", 1.0)
    instance.db.upsert_listed(ListItem(FILES_ID, "Pictures", 1789310000.0, 1789310300.0, False, "g-p-aaaa"), 1.0)

    assert instance.process_pending(20) == 1

    row = instance.db.get(FILES_ID)
    assert row.pending is False
    assert row.folder == "2026-09/2026-09-13_pictures_22222222"
    assert row.message_count == 4 and row.node_count == 5 and row.model == "gpt-5"
    folder = instance.archive.root / row.folder
    assert json.loads((folder / "conversation.json").read_text())["title"] == "Pictures"
    transcript = (folder / "transcript.md").read_text()
    assert "![image](files/file-Abc123_photo.png)" in transcript
    assert f"![image](files/{BIG_IMAGE}.png)" in transcript
    assert "- report: final?.pdf (not downloaded: gone)" in transcript
    assert "- Project: Work" in transcript
    assert (folder / "files" / "file-Abc123_photo.png").read_bytes() == b"\x89PNG-photo"
    assert (folder / "files" / f"{BIG_IMAGE}.png").read_bytes() == b"\x89PNG-cat"
    statuses = {file.file_id: file.status for file in instance.db.files_for(FILES_ID)}
    assert statuses == {"file-Abc123": "done", BIG_IMAGE: "done", "file-Rep456": "gone"}

    instance.write_index()
    assert "[Pictures](2026-09/2026-09-13_pictures_22222222/transcript.md)" in (instance.archive.root / "INDEX.md").read_text()

    # A later change fetches the chat again but downloads nothing twice.
    calls_before = len(instance.api_calls.calls)
    instance.db.upsert_listed(ListItem(FILES_ID, "Pictures", 1789310000.0, 1789310400.0, False, "g-p-aaaa"), 2.0)
    assert instance.process_pending(20) == 1
    assert len(instance.api_calls.calls) == calls_before + 1


def test_files_not_reached_before_a_stop_are_downloaded_later(poller, files_conversation):
    routes = {
        f"/backend-api/conversation/{FILES_ID}": files_conversation,
        "/backend-api/files/file-Abc123/download": {"download_url": ESTUARY + "abc"},
        f"/backend-api/conversation/{FILES_ID}/attachment/{BIG_IMAGE}/download": {"download_url": ESTUARY + "big"},
        "/backend-api/files/file-Rep456/download": ApiError("not_found", 404),
        f"/backend-api/conversation/{FILES_ID}/attachment/file-Rep456/download": ApiError("invalid", 422),
    }
    downloads = {ESTUARY + "abc": (b"\x89PNG-photo", "image/png"), ESTUARY + "big": (b"\x89PNG-cat", "image/png")}
    instance = poller(routes, downloads)
    instance.db.upsert_project("g-p-aaaa", "Work", 1.0)
    instance.db.upsert_listed(ListItem(FILES_ID, "Pictures", 1789310000.0, 1789310300.0, False, "g-p-aaaa"), 1.0)

    real_download = instance.browser.download

    def download_then_stop(url, max_bytes):
        instance.stop_requested = True                                     # "docker compose stop" arrives mid-chat
        return real_download(url, max_bytes)

    instance.browser.download = download_then_stop
    instance.process_pending(20)
    row = instance.db.get(FILES_ID)
    assert row.pending is False                                            # the chat itself is saved...
    assert sum(file.status == "pending" for file in instance.db.files_for(FILES_ID)) == 2   # ...two files still wait

    instance.stop_requested = False
    instance.browser.download = real_download
    assert instance.download_waiting_files(1) == 1                         # a tiny budget: one file per cycle
    assert sum(file.status == "pending" for file in instance.db.files_for(FILES_ID)) == 1
    assert instance.download_waiting_files(100) == 1
    statuses = {file.file_id: file.status for file in instance.db.files_for(FILES_ID)}
    assert statuses == {"file-Abc123": "done", BIG_IMAGE: "done", "file-Rep456": "gone"}
    transcript = (instance.archive.root / row.folder / "transcript.md").read_text()
    assert f"![image](files/{BIG_IMAGE}.png)" in transcript                # the saved copy links the late file
    assert instance.db.chats_with_waiting_files() == []


def test_streaming_answer_is_checked_again_later(poller, canvas_conversation):
    chat_id = canvas_conversation["conversation_id"]
    instance = poller({f"/backend-api/conversation/{chat_id}": canvas_conversation})
    instance.db.upsert_listed(ListItem(chat_id, "Trip planning", 1789320000.0, 1789320500.0, False, None), 1.0)

    instance.process_pending(20)

    row = instance.db.get(chat_id)
    assert row.pending is True and row.not_before is not None and row.not_before > time.time() + 100
    canvas = instance.archive.root / row.folder / "canvas"
    assert (canvas / "trip-plan.md").read_text() == "Day 1: Lisbon\nDay 2: Sintra"
    assert (canvas / "trip-plan.md.replay-warning.txt").exists()


def test_unfetchable_chat_is_scheduled_for_retry(poller):
    instance = poller({"/backend-api/conversation/gone": ApiError("not_found", 404)})
    instance.db.upsert_listed(ListItem("gone", "Gone", 1.0, 2.0, False, None), 1.0)
    assert instance.process_pending(20) == 0
    row = instance.db.get("gone")
    assert row.pending is True and row.fetch_failures == 1 and row.not_before > time.time()


def test_api_retries_once_on_an_expired_token(poller):
    instance = poller({"/x": [ApiError("token_expired", 401), {"ok": True}]})
    assert instance.api("/x") == {"ok": True}
    assert instance.browser.reloads == 1


def test_api_gives_up_after_a_second_expired_token(poller):
    instance = poller({"/y": [ApiError("token_expired", 401), ApiError("token_expired", 401)]})
    with pytest.raises(ApiError):
        instance.api("/y")
    assert instance.browser.reloads == 1


def test_api_does_not_retry_when_rate_limited(poller):
    instance = poller({"/x": [ApiError("rate_limited", 429), {"ok": True}]})
    with pytest.raises(ApiError):
        instance.api("/x")
    assert instance.api_calls.calls == ["/x"]                               # no second request into a rate limit


def test_refused_quick_checks_back_off_and_alert_after_a_while(poller):
    too_many = ApiError("rate_limited", 429, detail="Too many requests")
    instance = poller({
        "/backend-api/conversations?offset=0&limit=50&order=updated": [too_many] * 6 + [{"items": []}],
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
    })
    instance.next_sweep = float("inf")          # only quick checks run
    instance.next_restart = float("inf")        # no routine browser restart
    first, longest = LIST_BACKOFF_SECONDS

    waits = [instance._cycle() for _ in range(5)]
    assert waits == [first, first * 2, first * 4, first * 8, longest]            # 2, 4, 8, 16, 30 minutes
    assert instance.failures == 0 and instance.browser.starts == 0               # not an error, no browser restart
    assert instance.db.get_state("alert:rate_limited", "ok") == "ok"             # not during the first 30 minutes

    instance.lists_refused_since = time.time() - RATE_LIMIT_ALERT_SECONDS - 1
    assert instance._cycle() == longest
    assert instance.db.get_state("alert:rate_limited", "ok") == "problem"

    assert instance._cycle() < first                                             # answered: back to every minute
    assert instance.list_refusals == 0
    assert instance.db.get_state("alert:rate_limited", "ok") == "ok"
    assert instance.db.get_state("alert:api_errors", "ok") == "ok"


def test_rate_limited_full_check_is_postponed_while_quick_checks_carry_on(poller):
    instance = poller({
        "/backend-api/conversations?offset=0&limit=100&order=updated": ApiError("rate_limited", 429),
        "/backend-api/conversations?offset=0&limit=50&order=updated": {"items": []},
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
    })
    instance.next_restart = float("inf")

    assert instance._cycle() < LIST_BACKOFF_SECONDS[0]                     # no long pause
    assert instance.next_sweep > time.monotonic() + SWEEP_RETRY_SECONDS - 5
    assert instance.failures == 0 and instance.call_gap == (0, 0)          # normal pacing restored after the check

    instance.api_calls.calls.clear()
    instance._cycle()                                                      # the next cycle is a normal quick check
    assert "/backend-api/conversations?offset=0&limit=50&order=updated" in instance.api_calls.calls


def test_rest_file_means_no_requests_then_a_gentle_start(poller, monkeypatch):
    instance = poller({})
    slept, notes = [], []
    monkeypatch.setattr(instance, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(instance.notifier, "resting", lambda until_text: notes.append("resting"))
    monkeypatch.setattr(instance.notifier, "resumed", lambda: notes.append("resumed"))

    assert instance.rest_if_asked() is False                               # no rest file: carry on
    (instance.config.data_dir / REST_FILE).write_text(str(time.time() + 3 * 3600))
    assert instance.rest_if_asked() is True
    assert slept and slept[0] > 3 * 3600 - 60                              # sat out the whole rest...
    assert instance.api_calls.calls == []                                  # ...without a single request
    assert instance.browser.starts == 1                                    # then started again
    assert instance.next_sweep > time.monotonic() + FIRST_SWEEP_DELAY_SECONDS - 5   # quick checks first
    assert notes == ["resting"]

    instance._lists_answered()
    instance._lists_answered()
    assert notes == ["resting", "resumed"]                                 # one "resumed" note, once ChatGPT answers


def test_recent_chats_are_fetched_while_the_backlog_waits(poller, monkeypatch, branch_conversation):
    recent_id = branch_conversation["conversation_id"]
    now = time.time()
    routes = {
        "/backend-api/conversations?offset=0&limit=50&order=updated": {"items": [
            {"id": recent_id, "title": "Just now", "create_time": now - 60, "update_time": now - 60},
            {"id": "old-chat", "title": "Last year", "create_time": now - 400 * 86400, "update_time": now - 400 * 86400},
        ]},
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
        f"/backend-api/conversation/{recent_id}": branch_conversation,
        "/backend-api/conversation/old-chat": ApiError("rate_limited", 429, detail="Too many requests"),
    }
    instance = poller(routes)
    monkeypatch.setattr(instance, "sleep", lambda seconds: None)
    instance.next_sweep = float("inf")
    instance.next_restart = float("inf")

    assert instance._cycle() < RATE_LIMIT_PAUSE_SECONDS                     # a refused old chat doesn't stop polling
    assert instance.db.get(recent_id).pending is False                     # the recent chat was saved first
    assert instance.db.get("old-chat").pending is True
    assert instance.backlog_paused_until > time.monotonic() and instance.failures == 0

    instance.api_calls.calls.clear()
    instance._cycle()
    assert "/backend-api/conversation/old-chat" not in instance.api_calls.calls  # the backlog waits out the pause


def test_backlog_stops_at_the_request_budget(poller, monkeypatch, branch_conversation):
    old = time.time() - 400 * 86400
    routes = {
        "/backend-api/conversations?offset=0&limit=50&order=updated": {"items": [
            {"id": f"old-{n}", "title": f"Old {n}", "create_time": old + n, "update_time": old + n}
            for n in range(BACKLOG_REQUEST_BUDGET + 5)]},
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
        "/backend-api/conversation/": branch_conversation,
    }
    instance = poller(routes)
    instance.next_sweep = float("inf")
    instance.next_restart = float("inf")
    instance._cycle()
    fetches = [path for path in instance.api_calls.calls if path.startswith("/backend-api/conversation/")]
    assert len(fetches) == BACKLOG_REQUEST_BUDGET                          # one request per chat here, no files


def test_chats_outside_download_days_are_listed_but_not_downloaded(poller, branch_conversation):
    now = time.time()
    last_week_id = branch_conversation["conversation_id"]
    routes = {
        "/backend-api/conversations?offset=0&limit=50&order=updated": {"items": [
            {"id": "last-year", "title": "Last year", "create_time": now - 400 * 86400, "update_time": now - 300 * 86400},
            {"id": last_week_id, "title": "Last week", "create_time": now - 7 * 86400, "update_time": now - 7 * 86400},
        ]},
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
        "/backend-api/conversation/": branch_conversation,
    }
    instance = poller(routes)
    instance.config = dataclasses.replace(instance.config, download_days=14)
    instance.next_sweep = float("inf")
    instance.next_restart = float("inf")

    instance._cycle()
    fetched = [path for path in instance.api_calls.calls if path.startswith("/backend-api/conversation/")]
    assert fetched == [f"/backend-api/conversation/{last_week_id}"]
    old = instance.db.get("last-year")
    assert old.folder is None and old.title == "Last year"                 # listed by title, never downloaded


def test_new_chat_alert_only_when_enabled_and_only_for_recent_chats(poller, monkeypatch):
    instance = poller({})
    alerts = []
    monkeypatch.setattr(instance.notifier, "new_chat", lambda title, conversation_id: alerts.append(conversation_id))
    now = time.time()

    def chat(chat_id, created):
        return ListItem(id=chat_id, title=chat_id, create_time=created, update_time=now, is_archived=False, gizmo_id=None)

    instance._record(chat("off", now - 60))
    assert alerts == []                                          # off unless NOTIFY_NEW_CHATS is set

    instance.config = dataclasses.replace(instance.config, notify_new_chats=True)
    instance._record(chat("fresh", now - 60))
    instance._record(chat("old", now - 30 * 86400))              # an old chat seen for the first time stays quiet
    instance._record(chat("fresh", now - 60))                    # already known: no second alert
    assert alerts == ["fresh"]


def test_sweep_marks_missing_chats_deleted(poller, branch_conversation):
    chat_id = branch_conversation["conversation_id"]
    routes = {
        "/backend-api/conversations?offset=0&limit=100&order=updated": {"items": []},
        "/backend-api/conversations?offset=0&limit=100&order=updated&is_archived=true": {"items": []},
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
        f"/backend-api/conversation/{chat_id}": [branch_conversation, ApiError("not_found", 404)],
    }
    instance = poller(routes)
    instance.db.upsert_listed(ListItem(chat_id, "Joke time", 1789300000.0, 1789300400.0, False, None), 1.0)
    instance.process_pending(20)                  # archived while it still existed

    instance.sweep()                              # now it is gone from every list
    assert instance.call_gap == (0, 0)            # the slower full-check pacing is undone afterwards

    row = instance.db.get(chat_id)
    assert row.deleted_at is not None
    folder = instance.archive.root / row.folder
    assert (folder / "DELETED.txt").exists()
    assert "- Status: deleted from ChatGPT" in (folder / "transcript.md").read_text()
    assert (instance.archive.root / "state-snapshot.sqlite3").exists()
    assert instance.browser.saves == 1
    assert instance.db.get_state("last_complete_sweep_at") is not None


def test_run_once(poller, branch_conversation, capsys):
    chat_id = branch_conversation["conversation_id"]
    routes = {
        "/backend-api/conversations?offset=0&limit=50&order=updated": {"items": [
            {"id": chat_id, "title": "Joke time\n", "create_time": "2026-09-13T10:00:00Z",
             "update_time": "2026-09-13T10:06:40Z"}]},
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
        f"/backend-api/conversation/{chat_id}": branch_conversation,
    }
    instance = poller(routes)
    assert instance.run_once() == 0
    assert "1 chats known, 1 archived this run" in capsys.readouterr().out
    assert (instance.archive.root / "INDEX.md").exists()
    assert instance.browser.starts == 1


def test_answered_quick_check_records_last_success(poller):
    too_many = ApiError("rate_limited", 429, detail="Too many requests")
    instance = poller({
        "/backend-api/conversations?offset=0&limit=50&order=updated": [too_many, {"items": []}],
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
    })
    instance.next_sweep = float("inf")
    instance.next_restart = float("inf")
    marker = instance.config.data_dir / ".last-success"

    instance._cycle()
    assert not marker.exists()                                   # refused: nothing to record
    instance._cycle()
    assert abs(int(marker.read_text()) - time.time()) < 5        # answered: the host script sees a fresh time
