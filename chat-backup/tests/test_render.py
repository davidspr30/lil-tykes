import csv
import io
from types import SimpleNamespace

import pytest

from chatbackup import render
from chatbackup.render import CanvasDoc, FileInfo

BIG_IMAGE = "file_00000000842871f5b6a1bab8e3499232"


def test_main_path_follows_current_node(branch_conversation):
    ids = render.node_ids(render.main_path(branch_conversation["mapping"], "a1"))
    assert ids == ["root", "sys", "u1", "as1", "u2", "recap", "a1"]


def test_main_path_falls_back_to_newest_leaf(files_conversation):
    ids = render.node_ids(render.main_path(files_conversation["mapping"], "does-not-exist"))
    assert ids[-1] == "u2"
    assert render.main_path({}, None) == []


def test_nodes_without_an_id_field_still_get_one():
    mapping = {"r": {"parent": None, "children": ["x"], "message": None},
               "x": {"parent": "r", "children": [], "message": {"author": {"role": "user"}, "create_time": 1.0,
                                                                "content": {"content_type": "text", "parts": ["hi"]}}}}
    assert render.node_ids(render.main_path(mapping, "x")) == ["r", "x"]
    assert render.alternate_paths(mapping, ["r", "x"]) == []


def test_alternate_paths_fork_from_the_main_path(branch_conversation):
    mapping = branch_conversation["mapping"]
    branches = render.alternate_paths(mapping, render.node_ids(render.main_path(mapping, "a1")))
    assert len(branches) == 1
    assert branches[0].fork_id == "u2"
    assert render.node_ids(branches[0].nodes) == ["b1", "b2"]


def test_transcript_shows_main_thread_then_branches(branch_conversation, tz):
    text = render.build_transcript(branch_conversation, {}, None, None, tz)
    assert text.startswith("# Joke time\n")
    assert "- Link: https://chatgpt.com/c/11111111-2222-3333-4444-555555555555" in text
    assert "- Model: gpt-5" in text
    assert "## 1. User" in text and "Hello" in text
    assert "System" not in text
    assert "_Thought for 3 seconds_" in text
    assert text.index("chicken") < text.index("# Alternate branches") < text.index("Knock knock")
    assert "## Branch after message 3" in text
    assert "search results here" not in text          # tool plumbing stays out
    assert "- Alternate branches: 1" in text


def test_unknown_content_type_gets_a_placeholder(tz):
    conversation = {"title": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [], "message": {
            "author": {"role": "assistant"}, "create_time": 1.0,
            "content": {"content_type": "computer_output", "text": "?"}, "metadata": {}, "recipient": "all"}}}}
    text = render.build_transcript(conversation, {}, None, None, tz)
    assert "_(computer_output message omitted; see conversation.json)_" in text


def test_code_and_output_messages(tz):
    conversation = {"title": "code", "current_node": "o", "mapping": {
        "c": {"id": "c", "parent": None, "children": ["o"], "message": {
            "author": {"role": "assistant"}, "create_time": 1.0, "recipient": "python",
            "content": {"content_type": "code", "language": "python", "text": "print(1)"}, "metadata": {}}},
        "o": {"id": "o", "parent": "c", "children": [], "message": {
            "author": {"role": "tool"}, "create_time": 2.0, "recipient": "all",
            "content": {"content_type": "execution_output", "text": "1"}, "metadata": {}}}}}
    text = render.build_transcript(conversation, {}, None, None, tz)
    assert "```python\nprint(1)\n```" in text
    assert "_Output:_\n\n```\n1\n```" in text


def test_images_attachments_and_prompts(files_conversation, tz):
    files = {
        "file-Abc123": FileInfo("file-Abc123_photo.png", "done", "photo.png"),
        BIG_IMAGE: FileInfo(BIG_IMAGE + ".png", "done"),
        "file-Rep456": FileInfo(None, "failed", "report: final?.pdf"),
    }
    text = render.build_transcript(files_conversation, files, "Work", None, tz)
    assert "![image](files/file-Abc123_photo.png)" in text
    assert f"![image](files/{BIG_IMAGE}.png)" in text
    assert "_Image prompt:_ An oil painting of a cat" in text
    assert "- report: final?.pdf (not downloaded: failed)" in text
    assert "[photo.png]" not in text                    # shown as an image, not listed a second time
    assert "(Document preview image omitted)" in text
    assert "- Project: Work" in text
    assert "- Files: 2 of 3 downloaded (files/)" in text


def test_missing_image_is_explained(files_conversation, tz):
    text = render.build_transcript(files_conversation, {}, None, None, tz)
    assert f"_(Image {BIG_IMAGE}: not downloaded: pending)_" in text


def test_transcript_header_marks_deleted(files_conversation, tz):
    text = render.build_transcript(files_conversation, {}, None, 1789400000.0, tz)
    assert "- Status: deleted from ChatGPT (noticed 2026-09-14" in text


def test_extract_file_pointers(files_conversation):
    pointers = {pointer.file_id: pointer for pointer in render.extract_file_pointers(files_conversation)}
    assert set(pointers) == {"file-Abc123", "file-Rep456", BIG_IMAGE}
    assert pointers["file-Abc123"].kind == "image"
    assert pointers["file-Abc123"].pointer == "file-service://file-Abc123"
    assert pointers["file-Abc123"].name == "photo.png"     # merged in from the attachment record
    assert pointers["file-Rep456"].kind == "attachment"
    assert pointers["file-Rep456"].mime_type == "application/pdf"
    assert pointers[BIG_IMAGE].pointer == "sediment://" + BIG_IMAGE


