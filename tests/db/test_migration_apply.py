"""Run the real Alembic migrations against a throwaway database.

The other DB tests build their schema from the models via ``Base.metadata.create_all``
and therefore never exercise the migrations. This test closes that gap: it applies the
migrations on an empty database, asserts the result matches the models (no drift), and
verifies the chain is reversible - catching breakage in CI instead of on a prod deploy.

Alembic runs in a subprocess on purpose: in-process it would run ``logging.fileConfig``
(disabling the app loggers other tests assert on) and resolve ``Base.metadata`` from this
process (where test-only models such as tests/db/test_base.py's would leak into ``check``).
A subprocess keeps that global state isolated and matches how the migrate service runs.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from app.core.db.models import LessonStatus

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.db

_REVISION_0010 = "0010_subject_title_unique"
_REVISION_0011 = "0011_grant_mode_user_accounts"
# The schemas of today's models. `bot` is history: 0001_baseline creates it, 0013_retire_bot drops it.
_APPLICATION_SCHEMAS = {"auth", "billing", "core", "ext", "geo", "system"}


def _asyncpg_dsn(url: str) -> str:
    # SQLAlchemy URL (postgresql+asyncpg://...) -> plain libpq DSN for asyncpg.
    return url.replace("+asyncpg", "")


def _with_database(url: str, database: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{database}"))


async def _run_on_server(server_url: str, statement: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(server_url))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


async def _enum_labels(url: str, schema: str, enum_name: str) -> list[str]:
    conn = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await conn.fetch(
            """
            SELECT e.enumlabel
            FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typname = $1 AND n.nspname = $2
            ORDER BY e.enumsortorder
            """,
            enum_name,
            schema,
        )
        return [row["enumlabel"] for row in rows]
    finally:
        await conn.close()


async def _rows(url: str, query: str, *args: object) -> list[tuple[object, ...]]:
    conn = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return [tuple(row) for row in await conn.fetch(query, *args)]
    finally:
        await conn.close()


async def _primary_key_columns(url: str, schema: str, table: str) -> list[str]:
    rows = await _rows(
        url,
        """
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, position)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p' AND t.relname = $1 AND n.nspname = $2
        ORDER BY k.position
        """,
        table,
        schema,
    )
    return [str(name) for (name,) in rows]


async def _schemas(url: str) -> set[str]:
    """Every schema but Postgres' own and `public`."""
    rows = await _rows(
        url,
        """
        SELECT nspname FROM pg_namespace
        WHERE nspname !~ '^pg_' AND nspname NOT IN ('information_schema', 'public')
        """,
    )
    return {str(name) for (name,) in rows}


async def _leftovers(url: str) -> list[str]:
    """Every relation in an application schema and every enum type, `bot` and `public` included."""
    rows = await _rows(
        url,
        """
        SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('auth', 'bot', 'core', 'ext', 'geo', 'system')
        UNION ALL
        SELECT n.nspname || '.' || t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typtype = 'e' AND n.nspname IN ('auth', 'bot', 'core', 'ext', 'geo', 'public', 'system')
        """,
    )
    return sorted(str(name) for (name,) in rows)


# What `alembic check` cannot see: every column with its type, nullability and default, every constraint and
# index by name and definition, and every enum type with its labels in order - of the `bot` schema.
_BOT_CATALOG = """
SELECT 'column', c.relname, a.attnum || ' ' || a.attname,
       format_type(a.atttypid, a.atttypmod) || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
       || coalesce(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '')
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE c.relnamespace = 'bot'::regnamespace AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
UNION ALL
SELECT 'constraint', c.relname, k.conname, pg_get_constraintdef(k.oid)
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
WHERE k.connamespace = 'bot'::regnamespace
UNION ALL
SELECT 'index', c.relname, i.relname, pg_get_indexdef(i.oid)
FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid
WHERE c.relnamespace = 'bot'::regnamespace
UNION ALL
SELECT 'enum', t.typname, '', string_agg(e.enumlabel, ', ' ORDER BY e.enumsortorder)
FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
WHERE t.typnamespace = 'bot'::regnamespace
GROUP BY t.typname
"""


async def _bot_catalog(url: str) -> list[tuple[object, ...]]:
    return sorted(await _rows(url, _BOT_CATALOG))


