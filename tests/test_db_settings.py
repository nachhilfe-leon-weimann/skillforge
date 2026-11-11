"""Database settings keep their URLs, and the password in them, out of every validation error and representation."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.db.config import DatabaseSettings

PASSWORD = "synthetic-password"
VALID_URL = f"postgresql+asyncpg://skillforge:{PASSWORD}@db.example.org/skillforge"
VALID_MIGRATION_URL = f"postgresql+asyncpg://skillforge:{PASSWORD}@direct.db.example.org/skillforge"
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


def _database_settings() -> DatabaseSettings:
    return DatabaseSettings(_env_file=None, url=VALID_URL, migration_url=VALID_MIGRATION_URL)


@pytest.mark.parametrize(
    "load",
    [_database_settings, lambda: Settings(_env_file=None, db=_database_settings())],
    ids=["database settings", "app settings"],
)
def test_a_url_is_kept_out_of_the_settings_representation(clean_env, load):
    settings = load()

    for shown in (str(settings), repr(settings)):
        assert VALID_URL not in shown
        assert VALID_MIGRATION_URL not in shown
        assert PASSWORD not in shown


def test_the_urls_still_hold_their_value(clean_env):
    """The app and the migrations connect with them."""
    settings = _database_settings()

    assert str(settings.url) == VALID_URL
    assert str(settings.migration_url) == VALID_MIGRATION_URL
