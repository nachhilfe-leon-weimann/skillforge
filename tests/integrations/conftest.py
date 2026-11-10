"""Fixtures shared by the integration tests."""

import json
from typing import Any

import pytest

TOO_DEEP = b'[["synthetic: a body nested deeper than the parser goes"]]'
"""A body whose parse raises ``RecursionError`` under the ``too_deep`` fixture."""


@pytest.fixture
def too_deep(monkeypatch) -> bytes:
    """Parsing ``TOO_DEEP`` raises ``RecursionError``, as a deeply nested body does where the C stack ends first.

    How deep that is depends on the platform (Python 3.14 bounds C recursion by the stack), so a real deep body would
    test the platform, not the code. Every other body parses as before."""
    loads = json.loads

    def guarded(content: str | bytes, **kwargs: Any) -> Any:
        if content == TOO_DEEP:
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")
        return loads(content, **kwargs)

    monkeypatch.setattr(json, "loads", guarded)
    return TOO_DEEP
