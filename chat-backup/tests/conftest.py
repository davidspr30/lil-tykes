"""Shared test helpers. No test here touches the network or a browser."""

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chatbackup.chatgpt import ApiError

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeApi:
    """Stands in for the browser's api_get.

    routes maps a path (or a path prefix) to an answer, or to a list of answers
    handed out in order (the last one repeats). An answer that is an exception
    is raised instead of returned. Every call is recorded in .calls.
    """

    def __init__(self, routes: dict):
        self.routes = {path: list(answer) if isinstance(answer, list) else [answer] for path, answer in routes.items()}
        self.calls: list[str] = []

    def __call__(self, path: str):
        self.calls.append(path)
        matches = [prefix for prefix in self.routes if path == prefix or path.startswith(prefix)]
        if not matches:
            raise AssertionError(f"unexpected API call: {path}")
        answers = self.routes[max(matches, key=len)]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def tz():
    return ZoneInfo("America/New_York")


@pytest.fixture
def branch_conversation():
    return load_fixture("conversation_branch.json")


@pytest.fixture
def files_conversation():
    return load_fixture("conversation_files.json")


@pytest.fixture
def canvas_conversation():
    return load_fixture("conversation_canvas.json")


@pytest.fixture
def list_page():
    return load_fixture("list_page.json")


@pytest.fixture
def sidebar_page():
    return load_fixture("sidebar_page.json")


@pytest.fixture
def fake_api():
    return FakeApi
