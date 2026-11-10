"""No recorded fixture holds a token, a real e-mail address, an IBAN or the trial account's marker.

The patterns are this test's own, independent of the recorder's scrub, so a gap in the scrub fails here.
"""

import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
LEAKS = {
    "a 32-hex token": re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"),
    "an IBAN": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b"),
    "the trial marker": re.compile(r"TRIAL"),
}


def leaks(text: str) -> list[str]:
    """What in ``text`` must not be in a fixture."""
    found = [name for name, pattern in LEAKS.items() if pattern.search(text)]
    if any(match["domain"].lower() != "example.org" for match in _EMAIL.finditer(text)):
        found.append("an e-mail address outside example.org")
    return found


def test_no_fixture_leaks():
    files = [path for path in FIXTURES.rglob("*") if path.is_file()]

    assert {path.relative_to(FIXTURES).as_posix(): found for path in files if (found := leaks(path.read_text()))} == {}


@pytest.mark.parametrize(
    ("content", "leak"),
    [
        ('{"token": "0123456789abcdef0123456789ABCDEF"}', "a 32-hex token"),
        ('{"iban": "DE89 3704 0044 0532 0130 00"}', "an IBAN"),
        ('{"iban": "DE89370400440532013000"}', "an IBAN"),
        ('{"name": "Example Tutoring TRIAL"}', "the trial marker"),
        ('{"email": "someone@mail.example.com"}', "an e-mail address outside example.org"),
        ('{"text": "write to person@example.org.evil.test"}', "an e-mail address outside example.org"),
    ],
)
def test_the_scan_catches_each_pattern(tmp_path: Path, content: str, leak: str):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(content)

    assert leaks(fixture.read_text()) == [leak]


def test_the_scan_lets_synthetic_data_pass():
    clean = '{"id": "501", "email": "person@example.org", "date": "2026-11-02T00:00:00+01:00", "hex": "ab12"}'

    assert leaks(clean) == []