@pytest.fixture
def migration_db_url(db_url: str):
    """A freshly created, empty database so migrations run from a clean slate."""
    name = f"migtest_{uuid.uuid4().hex[:12]}"
    asyncio.run(_run_on_server(db_url, f'CREATE DATABASE "{name}"'))
    try:
        yield _with_database(db_url, name)
    finally:
        asyncio.run(_run_on_server(db_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic_result(db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DB__URL": db_url, "DB__MIGRATION_URL": db_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _alembic(db_url: str, *args: str) -> None:
    result = _alembic_result(db_url, *args)
    assert result.returncode == 0, f"`alembic {' '.join(args)}` failed:\n{result.stdout}\n{result.stderr}"


def test_migrations_apply_match_models_and_reverse(migration_db_url: str) -> None:
    # 1. Every migration applies cleanly from an empty database - without the bot models: 0001_baseline
    #    creates the `bot` schema itself and 0013_retire_bot drops it (bot-decoupling spec, decision L).
    _alembic(migration_db_url, "upgrade", "head")
    # 2. The migrated schema matches the models - fails if a revision is missing or
    #    incomplete (drift), which create_all-based tests cannot detect. `check` runs env.py once
    #    more: a leftover `models/bot/__pycache__/` must not bring the schema back.
    _alembic(migration_db_url, "check")
    assert asyncio.run(_schemas(migration_db_url)) == _APPLICATION_SCHEMAS
    # 3. The chain is reversible and can be rebuilt from scratch. env.py creates the schemas of
    #    today's models on every run, so they stay behind, empty; `bot` goes with 0001's downgrade.
    _alembic(migration_db_url, "downgrade", "base")
    assert asyncio.run(_schemas(migration_db_url)) == _APPLICATION_SCHEMAS
    assert asyncio.run(_leftovers(migration_db_url)) == []
    _alembic(migration_db_url, "upgrade", "head")


_OPERATION_KINDS_WITH_OFF_BOARDING = [
    "tutor_activate",
    "student_activate",
    "student_stash",
    "student_pop",
    "student_deactivate",
    "tutor_deactivate",
]
_OPERATION_KINDS_WITHOUT_OFF_BOARDING = _OPERATION_KINDS_WITH_OFF_BOARDING[:4]


def test_off_boarding_operation_kinds_migration_is_reversible(migration_db_url: str) -> None:
    # `alembic check` is blind to enum-label drift, so assert the actual DB labels the
    # migration path produces - forward adds the off-boarding kinds, downgrade removes them
    # (exercising the enum-recreate recast), re-upgrade adds them back. The type lives until
    # 0011: 0013_retire_bot drops the `bot` schema.
    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    assert asyncio.run(_enum_labels(migration_db_url, "bot", "operation_kind")) == _OPERATION_KINDS_WITH_OFF_BOARDING

    _alembic(migration_db_url, "downgrade", "0006_worker_heartbeat")
    assert asyncio.run(_enum_labels(migration_db_url, "bot", "operation_kind")) == _OPERATION_KINDS_WITHOUT_OFF_BOARDING

    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    assert asyncio.run(_enum_labels(migration_db_url, "bot", "operation_kind")) == _OPERATION_KINDS_WITH_OFF_BOARDING


_OPERATION_STATUSES_WITH_CANCELLED = ["prepared", "committed", "expired", "failed", "cancelled"]
_OPERATION_STATUSES_WITHOUT_CANCELLED = _OPERATION_STATUSES_WITH_CANCELLED[:4]


def test_cancelled_operation_status_migration_is_reversible(migration_db_url: str) -> None:
    # `alembic check` is blind to enum-label drift, so assert the actual labels the migration
    # path produces: forward adds `cancelled`, downgrade recreates the type without it
    # (exercising the enum-recreate recast under a server_default), re-upgrade adds it back.
    # The type lives until 0011: 0013_retire_bot drops the `bot` schema.
    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    assert asyncio.run(_enum_labels(migration_db_url, "bot", "operation_status")) == _OPERATION_STATUSES_WITH_CANCELLED

    _alembic(migration_db_url, "downgrade", "0008_idempotent_prepare")
    assert (
        asyncio.run(_enum_labels(migration_db_url, "bot", "operation_status")) == _OPERATION_STATUSES_WITHOUT_CANCELLED
    )

    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    assert asyncio.run(_enum_labels(migration_db_url, "bot", "operation_status")) == _OPERATION_STATUSES_WITH_CANCELLED


_REVISION_0011_ENUM_LABELS = {
    "grant_mode": ["application", "delegated"],
    "user_account_status": ["active", "disabled"],
    "user_account_role_name": ["admin"],
    "user_action_token_purpose": ["invitation", "password_reset"],
}
_GRANT_TABLE = ("auth", "application_client_scope_grant")
_GRANT_KEY_WITH_MODE = ["application_client_id", "scope_key", "mode"]
_GRANT_KEY_WITHOUT_MODE = _GRANT_KEY_WITH_MODE[:2]

_PARTY_ID = "11111111-1111-1111-1111-111111111111"
_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
_ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
_OTHER_PARTY_ID = "44444444-4444-4444-4444-444444444444"

# What the revision meets in production: a client holding a grant that has no mode yet.
_SEED_BEFORE_GRANT_MODES = f"""
INSERT INTO auth.permission_scope (key, description) VALUES ('crm:read', 'Read the CRM');
INSERT INTO core.party (id, type) VALUES ('{_PARTY_ID}', 'PERSON');
INSERT INTO auth.application_client (id, client_id, name) VALUES ('{_CLIENT_ID}', 'seed-client', 'Seed Client');
INSERT INTO auth.application_client_scope_grant (application_client_id, scope_key) VALUES ('{_CLIENT_ID}', 'crm:read');
"""

# A row in every new table - the account's status left to its server default - and a `delegated`
# grant of the scope the client already holds as an `application` grant.
_SEED_ACCOUNT_AND_DELEGATED_GRANT = f"""
INSERT INTO auth.user_account (id, party_id, email) VALUES ('{_ACCOUNT_ID}', '{_PARTY_ID}', 'admin@example.org');
INSERT INTO auth.user_account_role (user_account_id, role) VALUES ('{_ACCOUNT_ID}', 'admin');
INSERT INTO auth.user_session (id, user_account_id, application_client_id, scope, refresh_token_hash, expires_at)
    VALUES (gen_random_uuid(), '{_ACCOUNT_ID}', '{_CLIENT_ID}', 'account:self', 'refresh-hash',
            now() + interval '30 days');
INSERT INTO auth.user_action_token (id, user_account_id, purpose, token_hash, expires_at, issued_by)
    VALUES (gen_random_uuid(), '{_ACCOUNT_ID}', 'invitation', 'action-hash', now() + interval '7 days', 'cli');
INSERT INTO auth.application_client_scope_grant (application_client_id, scope_key, mode)
    VALUES ('{_CLIENT_ID}', 'crm:read', 'delegated');
"""

# Another person whose login e-mail is not lowercased. Both statements run in one implicit
# transaction, so the refused account takes its party with it.
_SEED_UPPERCASE_EMAIL = f"""
INSERT INTO core.party (id, type) VALUES ('{_OTHER_PARTY_ID}', 'PERSON');
INSERT INTO auth.user_account (id, party_id, email) VALUES (gen_random_uuid(), '{_OTHER_PARTY_ID}', 'Anna@Example.org');
"""


def test_grant_mode_and_user_account_migration_is_reversible(migration_db_url: str) -> None:
    # Autogenerate neither creates the type of an added enum column nor detects a primary-key
    # change, and `alembic check` is blind to both and to check constraints - so assert the types,
    # the key, the e-mail check and the rows on a database that holds data on either side of the
    # revision. The empty-database path is covered by test_migrations_apply_match_models_and_reverse.
    _alembic(migration_db_url, "upgrade", _REVISION_0010)
    asyncio.run(_run_on_server(migration_db_url, _SEED_BEFORE_GRANT_MODES))

    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    for name, labels in _REVISION_0011_ENUM_LABELS.items():
        assert asyncio.run(_enum_labels(migration_db_url, "public", name)) == labels
    assert asyncio.run(_primary_key_columns(migration_db_url, *_GRANT_TABLE)) == _GRANT_KEY_WITH_MODE
    grants = "SELECT application_client_id::text, scope_key, mode::text FROM auth.application_client_scope_grant"
    assert asyncio.run(_rows(migration_db_url, grants)) == [(_CLIENT_ID, "crm:read", "application")]

    asyncio.run(_run_on_server(migration_db_url, _SEED_ACCOUNT_AND_DELEGATED_GRANT))
    assert asyncio.run(_rows(migration_db_url, "SELECT status::text FROM auth.user_account")) == [("active",)]
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as refused:
        asyncio.run(_run_on_server(migration_db_url, _SEED_UPPERCASE_EMAIL))
    assert getattr(refused.value, "constraint_name", None) == "ck_user_account_email_lowercase"

    _alembic(migration_db_url, "downgrade", _REVISION_0010)
    for name in _REVISION_0011_ENUM_LABELS:
        assert asyncio.run(_enum_labels(migration_db_url, "public", name)) == [], f"{name} survived the downgrade"
    assert asyncio.run(_primary_key_columns(migration_db_url, *_GRANT_TABLE)) == _GRANT_KEY_WITHOUT_MODE
    # The party, the client and its original grant survive; the delegated grant does not.
    assert asyncio.run(_rows(migration_db_url, "SELECT id::text FROM core.party")) == [(_PARTY_ID,)]
    assert asyncio.run(_rows(migration_db_url, "SELECT id::text FROM auth.application_client")) == [(_CLIENT_ID,)]
    grants = "SELECT application_client_id::text, scope_key FROM auth.application_client_scope_grant"
    assert asyncio.run(_rows(migration_db_url, grants)) == [(_CLIENT_ID, "crm:read")]

    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    assert asyncio.run(_primary_key_columns(migration_db_url, *_GRANT_TABLE)) == _GRANT_KEY_WITH_MODE


# --- 0013_retire_bot (bot-decoupling spec, decision M) ---

_SKILLBOT_ID = "55555555-5555-5555-5555-555555555555"
_OPERATOR_ID = "66666666-6666-6666-6666-666666666666"

# What the revision meets in production: the bot scopes granted in both modes, a grant of another scope,
# and a row in a bot table.
_SEED_BOT_STATE = f"""
INSERT INTO auth.permission_scope (key, description) VALUES
    ('bot:read', 'Read bot API surface.'), ('bot:write', 'Write bot API surface.'), ('crm:read', 'Read the CRM');
INSERT INTO auth.application_client (id, client_id, name) VALUES
    ('{_SKILLBOT_ID}', 'skillbot', 'SkillBot'), ('{_OPERATOR_ID}', 'operator', 'Operator');
INSERT INTO auth.application_client_scope_grant (application_client_id, scope_key, mode) VALUES
    ('{_SKILLBOT_ID}', 'bot:read', 'application'),
    ('{_SKILLBOT_ID}', 'bot:write', 'application'),
    ('{_OPERATOR_ID}', 'bot:read', 'delegated'),
    ('{_OPERATOR_ID}', 'crm:read', 'delegated');
INSERT INTO bot.job (job_id, kind) VALUES (gen_random_uuid(), 'sync_roles');
"""

_GRANTS = """
SELECT c.client_id, g.scope_key, g.mode::text
FROM auth.application_client_scope_grant g JOIN auth.application_client c ON c.id = g.application_client_id
ORDER BY 1, 2, 3
"""
_SCOPES = "SELECT key, description, active FROM auth.permission_scope ORDER BY key"
_AUDIT = "SELECT principal_type, principal_id, event_type, success, detail FROM auth.auth_audit_log ORDER BY detail"

_ALL_SEEDED_GRANTS = [
    ("operator", "bot:read", "delegated"),
    ("operator", "crm:read", "delegated"),
    ("skillbot", "bot:read", "application"),
    ("skillbot", "bot:write", "application"),
]
_ALL_SEEDED_SCOPES = [
    ("bot:read", "Read bot API surface.", True),
    ("bot:write", "Write bot API surface.", True),
    ("crm:read", "Read the CRM", True),
]


def test_retiring_the_bot_schema_is_audited_and_reversible(migration_db_url: str) -> None:
    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    catalog_at_0011 = asyncio.run(_bot_catalog(migration_db_url))
    asyncio.run(_run_on_server(migration_db_url, _SEED_BOT_STATE))

    _alembic(migration_db_url, "upgrade", "head")

    assert asyncio.run(_schemas(migration_db_url)) == _APPLICATION_SCHEMAS
    assert asyncio.run(_rows(migration_db_url, _GRANTS)) == [("operator", "crm:read", "delegated")]
    assert asyncio.run(_rows(migration_db_url, _SCOPES)) == [("crm:read", "Read the CRM", True)]
    # One entry per deleted grant, worded as revoke_application_client_scope words a removal.
    assert asyncio.run(_rows(migration_db_url, _AUDIT)) == [
        (
            "application",
            _SKILLBOT_ID,
            "scope_grant.removed",
            True,
            "Removed scope bot:read in application mode from application client skillbot.",
        ),
        (
            "application",
            _OPERATOR_ID,
            "scope_grant.removed",
            True,
            "Removed scope bot:read in delegated mode from application client operator.",
        ),
        (
            "application",
            _SKILLBOT_ID,
            "scope_grant.removed",
            True,
            "Removed scope bot:write in application mode from application client skillbot.",
        ),
    ]

    _alembic(migration_db_url, "downgrade", _REVISION_0011)

    # The structure of 0011 and the two scope rows come back - no data, no grant.
    assert asyncio.run(_bot_catalog(migration_db_url)) == catalog_at_0011
    assert asyncio.run(_rows(migration_db_url, _SCOPES)) == _ALL_SEEDED_SCOPES
    assert asyncio.run(_rows(migration_db_url, _GRANTS)) == [("operator", "crm:read", "delegated")]
    assert asyncio.run(_rows(migration_db_url, "SELECT count(*) FROM bot.job")) == [(0,)]

    # Upgraded again, there is nothing left to revoke.
    _alembic(migration_db_url, "upgrade", "head")
    assert "bot" not in asyncio.run(_schemas(migration_db_url))
    assert len(asyncio.run(_rows(migration_db_url, _AUDIT))) == 3


def test_an_unknown_object_in_the_bot_schema_fails_the_upgrade_and_changes_nothing(migration_db_url: str) -> None:
    _alembic(migration_db_url, "upgrade", _REVISION_0011)
    asyncio.run(_run_on_server(migration_db_url, _SEED_BOT_STATE))
    asyncio.run(_run_on_server(migration_db_url, "CREATE TABLE bot.stray (id integer PRIMARY KEY)"))
    catalog_before = asyncio.run(_bot_catalog(migration_db_url))

    refused = _alembic_result(migration_db_url, "upgrade", "head")

    assert refused.returncode != 0
    assert "cannot drop schema bot because other objects depend on it" in refused.stderr
    # One transaction: neither 0012 nor 0013 happened, and every grant, scope, row and table is still there.
    version = "SELECT version_num FROM public.alembic_version"
    assert asyncio.run(_rows(migration_db_url, version)) == [(_REVISION_0011,)]
    assert asyncio.run(_bot_catalog(migration_db_url)) == catalog_before
    assert asyncio.run(_rows(migration_db_url, _GRANTS)) == _ALL_SEEDED_GRANTS
    assert asyncio.run(_rows(migration_db_url, _SCOPES)) == _ALL_SEEDED_SCOPES
    assert asyncio.run(_rows(migration_db_url, _AUDIT)) == []
    assert asyncio.run(_rows(migration_db_url, "SELECT count(*) FROM bot.job")) == [(1,)]


_REVISION_0013 = "0013_retire_bot"
_REVISION_0014 = "0014_link_code_purpose"
_PURPOSES_WITH_LINK_CODES = ["invitation", "password_reset", "discord_link"]
_PURPOSES_WITHOUT_LINK_CODES = _PURPOSES_WITH_LINK_CODES[:2]

# An account holding a token of every purpose: the downgrade removes the link code and keeps the other two.
_SEED_ONE_TOKEN_PER_PURPOSE = f"""
INSERT INTO core.party (id, type) VALUES ('{_PARTY_ID}', 'PERSON');
INSERT INTO auth.user_account (id, party_id) VALUES ('{_ACCOUNT_ID}', '{_PARTY_ID}');
INSERT INTO auth.user_action_token (id, user_account_id, purpose, token_hash, expires_at, issued_by) VALUES
    (gen_random_uuid(), '{_ACCOUNT_ID}', 'invitation', 'invitation-hash', now() + interval '7 days', 'cli'),
    (gen_random_uuid(), '{_ACCOUNT_ID}', 'password_reset', 'reset-hash', now() + interval '1 day', 'cli'),
    (gen_random_uuid(), '{_ACCOUNT_ID}', 'discord_link', 'link-code-hash', now() + interval '1 day', 'cli');
"""


def _token_purposes(url: str) -> list[str]:
    return asyncio.run(_enum_labels(url, "public", "user_action_token_purpose"))


def test_the_link_code_purpose_migration_is_reversible(migration_db_url: str) -> None:
    # `alembic check` is blind to enum labels, so assert them - and the tokens on either side: the downgrade deletes
    # the link codes and recreates the type without the label; the other tokens survive the recast.
    _alembic(migration_db_url, "upgrade", _REVISION_0014)
    assert _token_purposes(migration_db_url) == _PURPOSES_WITH_LINK_CODES
    asyncio.run(_run_on_server(migration_db_url, _SEED_ONE_TOKEN_PER_PURPOSE))

    _alembic(migration_db_url, "downgrade", _REVISION_0013)
    assert _token_purposes(migration_db_url) == _PURPOSES_WITHOUT_LINK_CODES
    tokens = "SELECT purpose::text, token_hash FROM auth.user_action_token ORDER BY token_hash"
    assert asyncio.run(_rows(migration_db_url, tokens)) == [
        ("invitation", "invitation-hash"),
        ("password_reset", "reset-hash"),
    ]

    _alembic(migration_db_url, "upgrade", _REVISION_0014)
    assert _token_purposes(migration_db_url) == _PURPOSES_WITH_LINK_CODES


# --- 0015_billing_lesson_rate (billing spec, SF2-1) ---

_REVISION_0015 = "0015_billing_lesson_rate"
_RATE_PARTY_ID = "77777777-7777-7777-7777-777777777777"
_BILLING_RELATIONS = """
SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'billing'::regnamespace ORDER BY 1
"""
# `alembic check` compares neither a check's expression nor every foreign-key option: pin each constraint's definition.
_BILLING_CONSTRAINTS = """
SELECT t.relname, k.conname, pg_get_constraintdef(k.oid) FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid
WHERE k.connamespace = 'billing'::regnamespace AND k.contype IN ('c', 'f', 'u')
ORDER BY 1, 2
"""
_BILLING_CONSTRAINTS_AT_0015 = [
    (
        "lesson",
        "ck_lesson_billable_status",
        "CHECK (((NOT billable) OR "
        "(status = ANY (ARRAY['held'::billing.lesson_status, 'cancelled'::billing.lesson_status]))))",
    ),
    (
        "lesson",
        "ck_lesson_quarter_hour_cents",
        "CHECK (((rate_cents_per_hour >= 0) AND ((rate_cents_per_hour % 4) = 0)))",
    ),
    ("lesson", "ck_lesson_quarter_hour_minutes", "CHECK (((minutes > 0) AND ((minutes % 15) = 0)))"),
    (
        "lesson",
        "lesson_student_party_id_fkey",
        "FOREIGN KEY (student_party_id) REFERENCES core.party(id) ON DELETE RESTRICT",
    ),
    ("lesson", "uq_lesson_student_start", "UNIQUE (student_party_id, starts_at)"),
    ("rate", "ck_rate_quarter_hour_cents", "CHECK (((cents_per_hour >= 0) AND ((cents_per_hour % 4) = 0)))"),
    (
        "rate",
        "rate_student_party_id_fkey",
        "FOREIGN KEY (student_party_id) REFERENCES core.party(id) ON DELETE RESTRICT",
    ),
    ("rate", "uq_rate_student_valid_from", "UNIQUE (student_party_id, valid_from)"),
]
# What rule G accepts, at its edges: a 15-minute lesson (SP-CAL recorded them), an own rate of 0 (PD3), no own rate,
# and rates of 0 and 4. One party holds only lessons, the other only rates, so each foreign key refuses on its own.
_BILLING_SEED = f"""
INSERT INTO core.party (id, type) VALUES ('{_PARTY_ID}', 'PERSON'), ('{_RATE_PARTY_ID}', 'PERSON');
INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes, rate_cents_per_hour) VALUES
    (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-02 15:00+00', 15, 0),
    (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-03 15:00+00', 45, NULL),
    (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-04 15:00+00', 120, 3204);
INSERT INTO billing.rate (id, student_party_id, cents_per_hour, valid_from) VALUES
    (gen_random_uuid(), '{_RATE_PARTY_ID}', 0, '2026-11-01'),
    (gen_random_uuid(), '{_RATE_PARTY_ID}', 4, '2026-12-01');
"""
_SEEDED_LESSONS = "SELECT minutes, rate_cents_per_hour, status::text, billable FROM billing.lesson ORDER BY starts_at"
_SEEDED_RATES = "SELECT cents_per_hour FROM billing.rate ORDER BY valid_from"
# Each statement is refused by exactly the constraint named beside it.
_BILLING_REFUSALS = [
    (
        f"INSERT INTO billing.rate (id, student_party_id, cents_per_hour, valid_from) "
        f"VALUES (gen_random_uuid(), '{_RATE_PARTY_ID}', 3001, '2027-01-01')",
        "ck_rate_quarter_hour_cents",
    ),
    (
        f"INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes) "
        f"VALUES (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-05 15:00+00', 50)",
        "ck_lesson_quarter_hour_minutes",
    ),
    (
        f"INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes, rate_cents_per_hour) "
        f"VALUES (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-05 15:00+00', 60, 3002)",
        "ck_lesson_quarter_hour_cents",
    ),
    (
        f"INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes, billable) "
        f"VALUES (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-05 15:00+00', 60, true)",
        "ck_lesson_billable_status",
    ),
    # RESTRICT: a party keeps its lessons and its rates.
    (f"DELETE FROM core.party WHERE id = '{_PARTY_ID}'", "lesson_student_party_id_fkey"),
    (f"DELETE FROM core.party WHERE id = '{_RATE_PARTY_ID}'", "rate_student_party_id_fkey"),
]


async def _refusing_constraint(url: str, statement: str) -> str | None:
    with pytest.raises(asyncpg.exceptions.IntegrityConstraintViolationError) as refused:
        await _run_on_server(url, statement)
    return getattr(refused.value, "constraint_name", None)


def test_the_billing_revision_is_reversible_and_holds_the_quarter_hour_rule(migration_db_url: str) -> None:
    # `alembic check` is blind to check expressions and enum labels, so assert them on the migrated database - the
    # labels against the model's - and what they accept and refuse; the downgrade by one drops both tables and the
    # enum type and leaves the schema to env.py, empty.
    _alembic(migration_db_url, "upgrade", _REVISION_0015)
    labels = asyncio.run(_enum_labels(migration_db_url, "billing", "lesson_status"))
    assert labels == [status.value for status in LessonStatus]
    assert asyncio.run(_rows(migration_db_url, _BILLING_CONSTRAINTS)) == _BILLING_CONSTRAINTS_AT_0015

    asyncio.run(_run_on_server(migration_db_url, _BILLING_SEED))
    assert asyncio.run(_rows(migration_db_url, _SEEDED_LESSONS)) == [
        (15, 0, "planned", False),
        (45, None, "planned", False),
        (120, 3204, "planned", False),
    ]
    assert asyncio.run(_rows(migration_db_url, _SEEDED_RATES)) == [(0,), (4,)]
    for statement, constraint in _BILLING_REFUSALS:
        assert asyncio.run(_refusing_constraint(migration_db_url, statement)) == constraint

    _alembic(migration_db_url, "downgrade", _REVISION_0014)
    assert asyncio.run(_rows(migration_db_url, _BILLING_RELATIONS)) == []
    assert asyncio.run(_enum_labels(migration_db_url, "billing", "lesson_status")) == []
    assert "billing" in asyncio.run(_schemas(migration_db_url))

    _alembic(migration_db_url, "upgrade", _REVISION_0015)
    assert asyncio.run(_rows(migration_db_url, _BILLING_CONSTRAINTS)) == _BILLING_CONSTRAINTS_AT_0015


# --- 0016_billing_payer (billing spec, SF3-1) ---

_REVISION_0016 = "0016_billing_payer"
_COMPANY_PAYER_ID = "88888888-8888-8888-8888-888888888888"
_MANUAL_PAYER_ID = "99999999-9999-9999-9999-999999999999"
_BILLING_CONSTRAINTS_AT_0016 = [
    *_BILLING_CONSTRAINTS_AT_0015,
    (
        "lesson",
        "lesson_payer_party_id_fkey",
        "FOREIGN KEY (payer_party_id) REFERENCES core.party(id) ON DELETE RESTRICT",
    ),
    (
        "manual_payer",
        "manual_payer_party_id_fkey",
        "FOREIGN KEY (party_id) REFERENCES core.party(id) ON DELETE RESTRICT",
    ),
]
# What the schema accepts: a lesson without a payer, a billable one whose payer is a company (decision I), and a
# payer marked as billed by hand without a note. One party is only a lesson's payer, the other only marked, so each
# foreign key refuses on its own.
_PAYER_SEED = f"""
INSERT INTO core.party (id, type) VALUES
    ('{_PARTY_ID}', 'PERSON'), ('{_COMPANY_PAYER_ID}', 'COMPANY'), ('{_MANUAL_PAYER_ID}', 'COMPANY');
INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes, status, billable, payer_party_id) VALUES
    (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-02 15:00+00', 60, 'planned', false, NULL),
    (gen_random_uuid(), '{_PARTY_ID}', 'Mathe', '2026-11-03 15:00+00', 60, 'held', true, '{_COMPANY_PAYER_ID}');
INSERT INTO billing.manual_payer (party_id) VALUES ('{_MANUAL_PAYER_ID}');
"""
_SEEDED_PAYERS = """
SELECT l.payer_party_id::text, p.type::text FROM billing.lesson l LEFT JOIN core.party p ON p.id = l.payer_party_id
ORDER BY l.starts_at
"""
_SEEDED_MARKS = "SELECT party_id::text, note FROM billing.manual_payer"
_PAYER_REFUSALS = [
    (f"INSERT INTO billing.manual_payer (party_id) VALUES ('{_MANUAL_PAYER_ID}')", "manual_payer_pkey"),
    (f"INSERT INTO billing.manual_payer (party_id) VALUES ('{uuid.uuid4()}')", "manual_payer_party_id_fkey"),
    # RESTRICT: a party keeps the lessons it pays for and its mark.
    (f"DELETE FROM core.party WHERE id = '{_COMPANY_PAYER_ID}'", "lesson_payer_party_id_fkey"),
    (f"DELETE FROM core.party WHERE id = '{_MANUAL_PAYER_ID}'", "manual_payer_party_id_fkey"),
]


def _billing_constraints(url: str) -> list[tuple[object, ...]]:
    return sorted(asyncio.run(_rows(url, _BILLING_CONSTRAINTS)))


def test_the_payer_revision_is_reversible_and_restricts_both_party_references(migration_db_url: str) -> None:
    # `alembic check` compares no foreign key's ON DELETE: pin every definition, and what the schema accepts and
    # refuses; the downgrade by one leaves the 0015 schema, the seeded lessons with it.
    _alembic(migration_db_url, "upgrade", _REVISION_0016)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0016)
    assert asyncio.run(_primary_key_columns(migration_db_url, "billing", "manual_payer")) == ["party_id"]

    asyncio.run(_run_on_server(migration_db_url, _PAYER_SEED))
    assert asyncio.run(_rows(migration_db_url, _SEEDED_PAYERS)) == [(None, None), (_COMPANY_PAYER_ID, "COMPANY")]
    assert asyncio.run(_rows(migration_db_url, _SEEDED_MARKS)) == [(_MANUAL_PAYER_ID, None)]
    for statement, constraint in _PAYER_REFUSALS:
        assert asyncio.run(_refusing_constraint(migration_db_url, statement)) == constraint

    _alembic(migration_db_url, "downgrade", _REVISION_0015)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0015)
    relations = {name for (name,) in asyncio.run(_rows(migration_db_url, _BILLING_RELATIONS))}
    assert {"lesson", "rate"} <= relations
    assert relations.isdisjoint({"manual_payer", "manual_payer_pkey", "ix_lesson_payer_party_id"})
    assert asyncio.run(_rows(migration_db_url, "SELECT count(*) FROM billing.lesson")) == [(2,)]

    _alembic(migration_db_url, "upgrade", _REVISION_0016)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0016)


