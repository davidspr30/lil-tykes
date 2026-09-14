import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chatbackup.archive import Archive
from chatbackup.chatgpt import ApiError, ListItem
from chatbackup.config import Config
from chatbackup.db import Database
from chatbackup.main import RATE_LIMIT_ALERT_AFTER, RATE_LIMIT_PAUSE_SECONDS, Poller, Watchdog
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


def test_api_waits_when_rate_limited(poller, monkeypatch):
    instance = poller({"/x": [ApiError("rate_limited", 429, retry_after=5), {"ok": True}]})
    waited = []
    monkeypatch.setattr(instance, "sleep", lambda seconds: waited.append(seconds))
    assert instance.api("/x") == {"ok": True}
    assert waited == [5]


def test_rate_limit_pauses_without_counting_as_a_failure(poller, monkeypatch):
    too_many = ApiError("rate_limited", 429, detail="Too many requests")
    # Each cycle's fast poll tries twice (api() retries once), then ChatGPT answers normally again.
    instance = poller({
        "/backend-api/conversations": [too_many] * (2 * RATE_LIMIT_ALERT_AFTER) + [{"items": []}],
        "/backend-api/gizmos/snorlax/sidebar": {"items": [], "cursor": None},
    })
    monkeypatch.setattr(instance, "sleep", lambda seconds: None)
    instance.next_sweep = float("inf")          # only the fast poll runs
    instance.next_restart = float("inf")        # no routine browser restart

    for cycle in range(1, RATE_LIMIT_ALERT_AFTER + 1):
        assert instance._cycle() == RATE_LIMIT_PAUSE_SECONDS
        assert instance.failures == 0 and instance.browser.starts == 0     # not an error, no browser restart
        expected = "problem" if cycle == RATE_LIMIT_ALERT_AFTER else "ok"   # the phone only hears after an hour
        assert instance.db.get_state("alert:rate_limited", "ok") == expected
    assert instance.db.get_state("alert:api_errors", "ok") == "ok"

    assert instance._cycle() < RATE_LIMIT_PAUSE_SECONDS                     # back to normal polling
    assert instance.rate_limited_cycles == 0
    assert instance.db.get_state("alert:rate_limited", "ok") == "ok"


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
