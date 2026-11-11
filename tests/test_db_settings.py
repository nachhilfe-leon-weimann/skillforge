"""Database settings keep a malformed URL, and the password in it, out of every validation error."""

import pytest
from pydantic import ValidationError

from app.core.db.config import DatabaseSettings

PASSWORD = "synthetic-password"
VALID_URL = f"postgresql+asyncpg://skillforge:{PASSWORD}@db.example.org/skillforge"
MALFORMED_URL = f"postgresql://u:{PASSWORD}@:x/db"
"""Short enough that pydantic would repeat it whole, password included."""


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No ``DB__*`` variable, and a working directory without an env file: independent of the checkout."""
    for field in DatabaseSettings.model_fields:
        monkeypatch.delenv(f"DB__{field.upper()}", raising=False)
        monkeypatch.delenv(f"db__{field}", raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


@pytest.mark.parametrize("field", ["url", "migration_url"])
def test_a_malformed_url_is_refused_without_repeating_it(clean_env, field: str):
    """As ``migrations/env.py`` loads them: on their own, from the environment."""
    clean_env.setenv("DB__URL", VALID_URL)
    clean_env.setenv(f"DB__{field.upper()}", MALFORMED_URL)

    with pytest.raises(ValidationError) as raised:
        DatabaseSettings.from_env()

    assert [error["loc"] for error in raised.value.errors()] == [(field,)]
    for shown in (str(raised.value), repr(raised.value)):
        assert MALFORMED_URL not in shown
        assert PASSWORD not in shown