# --- 0017_billing_calendar_alias (billing spec, SF4-2) ---

_REVISION_0017 = "0017_billing_calendar_alias"
_ALIAS_STUDENT_ID = "66666666-6666-6666-6666-666666666666"
_BILLING_CONSTRAINTS_AT_0017 = [
    *_BILLING_CONSTRAINTS_AT_0016,
    ("calendar_alias", "calendar_alias_name_normalized_key", "UNIQUE (name_normalized)"),
    (
        "calendar_alias",
        "calendar_alias_student_party_id_fkey",
        "FOREIGN KEY (student_party_id) REFERENCES core.party(id) ON DELETE RESTRICT",
    ),
]
# Two names of one student; a third row repeats a name, a fourth names no party, and the delete of the student is
# refused by the foreign key's RESTRICT.
_ALIAS_SEED = f"""
INSERT INTO core.party (id, type) VALUES ('{_ALIAS_STUDENT_ID}', 'PERSON');
INSERT INTO billing.calendar_alias (id, name_normalized, student_party_id) VALUES
    (gen_random_uuid(), 'student b', '{_ALIAS_STUDENT_ID}'),
    (gen_random_uuid(), 'student b senior', '{_ALIAS_STUDENT_ID}');
"""
_ALIAS_REFUSALS = [
    (
        f"INSERT INTO billing.calendar_alias (id, name_normalized, student_party_id) "
        f"VALUES (gen_random_uuid(), 'student b', '{_ALIAS_STUDENT_ID}')",
        "calendar_alias_name_normalized_key",
    ),
    (
        f"INSERT INTO billing.calendar_alias (id, name_normalized, student_party_id) "
        f"VALUES (gen_random_uuid(), 'student c', '{uuid.uuid4()}')",
        "calendar_alias_student_party_id_fkey",
    ),
    (f"DELETE FROM core.party WHERE id = '{_ALIAS_STUDENT_ID}'", "calendar_alias_student_party_id_fkey"),
]