def test_canvas_replay(canvas_conversation):
    mapping = canvas_conversation["mapping"]
    docs = render.extract_canvas_docs(canvas_conversation, render.node_ids(render.main_path(mapping, "a3")))
    assert len(docs) == 1
    doc = docs[0]
    assert doc.content == "Day 1: Lisbon\nDay 2: Sintra"
    assert doc.replay_ok is False
    assert doc.failed_pattern == "Day 3: Coimbra"
    assert doc.filename == "trip-plan.md"
    assert doc.node_ids == ["c1", "c2", "c3"]


def test_canvas_notes_in_transcript(canvas_conversation, tz):
    text = render.build_transcript(canvas_conversation, {}, None, None, tz)
    assert '_(Canvas "Trip plan" created; see canvas/trip-plan.md)_' in text
    assert "_(Canvas edited; see canvas/trip-plan.md)_" in text
    assert "- Canvas documents: 1 (canvas/)" in text


def test_apply_canvas_updates():
    assert render.apply_canvas_updates("a b a", [{"pattern": "a", "replacement": "x", "multiple": True}]) == ("x b x", True, None)
    assert render.apply_canvas_updates("a b a", [{"pattern": "a", "replacement": "x"}]) == ("x b a", True, None)
    assert render.apply_canvas_updates("old\ntext", [{"pattern": ".*", "replacement": "new"}]) == ("new", True, None)
    assert render.apply_canvas_updates("abc", [{"pattern": "(", "replacement": ""}]) == ("abc", False, "(")
    assert render.apply_canvas_updates("abc", [{"pattern": "zzz", "replacement": ""}]) == ("abc", False, "zzz")
    assert render.apply_canvas_updates("abc", ["nonsense"]) == ("abc", False, "(unreadable edit)")


def test_canvas_filenames():
    docs = [CanvasDoc("My Script", "code/python", ""), CanvasDoc("My Script", "code/python", ""),
            CanvasDoc("Notes", "mystery", "")]
    render.assign_canvas_filenames(docs)
    assert [doc.filename for doc in docs] == ["my-script.py", "my-script-2.py", "notes.txt"]


def test_is_streaming(canvas_conversation, branch_conversation):
    assert render.is_streaming(canvas_conversation) is True
    assert render.is_streaming(branch_conversation) is False


def test_cut_off_answer_is_not_streaming(branch_conversation):
    last = branch_conversation["mapping"][branch_conversation["current_node"]]["message"]
    last["status"] = "finished_partial_completion"
    assert render.is_streaming(branch_conversation) is False


def test_message_counts_and_model(branch_conversation):
    assert render.message_counts(branch_conversation) == (8, 9)
    assert render.model_of(branch_conversation) == "gpt-5"
    only_metadata = {"mapping": {"a": {"message": {"author": {"role": "assistant"}, "metadata": {"model_slug": "o3"}}}}}
    assert render.model_of(only_metadata) == "o3"
    assert render.model_of({}) is None


@pytest.mark.parametrize("title, slug", [
    ("Budget\n", "budget"),
    ("Café — Ünïcode!", "cafe-unicode"),
    ("", "untitled"),
    ("New chat", "new-chat"),
    ("a" * 80, "a" * 50),
    ("--x--", "x"),
])
def test_slugify(title, slug):
    assert render.slugify(title) == slug


@pytest.mark.parametrize("name, expected", [
    ("report: final?.pdf", "report_final_.pdf"),
    ("photo.png", "photo.png"),
    ("", "file"),
    ("...hidden", "hidden"),
    ("a" * 200 + ".txt", "a" * 76 + ".txt"),
    ("no extension", "no_extension"),
])
def test_safe_filename(name, expected):
    assert render.safe_filename(name) == expected


def make_row(**overrides):
    row = dict(id="c-1", title="Budget | plan\nq", create_time=1789300000.0, update_time=1789303600.0,
               folder="2026-09/2026-09-13_budget-plan-q_c1", gizmo_id="g-p-aaaa", is_archived=False,
               deleted_at=None, message_count=4, model="gpt-5")
    row.update(overrides)
    return SimpleNamespace(**row)


def test_index_md_and_csv(tz):
    rows = [make_row(),
            make_row(id="c-2", title="Gone", folder=None, gizmo_id=None, deleted_at=1789400000.0,
                     update_time=1789400000.0, message_count=None)]
    index = render.build_index_md(rows, {"g-p-aaaa": "Work"}, tz, now=1789400000.0)
    assert "2 chats, 1 deleted in ChatGPT but kept here." in index
    assert "[Budget \\| plan q](2026-09/2026-09-13_budget-plan-q_c1/transcript.md)" in index
    assert "| Work | active | 4 |" in index
    assert "| Gone |  | deleted |  |" in index
    assert index.index("Gone") < index.index("Budget")     # newest first

    parsed = list(csv.DictReader(io.StringIO(render.build_index_csv(rows, {"g-p-aaaa": "Work"}, tz))))
    assert parsed[0]["id"] == "c-2" and parsed[0]["deleted_at"].startswith("2026-09-14")
    assert parsed[1]["project"] == "Work" and parsed[1]["title"] == "Budget | plan q"
    assert parsed[1]["archived"] == "no" and parsed[1]["model"] == "gpt-5"
