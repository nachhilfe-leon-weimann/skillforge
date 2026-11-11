"""A form POST sent as a read goes out in ``dry_run`` and is never an unknown outcome: only the Graph token uses one.

``IntegrationHttp.read_form`` exists for a POST that changes nothing - a token request. Any other caller could send a
write as a "read" past ``dry_run`` and past ``OutcomeUnknownError`` (ADR 0010, points 3 and 6), so its use is
allowed in one module only.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {"app/integrations/graph/credential.py"}


def _uses(source: str) -> bool:
    """Whether a module calls or otherwise names ``read_form`` (``getattr`` with the name included)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "read_form":
            return True
        if isinstance(node, ast.Constant) and node.value == "read_form":
            return True
    return False


def test_only_the_graph_token_request_sends_a_form_as_a_read():
    users = {
        path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / "app").rglob("*.py") if _uses(path.read_text())
    }

    assert users == ALLOWED


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("await http.read_form('/token', form={})", True),
        ("send = http.read_form", True),
        ("getattr(http, 'read_form')", True),
        ("async def read_form(self): ...", False),
        ("await http.read('/things')", False),
    ],
)
def test_the_check_catches_every_form_of_a_use(source: str, found: bool):
    assert _uses(source) is found
