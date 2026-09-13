from zoneinfo import ZoneInfo

import pytest

from chatbackup.db import Database
from chatbackup.notify import Notifier


@pytest.fixture
def notifier(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.sqlite3")
    instance = Notifier("https://ntfy.example", "topic", db)
    sent = []
    monkeypatch.setattr(instance, "send", lambda title, message, priority=3, tags="": sent.append((title, priority)) or True)
    instance.sent = sent
    yield instance
    db.close()


def test_alert_only_on_state_change(notifier):
    notifier.set_problem("login", True, "dead")
    notifier.set_problem("login", True, "dead")
    notifier.set_problem("login", False, "ok")
    notifier.set_problem("login", False, "ok")
    assert notifier.sent == [("chat-backup: login", 4), ("chat-backup: login ok", 2)]


def test_recovery_without_a_problem_is_silent(notifier):
    notifier.set_problem("disk", False, "fine")
    assert notifier.sent == []


def test_startup_notice_is_rate_limited(notifier):
    notifier.startup_notice(1000.0)
    notifier.startup_notice(2000.0)
    notifier.startup_notice(1000.0 + 3600)
    assert len(notifier.sent) == 2


def test_digest_once_per_day(notifier):
    tz = ZoneInfo("America/New_York")
    morning = 1789300000.0                  # 2026-09-13 07:46 in New York, before the digest hour
    assert notifier.digest_due(morning, tz) is False
    later = morning + 3 * 3600
    assert notifier.digest_due(later, tz) is True
    notifier.send_digest({"new": 1, "fetched": 2, "deleted": 0, "files": 3, "total": 10, "deleted_total": 1,
                          "pending": 0, "failing": 0, "last_sweep": "10:00"}, later, tz)
    assert notifier.sent[-1] == ("chat-backup daily summary", 2)
    assert notifier.digest_due(later + 60, tz) is False
    assert notifier.digest_due(later + 86400, tz) is True


def test_send_without_topic_only_logs(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    assert Notifier("https://ntfy.example", "", db).send("t", "m") is False
    db.close()


def test_send_posts_to_ntfy(tmp_path, monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["data"] = request.data
        return FakeResponse()

    monkeypatch.setattr("chatbackup.notify.urllib.request.urlopen", fake_urlopen)
    db = Database(tmp_path / "t.sqlite3")
    assert Notifier("https://ntfy.example", "topic", db).send("Tïtle", "bödy", 4, "warning") is True
    db.close()
    assert captured["url"] == "https://ntfy.example/topic"
    assert captured["headers"]["Title"] == "T?tle"          # headers must stay ASCII
    assert captured["headers"]["Priority"] == "4"
    assert captured["headers"]["Tags"] == "warning"
    assert captured["data"] == "bödy".encode("utf-8")
