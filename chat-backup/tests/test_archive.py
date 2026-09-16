import json
from zoneinfo import ZoneInfo

import pytest

from chatbackup.archive import Archive, extension_for, write_atomic
from chatbackup.render import CanvasDoc

CREATED = 1789300000.0      # 2026-09-13 in New York
CHAT_ID = "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"


@pytest.fixture
def archive(tmp_path):
    return Archive(tmp_path / "archive", ZoneInfo("America/New_York"))


def test_folder_for_and_rename(archive):
    folder = archive.folder_for(CHAT_ID, "New chat", CREATED, None)
    assert folder == "2026-09/2026-09-13_new-chat_abcdef12"
    archive.write_transcript(folder, "hello")

    renamed = archive.folder_for(CHAT_ID, "Budget plan", CREATED, folder)
    assert renamed == "2026-09/2026-09-13_budget-plan_abcdef12"
    assert not (archive.root / folder).exists()
    assert (archive.root / renamed / "transcript.md").read_text() == "hello"

    archive.write_transcript("2026-09/2026-09-13_third_abcdef12", "x")     # a collision keeps the current name
    assert archive.folder_for(CHAT_ID, "Third", CREATED, renamed) == renamed


def test_history_kept_only_when_messages_disappear(archive):
    folder = "2026-09/2026-09-13_x_abcdef12"
    archive.write_conversation(folder, {"update_time": CREATED, "mapping": {"a": {}, "b": {}}})
    archive.write_conversation(folder, {"update_time": CREATED + 60, "mapping": {"a": {}, "b": {}, "c": {}}})
    assert not (archive.root / folder / "history").exists()

    archive.write_conversation(folder, {"update_time": CREATED + 120, "mapping": {"a": {}}})
    history = list((archive.root / folder / "history").iterdir())
    assert len(history) == 1 and history[0].name.startswith("conversation.2026-09-13T")
    assert set(json.loads(history[0].read_text())["mapping"]) == {"a", "b", "c"}
    assert set(archive.read_conversation(folder)["mapping"]) == {"a"}
    assert archive.read_conversation("2026-09/nothing-here") is None


def test_write_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "sub" / "file.txt"
    write_atomic(target, b"one")
    write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert [path.name for path in target.parent.iterdir()] == ["file.txt"]


def test_deleted_marker(archive):
    folder = "2026-09/2026-09-13_x_abcdef12"
    archive.write_deleted_marker(folder, CREATED)
    marker = archive.root / folder / "DELETED.txt"
    assert "deleted in ChatGPT" in marker.read_text()
    archive.remove_deleted_marker(folder)
    assert not marker.exists()
    archive.remove_deleted_marker(folder)      # removing it twice is fine


def test_local_names(archive):
    assert archive.local_name_for("file-Abc123", "attachment", "report: final?.pdf", "application/pdf", None) == "file-Abc123_report_final_.pdf"
    assert archive.local_name_for("file-Abc123", "image", "photo.png", "image/png", "image/png") == "file-Abc123_photo.png"
    assert archive.local_name_for("file_9f8e", "image", None, None, "image/png; charset=binary") == "file_9f8e.png"
    assert archive.local_name_for("file_9f8e", "image", None, "image/jpeg", "") == "file_9f8e.jpg"
    assert archive.local_name_for("file_9f8e", "image", None, None, "application/x-unknown-thing") == "file_9f8e.bin"
    assert extension_for("image/webp") == ".webp" and extension_for(None) is None


def test_canvas_files_and_index(archive):
    folder = "2026-09/2026-09-13_x_abcdef12"
    good = CanvasDoc("Plan", "document", "text", filename="plan.md")
    bad = CanvasDoc("Code", "code/python", "print(1)", replay_ok=False, failed_pattern="zzz", filename="code.py")
    archive.write_canvas(folder, [good, bad])
    canvas = archive.root / folder / "canvas"
    assert (canvas / "plan.md").read_text() == "text"
    assert "zzz" in (canvas / "code.py.replay-warning.txt").read_text()
    assert not (canvas / "plan.md.replay-warning.txt").exists()

    archive.write_file(folder, "file-1.png", b"\x89PNG")
    assert (archive.root / folder / "files" / "file-1.png").read_bytes() == b"\x89PNG"

    archive.write_index("# idx", "id\n")
    assert (archive.root / "INDEX.md").read_text() == "# idx"
    assert (archive.root / "index.csv").read_text() == "id\n"
    assert archive.free_bytes() > 0