def test_the_calendar_alias_revision_is_reversible_and_restricts_its_student(migration_db_url: str) -> None:
    # `alembic check` compares no foreign key's ON DELETE: pin every definition, and what the schema accepts and
    # refuses; the downgrade by one leaves the 0016 schema.
    _alembic(migration_db_url, "upgrade", _REVISION_0017)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0017)
    relations = {name for (name,) in asyncio.run(_rows(migration_db_url, _BILLING_RELATIONS))}
    assert {"calendar_alias", "calendar_alias_pkey", "ix_calendar_alias_student_party_id"} <= relations

    asyncio.run(_run_on_server(migration_db_url, _ALIAS_SEED))
    assert asyncio.run(_rows(migration_db_url, "SELECT count(*) FROM billing.calendar_alias")) == [(2,)]
    for statement, constraint in _ALIAS_REFUSALS:
        assert asyncio.run(_refusing_constraint(migration_db_url, statement)) == constraint

    _alembic(migration_db_url, "downgrade", _REVISION_0016)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0016)
    relations = {name for (name,) in asyncio.run(_rows(migration_db_url, _BILLING_RELATIONS))}
    assert {"lesson", "rate", "manual_payer"} <= relations
    assert relations.isdisjoint({"calendar_alias", "calendar_alias_pkey", "ix_calendar_alias_student_party_id"})

    _alembic(migration_db_url, "upgrade", _REVISION_0017)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0017)


