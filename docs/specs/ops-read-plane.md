# Spec: Ops read plane (jobs & operations)

> Status: Superseded by [bot-decoupling.md](bot-decoupling.md) (2026-09); was Implemented
> Tracking: [#49](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/49)
> First delivered slice of **Arc 2 (ops plane)** in [`lifecycle-guardian.md`](lifecycle-guardian.md).

## Problem statement

Forge exposes no read access to its two state machines. The job queue (`bot.job`) only offers the
bot's own `claim`/`complete`/`fail`, and two-phase transitions (`bot.operation`) only offer
`prepare`/`commit`; dead letters are reachable solely through a `just` CLI. After a restart or deploy
the bot cannot ask *"do I have `PREPARED`-but-uncommitted operations to reconcile?"* or *"what is the
status of job X?"*, and operators have no queue-depth/funnel view (the reaper emits logs only). This
is the read/observability seed of Arc 2.

## Goals

1. **Operation reconciliation.** The bot can fetch a single operation by id (with its `plan`) and list
   operations filtered by subject / status / kind, so after a crash it can find its outstanding
   `PREPARED` reservations and finish or abandon them.
2. **Job observability.** Operators/consumers can fetch a job's status by id and read a queue summary
   (funnel across statuses, broken down per kind) to spot anomalies early.
3. **Bounded reads.** List endpoints paginate, so a growing table never returns an unbounded payload.
4. **No new trust surface.** All read endpoints reuse the existing `BotRead` scope; no new scope,
   client grant, error type, or migration.

## Acceptance criteria

- [x] `GET` an operation by id, plus a filtered list (by subject / status / kind) for restart reconciliation.
- [x] `GET` job status by id, plus a queue summary view (depth + status funnel, global and per kind).
- [x] Read scope (`BotRead`) enforced on every read endpoint.
- [x] Pagination (`limit` / `offset` + `total`) on the list endpoints.

## Non-goals

- **Audit-log search / event history.** Arc 2 mentions it; parked. This slice is jobs + operations only.
- **Metrics export (Prometheus) / alerting.** Structured reaper logs remain the operational signal;
  a pull-based summary endpoint is the read counterpart, nothing is pushed.
- **Cursor pagination.** Offset/limit is enough for the current table sizes; revisit if a table grows
  large enough that deep offsets hurt.
- **Retention / archiving.** Terminal rows are retained (per the guardian spec) but not purged here.
- **Writes of any kind.** Purely additive read surface; no state changes.

## API

All endpoints require the existing **`BotRead`** scope and live under the `/api/v1/bot` prefix.
Pagination is `limit` (default 50, 1–100) + `offset` (≥ 0); list responses wrap items in
`{ items, total, limit, offset }`. Lists are ordered `created_at desc, <pk> desc` for stable paging.

### Operations — new router `app/api/v1/bot/operations.py` (`/operations`)

| Method & path | Response | Notes |
|---|---|---|
| `GET /operations/{operation_id}` | `OperationResponse` (full, **incl. `plan`**) | 404 → `OperationNotFoundError` |
| `GET /operations` | `Page[OperationSummary]` (**no `plan`**) | Filters (optional, AND-combined): `guild_id`, `subject_discord_id`, `status`, `kind` |

The subject of an operation is the pair `(guild_id, subject_discord_id)`; both are exposed as
independent optional filters (the `ix_operation_subject` index covers the `guild_id` prefix).
The heavy `plan` JSONB is returned only on the by-id detail, keeping list pages light — the bot lists
`PREPARED` operations, then GETs the ones it wants to reconcile by id to read their plan.

### Jobs — extend router `app/api/v1/bot/jobs.py` (`/jobs`) with read endpoints

| Method & path | Response | Notes |
|---|---|---|
| `GET /jobs/summary` | `JobQueueSummary` | Optional `kind` filter; declared **before** `/{job_id}` to avoid path capture |
| `GET /jobs/{job_id}` | `JobDetail` (full, **incl. `payload`**) | 404 → `JobNotFoundError` |
| `GET /jobs` | `Page[JobListItem]` (**no `payload`**) | Filters (optional, AND): `status`, `kind` |

`JobQueueSummary` is a queue-depth funnel with a per-kind breakdown:

```json
{
  "total": 42,
  "open": 7,
  "statuses": { "pending": 5, "claimed": 2, "completed": 33, "failed": 2 },
  "by_kind": [
    {
      "kind": "student_activate",
      "total": 25,
      "open": 4,
      "statuses": { "pending": 3, "claimed": 1, "completed": 20, "failed": 1 }
    }
  ]
}
```

`open` is outstanding work — `pending` (including jobs delayed by retry backoff) plus `claimed`;
terminal counts are historical totals (rows are retained). `statuses` is zero-filled across all four
`JobStatus` values (a typed object, not an open map, so consumer codegen gets named fields); `by_kind`
is sorted by kind. `GET /jobs/summary?kind=<k>` scopes the top-level totals **and** the `by_kind`
list to that exact kind. It is a single `GROUP BY (kind, status)` query, aggregated in Python.

## Decisions

| Topic | Decision | Rationale |
|---|---|---|
| Scope | Reuse `BotRead` | Already defined, seeded, and granted to `skillbot`; read plane needs no new trust. |
| Pagination | Offset/limit + `{items, total, limit, offset}` envelope | No existing convention; simplest for current sizes. Cursor deferred (non-goal). |
| List vs detail | List items omit heavy fields (`plan` / `payload`); detail-by-id returns them | Keeps pages light and paginatable; idiomatic list/detail split. |
| Summary shape | Global depth (`total`/`open`) + typed `statuses` object + per-kind funnel, optional `kind` filter | Directly models queue depth; a typed `statuses` object gives consumer codegen named fields over an opaque map; one query serves the funnel and dead-letter-rate-per-kind signals. |
| Read module layout | Operation reads in a new `services/bot/operations.py`; job reads extend `services/bot/jobs.py` | Mirrors the read/write split (`contexts.py`/`principals.py` read vs `transitions.py` write); `jobs.py` already holds both job reads and writes. |
| Errors | Reuse `JobNotFoundError` / `OperationNotFoundError` → 404 | Both already exist and are exported. |
| Migration | None | Purely additive endpoints over existing tables/indexes. |

## Testing

- **API tests** (fast, service layer monkeypatched — no DB): scope enforcement (403 on wrong scope,
  401 unauthenticated), 404 mapping, response shape, filter/pagination passthrough. Extend
  `tests/api/test_bot_jobs_endpoint.py`; add `tests/api/test_bot_operations_endpoint.py`.
- **DB service tests** (`@pytest.mark.db`): get-by-id found/not-found, filter AND-combining, pagination
  (`total`/`limit`/`offset`), newest-first ordering, and summary aggregation (zero-fill + per-kind).
  Extend `tests/db/test_bot_jobs_service.py`; add `tests/db/test_bot_operations_service.py`.
  Bulk operation fixtures must vary `subject_discord_id`: the partial unique index
  `uq_operation_prepared_subject_kind` allows only one open `PREPARED` row per
  `(guild_id, subject_discord_id, kind)`.

## Contract

`openapi.json` is regenerated with `just openapi` and committed (ADR 0001); CI enforces no drift.

## Dependency

Builds on Arc 1 (guardian). Does not block Arc 3/4 but shares the same job/operation substrate.
