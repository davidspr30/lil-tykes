"""What we know about ChatGPT's internal web API.

Nothing here touches the network. Every function that needs the API takes an
``api_get`` callable: give it a chatgpt.com path such as
``/backend-api/conversations?offset=0`` and it returns the decoded JSON, or
raises ApiError. The browser module provides the real one; tests pass a fake.

These endpoints are unofficial and undocumented. They were verified in
September 2026 and can change without notice, which is why every response is
also written to disk raw before we try to interpret it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.parse import quote

ApiGet = Callable[[str], Any]

PAGE_SIZE = 100          # the server answers 422 to anything above 100
RECENT_PAGE_SIZE = 50
PROJECT_PAGE_SIZE = 50   # a Project's chat list answers 422 to anything above 50
MAX_PAGES = 200          # safety stop so a misbehaving server can't make us loop forever

CHALLENGE_MARKERS = ("just a moment", "enable javascript and cookies", "cdn-cgi/challenge-platform")
LOGIN_URL_MARKERS = ("/auth/", "auth.openai.com", "auth0.openai.com")


class ApiError(Exception):
    """An API call failed in a way the poller has to react to.

    kind is one of:
      token_expired   401: reload the page to get a fresh bearer token
      login_required  the saved session is dead: a human must log in again
      challenge       Cloudflare wants the browser to prove it's a browser
      blocked         Cloudflare block that will not clear on its own (error 1020)
      rate_limited    429: wait retry_after seconds
      not_found       404: the chat or file is gone
      invalid         other 4xx: wrong request, permanent for this item
      server          5xx: try again later
      bad_body        we got a 2xx but not the JSON we expected
      network         the request never completed
      too_large       a file is above the size we are willing to store
    """

    def __init__(self, kind: str, status: int | None = None, retry_after: float | None = None, detail: str = ""):
        super().__init__(f"{kind} (HTTP {status}): {detail[:200]}")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.detail = detail


def classify_response(status: int, headers: Mapping[str, str], body: str, page_url: str) -> ApiError | None:
    """Return the ApiError an HTTP response deserves, or None when it looks like a normal JSON answer."""
    lower_headers = {key.lower(): value for key, value in headers.items()}
    content_type = lower_headers.get("content-type", "").lower()
    body_start = (body or "")[:3000].lower()
    snippet = (body or "")[:200]

    if status == 401:
        return ApiError("token_expired", status, detail=snippet)
    if status == 403:
        if lower_headers.get("cf-mitigated") == "challenge" or _looks_like_challenge(body_start):
            return ApiError("challenge", status, detail="Cloudflare challenge")
        if "error code: 1020" in body_start or "error 1020" in body_start:
            return ApiError("blocked", status, detail="Cloudflare block (error 1020)")
        return ApiError("invalid", status, detail=snippet)
    if status == 429:
        return ApiError("rate_limited", status, retry_after=_retry_after_seconds(lower_headers), detail=snippet)
    if status == 404:
        return ApiError("not_found", status, detail=snippet)
    if 400 <= status < 500:
        return ApiError("invalid", status, detail=snippet)
    if status >= 500:
        return ApiError("server", status, detail=snippet)

    if "text/html" in content_type or body_start.lstrip().startswith("<"):
        if any(marker in page_url for marker in LOGIN_URL_MARKERS):
            return ApiError("login_required", status, detail=f"browser is on {page_url}")
        if _looks_like_challenge(body_start):
            return ApiError("challenge", status, detail="Cloudflare challenge page")
        return ApiError("bad_body", status, detail=snippet)
    return None


def _looks_like_challenge(body_start: str) -> bool:
    return any(marker in body_start for marker in CHALLENGE_MARKERS)


def _retry_after_seconds(lower_headers: Mapping[str, str]) -> float:
    value = lower_headers.get("retry-after", "").strip()
    if not value:
        return 30.0
    try:
        return max(1.0, float(value))
    except ValueError:
        pass
    try:
        return max(1.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return 30.0


# --- conversation lists ---------------------------------------------------------------

@dataclass(frozen=True)
class ListItem:
    id: str
    title: str
    create_time: float
    update_time: float
    is_archived: bool
    gizmo_id: str | None    # project id (g-p-...) when the chat lives inside a Project


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    conversations: list[ListItem]


def parse_time(value: Any) -> float | None:
    """Accept epoch numbers or ISO-8601 strings (Z, an offset, or naive meaning UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def clean_title(title: Any) -> str:
    """Titles arrive with trailing newlines and odd whitespace; keep one line."""
    if not isinstance(title, str):
        return ""
    return " ".join(title.split())


def to_list_item(raw: Mapping[str, Any], gizmo_id: str | None = None) -> ListItem:
    create_time = parse_time(raw.get("create_time"))
    update_time = parse_time(raw.get("update_time"))
    if update_time is None:
        update_time = create_time if create_time is not None else 0.0
    if create_time is None:
        create_time = update_time
    return ListItem(
        id=str(raw["id"]),
        title=clean_title(raw.get("title")),
        create_time=create_time,
        update_time=update_time,
        is_archived=bool(raw.get("is_archived")),
        gizmo_id=raw.get("gizmo_id") or gizmo_id,
    )


def list_conversations(api_get: ApiGet, archived: bool) -> list[ListItem]:
    """Every chat outside Projects, newest first. Archived chats are a separate list."""
    items: list[ListItem] = []
    offset = 0
    for _ in range(MAX_PAGES):
        path = f"/backend-api/conversations?offset={offset}&limit={PAGE_SIZE}&order=updated"
        if archived:
            path += "&is_archived=true"
        page = api_get(path).get("items") or []
        items.extend(to_list_item(raw) for raw in page)
        # The "total" field is unreliable while paginating; a short page is the real end.
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return items


def list_recent_conversations(api_get: ApiGet) -> list[ListItem]:
    """One page of the most recently updated chats outside Projects."""
    page = api_get(f"/backend-api/conversations?offset=0&limit={RECENT_PAGE_SIZE}&order=updated").get("items") or []
    return [to_list_item(raw) for raw in page]


def list_projects(api_get: ApiGet, conversations_per_project: int, max_pages: int = MAX_PAGES) -> list[Project]:
    """Projects from the sidebar, each with up to conversations_per_project recent chats."""
    projects: list[Project] = []
    cursor = None
    for _ in range(max_pages):
        path = f"/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo={conversations_per_project}"
        if cursor:
            path += f"&cursor={quote(str(cursor), safe='')}"
        page = api_get(path)
        for item in page.get("items") or []:
            project = _to_project(item)
            if project is not None:
                projects.append(project)
        cursor = page.get("cursor")
        if not cursor:
            break
    return projects


def _to_project(item: Mapping[str, Any]) -> Project | None:
    gizmo = item.get("gizmo") or {}
    if isinstance(gizmo.get("gizmo"), Mapping):    # the sidebar nests it one level deeper
        gizmo = gizmo["gizmo"]
    project_id = gizmo.get("id")
    if not project_id:
        return None
    display = gizmo.get("display") or {}
    title = clean_title(display.get("name") or gizmo.get("name") or "")
    raw_conversations = item.get("conversations") or []
    if isinstance(raw_conversations, Mapping):    # the sidebar wraps them as {"items": [...], "cursor": ...}
        raw_conversations = raw_conversations.get("items") or []
    conversations = [to_list_item(raw, project_id) for raw in raw_conversations if isinstance(raw, Mapping) and raw.get("id")]
    return Project(id=str(project_id), title=title, conversations=conversations)


def list_project_conversations(api_get: ApiGet, project_id: str) -> list[ListItem]:
    """Every chat inside one Project. Uses cursor pagination; a null cursor means the end."""
    items: list[ListItem] = []
    cursor: Any = 0
    for _ in range(MAX_PAGES):
        path = (f"/backend-api/gizmos/{quote(project_id, safe='')}/conversations"
                f"?cursor={quote(str(cursor), safe='')}&limit={PROJECT_PAGE_SIZE}")
        page = api_get(path)
        items.extend(to_list_item(raw, project_id) for raw in page.get("items") or [] if raw.get("id"))
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


# --- one conversation ---------------------------------------------------------------

def get_conversation(api_get: ApiGet, conversation_id: str) -> dict:
    return api_get(f"/backend-api/conversation/{quote(conversation_id, safe='')}")


# --- files ------------------------------------------------------------------------------

def file_id_from_pointer(pointer: str) -> str:
    """'sediment://file_abc' -> 'file_abc', 'file-service://file-abc' -> 'file-abc', 'file-abc' -> 'file-abc'."""
    return pointer.split("://", 1)[-1]


def is_composite_pointer(pointer: str) -> bool:
    """PDF page previews look like 'sediment://x#file_y#p_0.hash.jpg' and cannot be downloaded."""
    return "#" in pointer


def download_paths_for(pointer: str, conversation_id: str) -> list[str]:
    """The endpoints that can turn a file pointer into a download URL, most likely first.

    Newer 'sediment://' pointers use the per-conversation attachment route and the
    older 'file-service://' ones use the files route. Bare ids (from message
    attachments) are usually the old kind, so we try the files route first and
    fall back to the other.
    """
    file_id = quote(file_id_from_pointer(pointer), safe="")
    attachment_route = f"/backend-api/conversation/{quote(conversation_id, safe='')}/attachment/{file_id}/download"
    files_route = f"/backend-api/files/{file_id}/download"
    if pointer.startswith("sediment://"):
        return [attachment_route, files_route]
    return [files_route, attachment_route]


def resolve_download_url(api_get: ApiGet, pointer: str, conversation_id: str) -> str:
    """Ask the API for a (short-lived) download URL for a file pointer."""
    last_error: ApiError | None = None
    for path in download_paths_for(pointer, conversation_id):
        try:
            response = api_get(path)
        except ApiError as error:
            if error.kind in ("not_found", "invalid"):
                last_error = error
                continue
            raise
        url = response.get("download_url") if isinstance(response, Mapping) else None
        if url:
            return str(url)
        last_error = ApiError("bad_body", detail=f"no download_url in the answer for {pointer}")
    assert last_error is not None
    raise last_error