# --- 0018_billing_calendar_event (billing spec, SF4-3) ---

_REVISION_0018 = "0018_billing_calendar_event"
_EVENT_STUDENT_ID = "55555555-5555-5555-5555-555555555555"
_EVENT_ID = "44444444-4444-4444-4444-444444444444"
_WAITING_EVENT_ID = "33333333-3333-3333-3333-333333333333"
_BILLING_CONSTRAINTS_AT_0018 = [
    *_BILLING_CONSTRAINTS_AT_0017,
    ("calendar_event", "calendar_event_external_id_key", "UNIQUE (external_id)"),
    (
        "calendar_event",
        "ck_calendar_event_reason",
        "CHECK ((reason = ANY (ARRAY['all_day'::text, 'phone'::text, 'first_meeting'::text, "
        "'no_title_pattern'::text, 'duration'::text, 'unmatched'::text, 'ambiguous'::text])))",
    ),
    (
        "calendar_event",
        "ck_calendar_event_title_name",
        "CHECK (((title_name IS NULL) OR ((NOT dismissed) AND (reason IS NOT NULL) AND "
        "(reason = ANY (ARRAY['duration'::text, 'unmatched'::text, 'ambiguous'::text])))))",
    ),
    (
        "lesson",
        "lesson_calendar_event_id_fkey",
        "FOREIGN KEY (calendar_event_id) REFERENCES billing.calendar_event(id) ON DELETE RESTRICT",
    ),
    ("lesson", "lesson_calendar_event_id_key", "UNIQUE (calendar_event_id)"),
]
_EVENT_RELATIONS = {"calendar_event", "calendar_event_pkey", "calendar_event_external_id_key"}
_EVENT_RELATIONS |= {"ix_calendar_event_starts_at", "lesson_calendar_event_id_key"}
# What the schema accepts: an occurrence with its lesson, one waiting with its name, a dismissed one without a name,
# one with no reason and no lesson (it collided with a lesson added by hand), and a lesson without an occurrence.
_EVENT_SEED = f"""
INSERT INTO core.party (id, type) VALUES ('{_EVENT_STUDENT_ID}', 'PERSON');
INSERT INTO billing.calendar_event (id, external_id, starts_at, ends_at, title_subject, title_name, cancelled, reason,
                                    dismissed, changed, last_seen_at) VALUES
    ('{_EVENT_ID}', 'event-a', '2026-11-02 15:00+00', '2026-11-02 16:00+00', 'Mathe', NULL, false, NULL,
     false, false, now()),
    ('{_WAITING_EVENT_ID}', 'event-b', '2026-11-03 15:00+00', '2026-11-03 15:50+00', 'Mathe', 'Student B', false,
     'duration', false, false, now()),
    (gen_random_uuid(), 'event-c', '2026-11-04 15:00+00', '2026-11-04 16:00+00', NULL, NULL, true, 'no_title_pattern',
     true, false, now()),
    (gen_random_uuid(), 'event-d', '2026-11-05 15:00+00', '2026-11-05 16:00+00', 'Mathe', NULL, false, NULL,
     false, true, now());
INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes, calendar_event_id) VALUES
    (gen_random_uuid(), '{_EVENT_STUDENT_ID}', 'Mathe', '2026-11-02 15:00+00', 60, '{_EVENT_ID}'),
    (gen_random_uuid(), '{_EVENT_STUDENT_ID}', 'Mathe', '2026-11-05 15:00+00', 60, NULL);
"""
_SEEDED_EVENTS = "SELECT external_id, dismissed, changed FROM billing.calendar_event ORDER BY starts_at"
_EVENT_REFUSALS = [
    (
        "INSERT INTO billing.calendar_event (id, external_id, starts_at, ends_at, cancelled, last_seen_at) "
        "VALUES (gen_random_uuid(), 'event-a', now(), now(), false, now())",
        "calendar_event_external_id_key",
    ),
    (
        "INSERT INTO billing.calendar_event (id, external_id, starts_at, ends_at, cancelled, reason, last_seen_at) "
        "VALUES (gen_random_uuid(), 'event-e', now(), now(), false, 'holiday', now())",
        "ck_calendar_event_reason",
    ),
    # PD13: a name only while the occurrence waits - never for all-day, phone and first-meeting occurrences, never
    # once it has a lesson (no reason) or is dismissed.
    *(
        (
            "INSERT INTO billing.calendar_event (id, external_id, starts_at, ends_at, title_name, cancelled, reason, "
            f"dismissed, last_seen_at) VALUES (gen_random_uuid(), 'event-{index}', now(), now(), 'Student B', false, "
            f"{reason}, {dismissed}, now())",
            "ck_calendar_event_title_name",
        )
        for index, (reason, dismissed) in enumerate((
            ("'all_day'", "false"),
            ("'phone'", "false"),
            ("'first_meeting'", "false"),
            ("'no_title_pattern'", "false"),
            ("NULL", "false"),
            ("'unmatched'", "true"),
        ))
    ),
    # One lesson per occurrence, an occurrence that exists, and RESTRICT: no occurrence goes before its lesson.
    (
        f"INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes, calendar_event_id) "
        f"VALUES (gen_random_uuid(), '{_EVENT_STUDENT_ID}', 'Mathe', '2026-11-06 15:00+00', 60, '{_EVENT_ID}')",
        "lesson_calendar_event_id_key",
    ),
    (
        f"INSERT INTO billing.lesson (id, student_party_id, subject, starts_at, minutes, calendar_event_id) "
        f"VALUES (gen_random_uuid(), '{_EVENT_STUDENT_ID}', 'Mathe', '2026-11-06 15:00+00', 60, '{uuid.uuid4()}')",
        "lesson_calendar_event_id_fkey",
    ),
    (f"DELETE FROM billing.calendar_event WHERE id = '{_EVENT_ID}'", "lesson_calendar_event_id_fkey"),
]


