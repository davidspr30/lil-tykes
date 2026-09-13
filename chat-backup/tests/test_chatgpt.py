import pytest

from chatbackup import chatgpt
from chatbackup.chatgpt import ApiError, classify_response


def test_parse_time_variants():
    epoch = chatgpt.parse_time("2026-09-13T10:00:00Z")
    assert epoch == chatgpt.parse_time("2026-09-13T10:00:00+00:00")
    assert epoch == chatgpt.parse_time("2026-09-13T10:00:00")       # naive means UTC
    assert epoch == chatgpt.parse_time(epoch)
    assert chatgpt.parse_time("2026-09-13T10:00:00.123456Z") == pytest.approx(epoch + 0.123456)
    assert chatgpt.parse_time(1789300000) == 1789300000.0
    assert chatgpt.parse_time(None) is None
    assert chatgpt.parse_time("") is None
    assert chatgpt.parse_time("garbage") is None
    assert chatgpt.parse_time(True) is None


def test_clean_title():
    assert chatgpt.clean_title("Budget\n") == "Budget"
    assert chatgpt.clean_title("  two\n\nlines ") == "two lines"
    assert chatgpt.clean_title(None) == ""


def test_list_conversations_stops_on_a_short_page(fake_api, list_page):
    full_page = {"items": [{"id": f"c-{n}", "title": "t", "create_time": "2026-09-13T10:00:00Z",
                            "update_time": "2026-09-13T10:00:00Z"} for n in range(100)]}
    api = fake_api({
        "/backend-api/conversations?offset=0&limit=100&order=updated": full_page,
        "/backend-api/conversations?offset=100&limit=100&order=updated": list_page,
    })
    items = chatgpt.list_conversations(api, archived=False)
    assert len(items) == 102
    assert len(api.calls) == 2


def test_list_conversations_archived(fake_api, list_page):
    api = fake_api({"/backend-api/conversations?offset=0&limit=100&order=updated&is_archived=true": list_page})
    assert [item.id for item in chatgpt.list_conversations(api, archived=True)] == ["c-1", "c-2"]


def test_list_items_are_normalised(fake_api, list_page):
    api = fake_api({"/backend-api/conversations?offset=0&limit=50&order=updated": list_page})
    first, second = chatgpt.list_recent_conversations(api)
    assert first.title == "Budget"
    assert first.is_archived is False and first.gizmo_id is None
    assert first.create_time == pytest.approx(chatgpt.parse_time("2026-09-13T10:00:00.123456Z"))
    assert second.is_archived is True


def test_list_item_without_times_still_works():
    item = chatgpt.to_list_item({"id": "x", "title": None}, "g-p-1")
    assert item.title == "" and item.create_time == 0.0 and item.update_time == 0.0 and item.gizmo_id == "g-p-1"


def test_list_projects_follows_the_cursor(fake_api, sidebar_page):
    page_one = {"items": sidebar_page["items"][:1], "cursor": "abc"}
    page_two = {"items": sidebar_page["items"][1:], "cursor": None}
    base = "/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=5"
    api = fake_api({base: page_one, base + "&cursor=abc": page_two})
    projects = chatgpt.list_projects(api, 5)
    assert [(project.id, project.title) for project in projects] == [("g-p-aaaa", "Work"), ("g-p-bbbb", "Home")]
    assert projects[0].conversations[0].id == "c-p1"
    assert projects[0].conversations[0].gizmo_id == "g-p-aaaa"
    assert len(api.calls) == 2

    api = fake_api({base: page_one, base + "&cursor=abc": page_two})
    assert len(chatgpt.list_projects(api, 5, max_pages=1)) == 1


def test_list_project_conversations_follows_the_cursor(fake_api):
    first = {"items": [{"id": "p1", "title": "A", "create_time": 1.0, "update_time": 2.0}], "cursor": "n1"}
    second = {"items": [{"id": "p2", "title": "B", "create_time": 1.0, "update_time": 2.0}], "cursor": None}
    api = fake_api({
        "/backend-api/gizmos/g-p-aaaa/conversations?cursor=0&limit=100": first,
        "/backend-api/gizmos/g-p-aaaa/conversations?cursor=n1&limit=100": second,
    })
    items = chatgpt.list_project_conversations(api, "g-p-aaaa")
    assert [item.id for item in items] == ["p1", "p2"]
    assert all(item.gizmo_id == "g-p-aaaa" for item in items)


