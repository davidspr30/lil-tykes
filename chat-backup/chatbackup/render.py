"""Turns a raw conversation (the JSON from /backend-api/conversation/{id}) into
things a person can read. No file or network access happens in this module.

A conversation's "mapping" is a tree of nodes. Editing a message or regenerating
an answer starts a new branch; ChatGPT shows only the branch that ends at
"current_node". We render that branch as the main transcript and every other
branch under "Alternate branches", so nothing that was ever said is lost.

What the transcript shows:
- user and assistant messages meant for the user (recipient "all")
- images ChatGPT generated, code it ran and the output of that code
- one-line notes for canvas edits, image prompts and memory updates
Hidden system context, web-search plumbing and other tool traffic are left out;
conversation.json always has the complete data.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, tzinfo
from typing import Any, Iterable, Mapping

from .chatgpt import clean_title, file_id_from_pointer, is_composite_pointer, parse_time

CHAT_URL = "https://chatgpt.com/c/"
SLUG_MAX = 50
FILENAME_MAX = 80

# Content types that are never shown, whoever sent them.
OMITTED_CONTENT_TYPES = {
    "user_editable_context", "model_editable_context", "tether_quote",
    "tether_browsing_display", "sonic_webpage", "tether_browsing_code",
}
# Tool messages are only shown when they carry one of these.
SHOWN_TOOL_CONTENT_TYPES = {"multimodal_text", "execution_output"}

CANVAS_EXTENSIONS = {
    "document": ".md", "code/python": ".py", "code/javascript": ".js", "code/typescript": ".ts",
    "code/html": ".html", "code/css": ".css", "code/java": ".java", "code/cpp": ".cpp",
    "code/c": ".c", "code/csharp": ".cs", "code/go": ".go", "code/rust": ".rs",
    "code/php": ".php", "code/ruby": ".rb", "code/sql": ".sql", "code/shell": ".sh",
    "code/bash": ".sh", "code/json": ".json", "code/yaml": ".yaml", "code/react": ".jsx",
    "webview": ".html",
}


@dataclass(frozen=True)
class FilePointer:
    """A file referenced by a conversation, before or after download."""
    file_id: str
    pointer: str            # what to hand to the download endpoints
    kind: str               # "image" (generated or pasted) or "attachment" (uploaded)
    name: str | None
    mime_type: str | None
    size: int | None


@dataclass(frozen=True)
class FileInfo:
    """What the renderer needs to know about a file on disk."""
    local_name: str | None
    status: str             # pending, done, failed or gone
    name: str | None = None


@dataclass
class CanvasDoc:
    name: str
    doc_type: str
    content: str
    node_ids: list[str] = field(default_factory=list)   # the messages that created and edited it
    replay_ok: bool = True
    failed_pattern: str | None = None
    filename: str = ""


@dataclass(frozen=True)
class Branch:
    fork_id: str | None     # the main-path node this branch splits off from; None if disconnected
    nodes: list[dict]       # the branch's own nodes, oldest first


# --- walking the message tree ------------------------------------------------------------

def message_of(node: Mapping[str, Any]) -> dict:
    return node.get("message") or {}


def role_of(node: Mapping[str, Any]) -> str:
    return (message_of(node).get("author") or {}).get("role") or ""


def content_of(node: Mapping[str, Any]) -> dict:
    return message_of(node).get("content") or {}


def content_type_of(node: Mapping[str, Any]) -> str:
    return content_of(node).get("content_type") or "text"


def recipient_of(node: Mapping[str, Any]) -> str:
    return message_of(node).get("recipient") or "all"


def metadata_of(node: Mapping[str, Any]) -> dict:
    return message_of(node).get("metadata") or {}


def create_time_of(node: Mapping[str, Any]) -> float | None:
    return parse_time(message_of(node).get("create_time"))


def leaves(mapping: Mapping[str, dict]) -> list[str]:
    """Node ids without children (children that are missing from the mapping don't count)."""
    return [node_id for node_id, node in mapping.items()
            if not any(child in mapping for child in node.get("children") or [])]


def newest_leaf(mapping: Mapping[str, dict]) -> str:
    best_id = None
    best_time = -1.0
    for node_id in leaves(mapping) or list(mapping):
        node_time = create_time_of(mapping[node_id]) or 0.0
        if best_id is None or node_time >= best_time:
            best_id, best_time = node_id, node_time
    assert best_id is not None
    return best_id


def node_with_id(mapping: Mapping[str, dict], node_id: str) -> dict:
    """The node as stored, guaranteed to carry its own id (older data sometimes leaves it out)."""
    node = mapping[node_id]
    if node.get("id") == node_id:
        return node
    return {**node, "id": node_id}


def main_path(mapping: Mapping[str, dict], current_node: str | None) -> list[dict]:
    """The nodes ChatGPT displays, root first. Falls back to the newest leaf when current_node is unusable."""
    if not mapping:
        return []
    node_id = current_node if current_node in mapping else newest_leaf(mapping)
    path: list[dict] = []
    seen: set[str] = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        path.append(node_with_id(mapping, node_id))
        node_id = mapping[node_id].get("parent")
    path.reverse()
    return path


def node_ids(nodes: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(node.get("id")) for node in nodes]


def alternate_paths(mapping: Mapping[str, dict], main_ids: list[str]) -> list[Branch]:
    """Every branch that is not on the main path, ordered by where it forks off."""
    main_set = set(main_ids)
    position = {node_id: index for index, node_id in enumerate(main_ids)}
    branches: list[Branch] = []
    for leaf_id in leaves(mapping):
        if leaf_id in main_set:
            continue
        chain: list[dict] = []
        seen: set[str] = set()
        fork_id = None
        node_id: str | None = leaf_id
        while node_id and node_id in mapping and node_id not in seen:
            if node_id in main_set:
                fork_id = node_id
                break
            seen.add(node_id)
            chain.append(node_with_id(mapping, node_id))
            node_id = mapping[node_id].get("parent")
        chain.reverse()
        branches.append(Branch(fork_id, chain))

    def sort_key(branch: Branch) -> tuple[int, float]:
        leaf_time = create_time_of(branch.nodes[-1]) if branch.nodes else None
        return (position.get(branch.fork_id, -1), leaf_time or 0.0)

    branches.sort(key=sort_key)
    return branches


def message_counts(conversation: Mapping[str, Any]) -> tuple[int, int]:
    """(nodes that carry a message, all nodes) across every branch."""
    mapping = conversation.get("mapping") or {}
    return sum(1 for node in mapping.values() if node.get("message")), len(mapping)


def model_of(conversation: Mapping[str, Any]) -> str | None:
    model = conversation.get("default_model_slug")
    if model:
        return str(model)
    for node in (conversation.get("mapping") or {}).values():
        if role_of(node) == "assistant" and metadata_of(node).get("model_slug"):
            return str(metadata_of(node)["model_slug"])
    return None


def is_streaming(conversation: Mapping[str, Any]) -> bool:
    """True while the displayed answer is still being written, so we fetch again later."""
    path = main_path(conversation.get("mapping") or {}, conversation.get("current_node"))
    if not path:
        return False
    status = message_of(path[-1]).get("status")
    # Every "finished_*" status is final. "finished_partial_completion" is an answer cut off for good
    # (for example by a usage limit); treating it as unfinished re-fetched the chat every 2 minutes forever.
    return status is not None and not str(status).startswith("finished")


# --- rendering ----------------------------------------------------------------------------

def format_time(timestamp: float | None, tz: tzinfo) -> str:
    if timestamp is None:
        return "unknown time"
    return datetime.fromtimestamp(timestamp, tz).strftime("%Y-%m-%d %H:%M %Z")


def message_heading(node: Mapping[str, Any], number: int | None) -> str:
    role = role_of(node)
    if role == "assistant":
        model = metadata_of(node).get("model_slug")
        label = f"Assistant ({model})" if model else "Assistant"
    elif role == "user":
        label = "User"
    else:
        name = (message_of(node).get("author") or {}).get("name")
        label = f"Tool ({name})" if name else "Tool"
    return f"## {number}. {label}" if number else f"## {label}"


def render_message(node: Mapping[str, Any], files: Mapping[str, FileInfo], tz: tzinfo,
                   number: int | None = None, canvas_names: Mapping[str, str] | None = None) -> str | None:
    """One message as Markdown, or None when it should not appear in the transcript."""
    if not node.get("message"):
        return None
    role = role_of(node)
    content_type = content_type_of(node)
    if metadata_of(node).get("is_visually_hidden_from_conversation"):
        return None
    if role == "system" or content_type in OMITTED_CONTENT_TYPES:
        return None
    if role == "tool" and content_type not in SHOWN_TOOL_CONTENT_TYPES:
        return None

    body = render_body(node, files, canvas_names or {})
    if body is None:
        return None

    parts = content_of(node).get("parts") or []
    image_ids = {file_id_from_pointer(str(part.get("asset_pointer") or "")) for part in parts
                 if isinstance(part, dict) and part.get("content_type") == "image_asset_pointer"}
    attachments = render_attachments(metadata_of(node).get("attachments") or [], files, image_ids)

    lines = [message_heading(node, number), f"_{format_time(create_time_of(node), tz)}_", "", body]
    if attachments:
        lines += ["", "Attachments:", attachments]
    return "\n".join(lines)


def render_body(node: Mapping[str, Any], files: Mapping[str, FileInfo], canvas_names: Mapping[str, str]) -> str | None:
    content = content_of(node)
    content_type = content_type_of(node)
    if content_type == "text":
        text = "\n".join(part for part in content.get("parts") or [] if isinstance(part, str))
        return text if text.strip() else None
    if content_type == "multimodal_text":
        return render_multimodal(content.get("parts") or [], files)
    if content_type == "code":
        return render_code(node, canvas_names)
    if content_type == "execution_output":
        return "_Output:_\n\n```\n" + str(content.get("text") or "") + "\n```"
    if content_type == "thoughts":
        return render_thoughts(content.get("thoughts") or [])
    if content_type == "reasoning_recap":
        text = str(content.get("content") or "")
        return f"_{text}_" if text.strip() else None
    if content_type == "system_error":
        return f"_(Error: {content.get('name')}: {content.get('text')})_"
    return f"_({content_type} message omitted; see conversation.json)_"


def render_multimodal(parts: list, files: Mapping[str, FileInfo]) -> str | None:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if part.strip():
                chunks.append(part)
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("content_type") or ""
        if part_type == "image_asset_pointer":
            chunks.append(render_image(part, files))
        elif part_type == "audio_transcription":
            chunks.append(f"_(Voice message)_ {part.get('text') or ''}")
        elif part_type.endswith("asset_pointer"):
            chunks.append("_(Voice or video clip; not saved)_")
        else:
            chunks.append(f"_({part_type} part omitted)_")
    return "\n\n".join(chunks) or None


def render_image(part: Mapping[str, Any], files: Mapping[str, FileInfo]) -> str:
    pointer = str(part.get("asset_pointer") or "")
    file_id = file_id_from_pointer(pointer)
    info = files.get(file_id)
    if info and info.status == "done" and info.local_name:
        line = f"![image](files/{info.local_name})"
    elif is_composite_pointer(pointer):
        line = "_(Document preview image omitted)_"
    else:
        line = f"_(Image {file_id}: not downloaded: {info.status if info else 'pending'})_"
    metadata = part.get("metadata") or {}
    prompt = (metadata.get("dalle") or {}).get("prompt") or (metadata.get("generation") or {}).get("prompt")
    if prompt:
        line += f"\n\n_Image prompt:_ {prompt}"
    return line


def render_code(node: Mapping[str, Any], canvas_names: Mapping[str, str]) -> str | None:
    content = content_of(node)
    recipient = recipient_of(node)
    text = str(content.get("text") or "")
    language = str(content.get("language") or "")
    node_id = str(node.get("id"))
    if recipient.startswith("canmore.create_textdoc"):
        name = parse_json_object(text).get("name") or "untitled"
        return f'_(Canvas "{name}" created; see canvas/{canvas_names.get(node_id, "")})_'
    if recipient.startswith("canmore.update_textdoc"):
        return f"_(Canvas edited; see canvas/{canvas_names.get(node_id, '')})_"
    if recipient.startswith("canmore."):
        return None
    if recipient.startswith("dalle") or "image" in recipient:
        payload = parse_json_object(text)
        prompts = payload.get("prompts") or payload.get("prompt") or text
        if isinstance(prompts, list):
            prompts = "; ".join(str(prompt) for prompt in prompts)
        return f"_Image prompt:_ {prompts}"
    if recipient == "bio":
        return f"_(Memory update: {text})_"
    if recipient in ("all", "python"):
        fence_language = "" if language in ("", "unknown") else language
        return f"```{fence_language}\n{text}\n```"
    return f"_(Call to {recipient} omitted; see conversation.json)_"


def render_thoughts(thoughts: list) -> str | None:
    lines = ["> **Reasoning**"]
    for thought in thoughts:
        if not isinstance(thought, dict):
            continue
        for key in ("summary", "content"):
            value = thought.get(key)
            if value:
                lines.extend("> " + line for line in str(value).splitlines())
                lines.append(">")
    return "\n".join(lines) if len(lines) > 1 else None


def render_attachments(attachments: list, files: Mapping[str, FileInfo], already_shown: set[str]) -> str:
    lines: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        file_id = str(attachment.get("id") or "")
        if not file_id or file_id in already_shown:
            continue
        name = str(attachment.get("name") or file_id)
        info = files.get(file_id)
        if info and info.status == "done" and info.local_name:
            lines.append(f"- [{name}](files/{info.local_name})")
        else:
            lines.append(f"- {name} (not downloaded: {info.status if info else 'pending'})")
    return "\n".join(lines)


def build_transcript(conversation: Mapping[str, Any], files: Mapping[str, FileInfo], project_title: str | None,
                     deleted_at: float | None, tz: tzinfo, canvas_docs: list[CanvasDoc] | None = None) -> str:
    """The whole transcript.md for one conversation."""
    mapping = conversation.get("mapping") or {}
    main_nodes = main_path(mapping, conversation.get("current_node"))
    main_ids = node_ids(main_nodes)
    docs = canvas_docs if canvas_docs is not None else extract_canvas_docs(conversation, main_ids)
    canvas_names = {node_id: doc.filename for doc in docs for node_id in doc.node_ids}
    branches = alternate_paths(mapping, main_ids)

    title = clean_title(conversation.get("title")) or "Untitled chat"
    conversation_id = conversation.get("conversation_id") or conversation.get("id") or ""
    header = [f"# {title}", "",
              f"- Link: {CHAT_URL}{conversation_id}",
              f"- Created: {format_time(parse_time(conversation.get('create_time')), tz)}",
              f"- Updated: {format_time(parse_time(conversation.get('update_time')), tz)}"]
    model = model_of(conversation)
    if model:
        header.append(f"- Model: {model}")
    if project_title:
        header.append(f"- Project: {project_title}")
    if conversation.get("is_archived"):
        header.append("- Archived: yes")
    if deleted_at:
        header.append(f"- Status: deleted from ChatGPT (noticed {format_time(deleted_at, tz)})")
    if files:
        done = sum(1 for info in files.values() if info.status == "done")
        header.append(f"- Files: {done} of {len(files)} downloaded (files/)")
    if docs:
        header.append(f"- Canvas documents: {len(docs)} (canvas/)")
    if branches:
        header.append(f"- Alternate branches: {len(branches)}")
    header += ["", "_Search results, tool calls and hidden system messages are left out here; "
                   "conversation.json has everything._", "", "---", ""]

    body: list[str] = []
    shown_before: dict[str, int] = {}   # node id -> how many messages were shown up to and including it
    shown = 0
    for node in main_nodes:
        rendered = render_message(node, files, tz, shown + 1, canvas_names)
        if rendered is not None:
            shown += 1
            body += [rendered, ""]
        shown_before[str(node.get("id"))] = shown

    if branches:
        body += ["# Alternate branches", ""]
        for branch in branches:
            after = shown_before.get(branch.fork_id or "", 0)
            body += [f"## Branch after message {after}" if after else "## Branch from the start", ""]
            for node in branch.nodes:
                rendered = render_message(node, files, tz, None, canvas_names)
                if rendered is not None:
                    body += [rendered, ""]

    return "\n".join(header + body).rstrip() + "\n"


# --- files and canvas docs ------------------------------------------------------------------

def extract_file_pointers(conversation: Mapping[str, Any]) -> list[FilePointer]:
    """Every downloadable file the conversation refers to, across all branches, one entry per file."""
    found: dict[str, FilePointer] = {}
    for node in (conversation.get("mapping") or {}).values():
        message = node.get("message") or {}
        for part in (message.get("content") or {}).get("parts") or []:
            if not isinstance(part, dict) or part.get("content_type") != "image_asset_pointer":
                continue
            pointer = str(part.get("asset_pointer") or "")
            if not pointer or is_composite_pointer(pointer):
                continue
            file_id = file_id_from_pointer(pointer)
            if file_id not in found:
                found[file_id] = FilePointer(file_id, pointer, "image", None, None, part.get("size_bytes"))
        for attachment in (message.get("metadata") or {}).get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            file_id = str(attachment.get("id") or "")
            if not file_id:
                continue
            existing = found.get(file_id)
            if existing is None:
                found[file_id] = FilePointer(file_id, file_id, "attachment", attachment.get("name"),
                                             attachment.get("mime_type"), attachment.get("size"))
            else:
                found[file_id] = replace(existing, name=attachment.get("name") or existing.name,
                                         mime_type=attachment.get("mime_type") or existing.mime_type,
                                         size=attachment.get("size") or existing.size)
    return list(found.values())


def parse_json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def extract_canvas_docs(conversation: Mapping[str, Any], main_ids: list[str]) -> list[CanvasDoc]:
    """Canvas documents created on the main path, with later edits replayed onto them."""
    mapping = conversation.get("mapping") or {}
    docs: list[CanvasDoc] = []
    current: CanvasDoc | None = None
    for node_id in main_ids:
        node = mapping.get(node_id) or {}
        if content_type_of(node) != "code":
            continue
        recipient = recipient_of(node)
        text = str(content_of(node).get("text") or "")
        if recipient.startswith("canmore.create_textdoc"):
            payload = parse_json_object(text)
            current = CanvasDoc(
                name=str(payload.get("name") or f"canvas-{len(docs) + 1}"),
                doc_type=str(payload.get("type") or "document"),
                content=str(payload.get("content") if payload else text),
                node_ids=[node_id],
            )
            docs.append(current)
        elif recipient.startswith("canmore.update_textdoc") and current is not None:
            current.node_ids.append(node_id)
            if not current.replay_ok:
                continue    # once an edit failed, later edits would land on the wrong text
            updates = parse_json_object(text).get("updates")
            if not isinstance(updates, list):
                current.replay_ok = False
                current.failed_pattern = "(unreadable edit)"
                continue
            current.content, ok, failed_pattern = apply_canvas_updates(current.content, updates)
            if not ok:
                current.replay_ok = False
                current.failed_pattern = failed_pattern
    assign_canvas_filenames(docs)
    return docs


def apply_canvas_updates(content: str, updates: list) -> tuple[str, bool, str | None]:
    """Replay canvas edits: each is a regex pattern with a replacement. Stops at the first edit that doesn't apply."""
    for update in updates:
        if not isinstance(update, dict):
            return content, False, "(unreadable edit)"
        pattern = update.get("pattern")
        replacement = str(update.get("replacement") or "")
        if not isinstance(pattern, str):
            return content, False, str(pattern)
        if pattern == ".*":                     # ChatGPT's way of rewriting the whole document
            content = replacement
            continue
        try:
            regex = re.compile(pattern, re.DOTALL)
        except re.error:
            return content, False, pattern
        count = 0 if update.get("multiple") else 1
        content, replaced = regex.subn(lambda match: replacement, content, count=count)
        if replaced == 0:
            return content, False, pattern
    return content, True, None


def canvas_filename(doc: CanvasDoc) -> str:
    return slugify(doc.name) + CANVAS_EXTENSIONS.get(doc.doc_type, ".txt")


def assign_canvas_filenames(docs: list[CanvasDoc]) -> None:
    used: set[str] = set()
    for doc in docs:
        base = canvas_filename(doc)
        stem, dot, extension = base.rpartition(".")
        candidate = base
        counter = 2
        while candidate in used:
            candidate = f"{stem}-{counter}{dot}{extension}"
            counter += 1
        used.add(candidate)
        doc.filename = candidate


# --- names -----------------------------------------------------------------------------------

def slugify(title: str) -> str:
    """Folder-friendly version of a title: ascii, lowercase, dashes, at most SLUG_MAX chars."""
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    text = text[:SLUG_MAX].rstrip("-")
    return text or "untitled"


def safe_filename(name: str) -> str:
    """A file name that works on Linux, Windows and Google Drive, keeping the extension."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._ ")
    stem, dot, extension = text.rpartition(".")
    if not dot or not stem or len(extension) > 10:
        stem, extension = text, ""
    stem = stem[: FILENAME_MAX - len(extension) - 1] or "file"
    return f"{stem}.{extension}" if extension else stem


# --- index --------------------------------------------------------------------------------------

def _cell(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def build_index_md(rows: Iterable[Any], project_titles: Mapping[str, str], tz: tzinfo, now: float | None = None) -> str:
    """INDEX.md: one table row per chat, newest first. Rows are database rows (duck-typed)."""
    rows = sorted(rows, key=lambda row: row.update_time or 0.0, reverse=True)
    deleted = sum(1 for row in rows if row.deleted_at)
    lines = ["# ChatGPT archive", "",
             f"Updated {format_time(now or time.time(), tz)}. {len(rows)} chats, "
             f"{deleted} deleted in ChatGPT but kept here.", "",
             "| Updated | Created | Title | Project | Status | Messages |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        title = _cell(row.title) or "Untitled chat"
        link = f"[{title}]({row.folder}/transcript.md)" if row.folder else title
        status = "deleted" if row.deleted_at else ("archived" if row.is_archived else "active")
        project = _cell(project_titles.get(row.gizmo_id, row.gizmo_id)) if row.gizmo_id else ""
        updated = datetime.fromtimestamp(row.update_time or 0, tz).strftime("%Y-%m-%d %H:%M")
        created = datetime.fromtimestamp(row.create_time or 0, tz).strftime("%Y-%m-%d")
        lines.append(f"| {updated} | {created} | {link} | {project} | {status} | {row.message_count or ''} |")
    return "\n".join(lines) + "\n"


def build_index_csv(rows: Iterable[Any], project_titles: Mapping[str, str], tz: tzinfo) -> str:
    def iso(timestamp: float | None) -> str:
        return datetime.fromtimestamp(timestamp, tz).isoformat(timespec="seconds") if timestamp else ""

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "created", "updated", "folder", "project", "archived",
                     "deleted_at", "message_count", "model"])
    for row in sorted(rows, key=lambda row: row.update_time or 0.0, reverse=True):
        writer.writerow([row.id, clean_title(row.title), iso(row.create_time), iso(row.update_time),
                         row.folder or "", project_titles.get(row.gizmo_id, row.gizmo_id or "") if row.gizmo_id else "",
                         "yes" if row.is_archived else "no", iso(row.deleted_at),
                         row.message_count if row.message_count is not None else "", row.model or ""])
    return output.getvalue()
