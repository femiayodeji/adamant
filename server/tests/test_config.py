"""Environment handling at import time.

These are cheap and they guard a failure mode that is disproportionately painful:
anything that raises while `server.main` is being imported is a container crash
loop, with the real cause buried in a restart log.
"""

from __future__ import annotations

import pytest

from server.main import _log_level


@pytest.mark.parametrize(
    "value,expected",
    [
        ("info", "INFO"),  # what docker-compose and uvicorn conventionally pass
        ("INFO", "INFO"),
        ("debug", "DEBUG"),
        ("  warning  ", "WARNING"),
        ("critical", "CRITICAL"),
    ],
)
def test_log_level_accepts_the_conventional_lowercase_spellings(monkeypatch, value, expected):
    monkeypatch.setenv("ARENA_LOG_LEVEL", value)
    assert _log_level() == expected


@pytest.mark.parametrize("value", ["", "verbose", "loud", "9"])
def test_an_unusable_log_level_falls_back_instead_of_crashing(monkeypatch, value):
    monkeypatch.setenv("ARENA_LOG_LEVEL", value)
    assert _log_level() == "INFO"


def test_log_level_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("ARENA_LOG_LEVEL", raising=False)
    assert _log_level() == "INFO"