def test_download_paths_by_scheme():
    assert chatgpt.download_paths_for("sediment://file_abc", "conv-1") == [
        "/backend-api/conversation/conv-1/attachment/file_abc/download",
        "/backend-api/files/file_abc/download",
    ]
    assert chatgpt.download_paths_for("file-service://file-abc", "conv-1")[0] == "/backend-api/files/file-abc/download"
    assert chatgpt.download_paths_for("file-abc", "conv-1")[0] == "/backend-api/files/file-abc/download"
    assert chatgpt.is_composite_pointer("sediment://abc#file_zzz#p_0.hash.jpg")
    assert not chatgpt.is_composite_pointer("sediment://file_abc")


def test_resolve_download_url_falls_back_to_the_other_route(fake_api):
    api = fake_api({
        "/backend-api/files/file-abc/download": ApiError("invalid", 422),
        "/backend-api/conversation/conv-1/attachment/file-abc/download": {"download_url": "https://x/y"},
    })
    assert chatgpt.resolve_download_url(api, "file-abc", "conv-1") == "https://x/y"

    api = fake_api({"/backend-api/files/": ApiError("not_found", 404), "/backend-api/conversation/": ApiError("not_found", 404)})
    with pytest.raises(ApiError) as failure:
        chatgpt.resolve_download_url(api, "file-abc", "conv-1")
    assert failure.value.kind == "not_found"

    api = fake_api({"/backend-api/files/": ApiError("challenge", 403)})
    with pytest.raises(ApiError) as failure:
        chatgpt.resolve_download_url(api, "file-abc", "conv-1")
    assert failure.value.kind == "challenge"
    assert len(api.calls) == 1              # no point trying the other route

    api = fake_api({"/backend-api/files/": {"status": "success"}, "/backend-api/conversation/": {}})
    with pytest.raises(ApiError) as failure:
        chatgpt.resolve_download_url(api, "file-abc", "conv-1")
    assert failure.value.kind == "bad_body"


@pytest.mark.parametrize("status, headers, body, url, kind", [
    (200, {"content-type": "application/json"}, '{"items": []}', "https://chatgpt.com/", None),
    (401, {}, "", "https://chatgpt.com/", "token_expired"),
    (403, {"cf-mitigated": "challenge", "content-type": "text/html"}, "<html>Just a moment...</html>", "https://chatgpt.com/", "challenge"),
    (403, {"content-type": "text/html"}, "<html>error code: 1020</html>", "https://chatgpt.com/", "blocked"),
    (403, {}, '{"detail": "no"}', "https://chatgpt.com/", "invalid"),
    (429, {"Retry-After": "120"}, "", "https://chatgpt.com/", "rate_limited"),
    (404, {}, "", "https://chatgpt.com/", "not_found"),
    (422, {}, "", "https://chatgpt.com/", "invalid"),
    (503, {}, "", "https://chatgpt.com/", "server"),
    (200, {"content-type": "text/html"}, "<html>login</html>", "https://chatgpt.com/auth/login", "login_required"),
    (200, {"content-type": "text/html"}, "<html>Just a moment...</html>", "https://chatgpt.com/", "challenge"),
    (200, {"content-type": "text/html"}, "<html>other</html>", "https://chatgpt.com/", "bad_body"),
])
def test_classify_response(status, headers, body, url, kind):
    error = classify_response(status, headers, body, url)
    assert (error.kind if error else None) == kind


def test_retry_after_header():
    assert classify_response(429, {"retry-after": "120"}, "", "u").retry_after == 120.0
    assert classify_response(429, {}, "", "u").retry_after == 30.0
    assert classify_response(429, {"retry-after": "soon"}, "", "u").retry_after == 30.0
