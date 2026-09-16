import sqlite3

import pytest

from chatbackup.chatgpt import ListItem
from chatbackup.db import Database
from chatbackup.render import FilePointer


def item(conversation_id="c-1", title="Budget", update_time=1000.0, is_archived=False, gizmo_id=None):
    return ListItem(id=conversation_id, title=title, create_time=900.0, update_time=update_time,
                    is_archived=is_archived, gizmo_id=gizmo_id)


def fetched(db, conversation_id, at):
    db.mark_fetched(conversation_id, title="Budget", fetched_update_time=at, folder="f", model="gpt-5",
                    message_count=1, node_count=2, now=at)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


def test_pending_only_on_a_real_change(db):
    assert db.upsert_listed(item(), 10.0) == "new"
    assert db.get("c-1").pending is True
    assert db.upsert_listed(item(update_time=1003.0), 10.5) == "same"     # already waiting for its first fetch
    assert db.get("c-1").pending is True
    fetched(db, "c-1", 1000.0)
    assert db.get("c-1").pending is False

    assert db.upsert_listed(item(update_time=1000.5), 11.0) == "same"      # inside the 1 s tolerance
    assert db.get("c-1").pending is False
    assert db.upsert_listed(item(update_time=1002.0), 12.0) == "changed"
    assert db.get("c-1").pending is True
    fetched(db, "c-1", 1002.0)

    assert db.upsert_listed(item(update_time=1002.0, title="Budget 2"), 13.0) == "changed"
    assert db.upsert_listed(item(update_time=1002.0, title="Budget 2"), 14.0) == "same"
    assert db.get("c-1").pending is True                                  # still waiting for the fetch
    fetched(db, "c-1", 1002.0)
    assert db.upsert_listed(item(update_time=1002.0, title="Budget", is_archived=True), 15.0) == "changed"
    fetched(db, "c-1", 1002.0)
    assert db.upsert_listed(item(update_time=1002.0, gizmo_id="g-p-1"), 16.0) == "changed"


def test_pending_skips_chats_without_recent_activity(db):
    def listed(chat_id, created, updated):
        db.upsert_listed(ListItem(id=chat_id, title=chat_id, create_time=created, update_time=updated,
                                  is_archived=False, gizmo_id=None), 1.0)

    listed("untouched", created=100.0, updated=200.0)
    listed("old-but-used", created=100.0, updated=5000.0)      # an old chat you used again recently
    listed("new", created=4000.0, updated=4000.0)
    assert [row.id for row in db.pending_conversations(None, now=1.0, active_since=3000.0)] == ["old-but-used", "new"]
    assert db.stats_since(0.0, active_since=3000.0)["pending"] == 2
    assert db.stats_since(0.0)["pending"] == 3


def test_deletion_detection_and_reappearance(db):
    db.upsert_listed(item("c-1"), 100.0)
    db.upsert_listed(item("c-2"), 100.0)
    fetched(db, "c-1", 1000.0)
    fetched(db, "c-2", 1000.0)

    sweep_started = 200.0
    db.upsert_listed(item("c-1"), 201.0)             # only c-1 is still listed
    assert [row.id for row in db.not_seen_since(sweep_started)] == ["c-2"]

    db.mark_deleted("c-2", 250.0)
    row = db.get("c-2")
    assert row.deleted_at == 250.0 and row.pending is False
    assert "c-2" not in [row.id for row in db.not_seen_since(300.0)]      # deleted chats are not reported again
    assert [row.id for row in db.pending_conversations(None, 999.0)] == []

    assert db.upsert_listed(item("c-2"), 300.0) == "reappeared"
    row = db.get("c-2")
    assert row.deleted_at is None and row.pending is True


def test_pending_order_and_retry_backoff(db):
    db.upsert_listed(item("old", update_time=10.0), 1.0)
    db.upsert_listed(item("new", update_time=20.0), 1.0)
    assert [row.id for row in db.pending_conversations(None, now=5.0)] == ["new", "old"]
    assert [row.id for row in db.pending_conversations(1, now=5.0)] == ["new"]
    assert [row.id for row in db.pending_conversations(None, now=5.0, updated_after=15.0)] == ["new"]
    assert [row.id for row in db.pending_conversations(None, now=5.0, updated_before=15.0)] == ["old"]

    db.mark_fetch_failed("new", "boom", now=100.0)
    assert [row.id for row in db.pending_conversations(None, now=100.0)] == ["old"]
    assert [row.id for row in db.pending_conversations(None, now=100.0 + 601)] == ["new", "old"]
    assert db.get("new").last_error == "boom"

    db.mark_fetch_failed("new", "boom", now=100.0)
    db.mark_fetch_failed("new", "boom", now=100.0)
    row = db.get("new")
    assert row.fetch_failures == 3 and row.not_before == 100.0 + 86400


def test_files_lifecycle(db):
    pointer = FilePointer("file-1", "file-1", "attachment", "a.pdf", "application/pdf", 10)
    db.upsert_file("c-1", pointer)
    db.upsert_file("c-1", pointer)
    assert [file.file_id for file in db.files_to_download("c-1")] == ["file-1"]

    db.mark_file_failed("c-1", "file-1", "network", permanent=False)
    assert db.files_for("c-1")[0].status == "failed" and db.files_to_download("c-1")
    db.mark_file_failed("c-1", "file-1", "network", permanent=False)
    db.mark_file_failed("c-1", "file-1", "network", permanent=False)
    assert db.files_for("c-1")[0].status == "gone" and db.files_to_download("c-1") == []

    db.upsert_file("c-1", FilePointer("file-2", "sediment://file-2", "image", None, None, None))
    db.mark_file_done("c-1", "file-2", "file-2.png", now=50.0)
    done = next(file for file in db.files_for("c-1") if file.file_id == "file-2")
    assert done.status == "done" and done.local_name == "file-2.png" and done.downloaded_at == 50.0
    assert db.files_to_download("c-1") == []

    db.upsert_file("c-1", FilePointer("file-3", "file-3", "attachment", None, None, None))
    db.mark_file_failed("c-1", "file-3", "404", permanent=True)
    assert next(file.status for file in db.files_for("c-1") if file.file_id == "file-3") == "gone"

    db.upsert_file("c-1", FilePointer("file-2", "sediment://file-2", "image", "pic.png", "image/png", 5))
    assert next(file.name for file in db.files_for("c-1") if file.file_id == "file-2") == "pic.png"


def test_state_stats_projects_and_snapshot(db, tmp_path):
    assert db.get_state("x") is None and db.get_state("x", "d") == "d"
    db.set_state("x", "1")
    db.set_state("x", "2")
    assert db.get_state("x") == "2"

    db.upsert_listed(item("c-1"), 100.0)
    fetched(db, "c-1", 1000.0)
    stats = db.stats_since(50.0)
    assert stats["total"] == 1 and stats["new"] == 1 and stats["fetched"] == 1
    assert stats["pending"] == 0 and stats["deleted"] == 0 and stats["failing"] == 0

    db.upsert_project("g-p-1", "Work", 1.0)
    db.upsert_project("g-p-1", "Work 2", 2.0)
    assert db.project_titles() == {"g-p-1": "Work 2"}
    assert db.project_title("g-p-1") == "Work 2" and db.project_title(None) is None

    snapshot = tmp_path / "archive" / "snap.sqlite3"
    db.snapshot_to(snapshot)
    copy = sqlite3.connect(snapshot)
    assert copy.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    copy.close()
    assert [path.name for path in snapshot.parent.iterdir()] == ["snap.sqlite3"]
