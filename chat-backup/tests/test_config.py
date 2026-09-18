import pytest

from chatbackup.config import parse_quiet_hours


def test_parse_quiet_hours():
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("1-8") == (1, 8)
    assert parse_quiet_hours("22-6") == (22, 6)
    for bad in ("1", "1-24", "8-8", "one-eight"):
        with pytest.raises(SystemExit):
            parse_quiet_hours(bad)
