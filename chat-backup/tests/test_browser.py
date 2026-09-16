from chatbackup.browser import is_fetch_failure, seed_cookies


def test_seed_cookies_strips_cloudflare_cookies():
    state = {"cookies": [
        {"name": "cf_clearance", "value": "1"},
        {"name": "__cf_bm", "value": "2"},
        {"name": "_cfuvid", "value": "3"},
        {"name": "__Secure-next-auth.session-token", "value": "s"},
        {"name": "oai-did", "value": "d"},
    ]}
    assert [cookie["name"] for cookie in seed_cookies(state)] == ["__Secure-next-auth.session-token", "oai-did"]
    assert seed_cookies({}) == []


def test_is_fetch_failure():
    assert is_fetch_failure(Exception("TypeError: Failed to fetch"))
    assert is_fetch_failure(Exception("TimeoutError: signal timed out"))
    assert not is_fetch_failure(Exception("Target page, context or browser has been closed"))
