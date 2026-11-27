# ADR 0002 - Separate DB URLs for the app and migrations

Status: Accepted, 2026-05

## Context

In production the DB runs on Neon. The app uses the **pooled** connection (Neon's `-pooler` host,
PgBouncer in transaction mode) for many short requests. But PgBouncer in transaction mode breaks
two things Alembic needs:

- **advisory locks** (Alembic serializes migrations through them),
- **`CREATE INDEX CONCURRENTLY`** and similar session-bound operations.

In addition, `asyncpg` caches prepared statements by default, which also breaks behind a
transaction-mode pooler (statements don't survive the connection).

## Decision

Two connections, split by purpose:

- **`DB__URL`** - pooled, for the app runtime.
- **`DB__MIGRATION_URL`** - direct (unpooled) connection, used exclusively by Alembic. Falls back
  to `DB__URL` when unset (local/tests, where no pooler sits in between).

Accompanying: `prepared_statement_cache_size=0` is set for **every** `asyncpg` connection (app and
Alembic alike) - asyncpg's statement cache doesn't survive a transaction-mode pooler. Alembic always
uses `NullPool`; the app engine additionally switches to `NullPool` and pins a constant
prepared-statement name (`prepared_statement_name_func` returning `""`) when the `DB__URL` host is
detected as a pooler (host contains `pooler` or `pgbouncer`). Against a direct host it keeps
SQLAlchemy's normal pool.

See `app/core/db/config.py`, `app/core/db/database.py`, `migrations/env.py`.

## Consequences

- Migrations run safely against the direct connection; the app keeps benefiting from pooling.
- Two URLs to maintain (deploy: `DB__MIGRATION_URL` for the one-shot `migrate` service,
  `DB__URL` for the app and reaper).
- Nothing extra is needed locally - the fallback kicks in.

Introduced in PR #25.

## Update (2026-11)

Production runs on SkillDB since 2026-11: Postgres as a Dokploy database service, without a pooler, so
`DB__MIGRATION_URL` is unset and the app and migrations both use `DB__URL`. The principle still holds: behind a
transaction-mode pooler, migrations need the direct URL.