def test_the_calendar_event_revision_is_reversible_and_keeps_names_only_while_waiting(migration_db_url: str) -> None:
    # `alembic check` compares neither a check's expression nor a foreign key's ON DELETE: pin every definition, and
    # what the schema accepts and refuses; the downgrade by one leaves the 0017 schema, the lessons with it.
    _alembic(migration_db_url, "upgrade", _REVISION_0018)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0018)
    relations = {name for (name,) in asyncio.run(_rows(migration_db_url, _BILLING_RELATIONS))}
    assert _EVENT_RELATIONS <= relations

    asyncio.run(_run_on_server(migration_db_url, _EVENT_SEED))
    assert asyncio.run(_rows(migration_db_url, _SEEDED_EVENTS)) == [
        ("event-a", False, False),
        ("event-b", False, False),
        ("event-c", True, False),
        ("event-d", False, True),
    ]
    for statement, constraint in _EVENT_REFUSALS:
        assert asyncio.run(_refusing_constraint(migration_db_url, statement)) == constraint
    # An occurrence without a lesson can go.
    asyncio.run(
        _run_on_server(migration_db_url, f"DELETE FROM billing.calendar_event WHERE id = '{_WAITING_EVENT_ID}'")
    )

    _alembic(migration_db_url, "downgrade", _REVISION_0017)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0017)
    relations = {name for (name,) in asyncio.run(_rows(migration_db_url, _BILLING_RELATIONS))}
    assert {"lesson", "rate", "manual_payer", "calendar_alias"} <= relations
    assert relations.isdisjoint(_EVENT_RELATIONS)
    assert asyncio.run(_rows(migration_db_url, "SELECT count(*) FROM billing.lesson")) == [(2,)]

    _alembic(migration_db_url, "upgrade", _REVISION_0018)
    assert _billing_constraints(migration_db_url) == sorted(_BILLING_CONSTRAINTS_AT_0018)
