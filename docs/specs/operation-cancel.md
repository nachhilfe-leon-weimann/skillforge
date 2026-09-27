# Spec: Cancel a prepared operation

> Status: Superseded by [bot-decoupling.md](bot-decoupling.md) (2026-09); was Implemented
> Tracking: [#50](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/50)
> Write counterpart to [`ops-read-plane.md`](ops-read-plane.md); continues **Arc 2 (ops plane)**
> from [`lifecycle-guardian.md`](lifecycle-guardian.md).

## Problem statement

When SkillBot `prepare`s an operation and the action is then aborted (e.g. the user cancels the
slash command before the bot commits), there is no explicit release. The reservation lingers as
`PREPARED`, holding its capacity slot and the `(guild, subject, kind)` reservation, until it
expires by TTL (`OPERATION_TTL` = 10 min) and the sweeper materializes it to `EXPIRED`. Until
then capacity is over-counted and a fresh `prepare` for the same natural key is blocked by the
partial unique index `uq_operation_prepared_subject_kind`. An explicit cancel frees both
immediately -- and is semantically distinct from a timeout.

## Goals

1. **Immediate release.** A `PREPARED` operation can be cancelled by id, flipping it to a terminal
   state at once. Capacity counts only non-expired `PREPARED` rows, so this frees the slot, and it
   drops the `uq_operation_prepared_subject_kind` reservation, so a new `prepare` for the same
   `(guild, subject, kind)` succeeds immediately.
2. **Intent preserved.** Cancel is recorded as a distinct terminal status `CANCELLED` (with a
   `cancelled_at` timestamp), so the ops read plane / audit can tell an explicit abort from a TTL
   expiry.
3. **Idempotent & safe.** Retrying a cancel on an already-`CANCELLED` operation is a no-op success;
   any other non-`PREPARED` state is rejected without side effects.
4. **Minimal trust surface.** Reuses the existing `BotWrite` scope and the existing
   `OperationNotFoundError` / `OperationNotPendingError` errors; no new scope, client grant, or
   error type.

## Acceptance criteria

- [x] `POST /api/v1/bot/operations/{operation_id}/cancel` moves a `PREPARED` operation to terminal
      `CANCELLED`.
- [x] Frees the capacity slot and the reservation immediately (a subsequent `prepare` for the same
      natural key succeeds).
- [x] Idempotent on an already-`CANCELLED` operation; safe (clean `409`) on any other terminal state.
- [x] Only `PREPARED` operations are actively cancellable.

## API

Requires the existing **`BotWrite`** scope; lives on the existing `/api/v1/bot/operations` router
(its first write endpoint).

| Method & path | Body | Response | Notes |
|---|---|---|---|
| `POST /operations/{operation_id}/cancel` | -- | `OperationCancelResponse` | 404 -> `OperationNotFoundError`; 409 -> `OperationNotPendingError` |

`OperationCancelResponse` is lean, mirroring `TransitionCommitResponse` (which returns
`committed_at`):

```json
{ "operation_id": "...", "kind": "student_activate", "status": "cancelled", "cancelled_at": "..." }
```

Errors map through the existing `transition_http_exception`; a new `CANCEL_RESPONSES = {404, 409}`
is added to `_transitions.py`. *(Superseded: both were removed by the
[API conventions](api-conventions.md) arc - the global exception handlers map the errors now.)*

## State semantics

`cancel_operation(session, *, operation_id)` in `services/bot/transitions.py` -- kind-agnostic,
identified purely by id, mirroring `commit`'s precondition handling:

| Starting state | Outcome | HTTP |
|---|---|---|
| unknown id | `OperationNotFoundError` | 404 |
| `PREPARED`, `expires_at > now` | -> `CANCELLED`, set `cancelled_at`, return | 200 |
| `PREPARED`, `expires_at <= now` | lazy-materialize -> `EXPIRED`, `OperationNotPendingError` | 409 |
| `CANCELLED` | idempotent replay: return unchanged | 200 |
| `COMMITTED` / `EXPIRED` / `FAILED` | `OperationNotPendingError` | 409 |

The lazy-expiry branch and the `EXPIRED -> 409` decision match `commit` exactly (see
`_load_prepared_operation`): an expired reservation reads as `EXPIRED`, never as `CANCELLED`, even
if the cancel arrives just after the TTL. Only `PREPARED -> CANCELLED` is an actual write; every
other path either leaves state untouched (the `CANCELLED` replay) or applies the same expiry the
sweeper would.

## Data model & migration

- `OperationStatus` gains `CANCELLED = "cancelled"`.
- `Operation` gains `cancelled_at: datetime | None`, symmetric with `committed_at` / `failed_at`
  and set by `cancel_operation` (mirrors `_mark_committed`). Exposed in `OperationSummary` and
  `OperationResponse` for read-plane completeness.
- **Migration `0009`**:
  - `upgrade`: `ALTER TYPE bot.operation_status ADD VALUE IF NOT EXISTS 'cancelled'` (PG12+ in-tx;
    the value is not *used* in the same tx -- same pattern as `0007`) plus
    `add_column operation.cancelled_at`.
  - `downgrade`: `drop_column cancelled_at`, then recreate `operation_status` without `cancelled`
    via the rename-aside pattern from `0007`. Because `operation.status` carries a `server_default`,
    the sequence is `DROP DEFAULT` -> `ALTER COLUMN ... TYPE ...` (re-cast) -> `SET DEFAULT
    'prepared'` -> `DROP TYPE ..._old`. The re-cast fails if any row still uses `cancelled` -- an
    accepted limitation of downgrading past this data.

## Non-goals

- **Cancelling a committed operation / undo.** `COMMITTED` is not reversible via cancel; tearing
  down provisioned state is what the `*_deactivate` transitions are for.
- **A cancel reason / actor.** No free-text reason or actor is recorded; `status` + `cancelled_at`
  is the whole signal. (`last_error` stays reserved for a future operation dead-letter path,
  alongside the currently-unused `FAILED` / `failed_at`.)
- **Per-kind cancel endpoints.** Cancel is uniform across kinds; one generic by-id endpoint
  replaces six.
- **Auto-cancel hooks.** Forge exposes the endpoint; deciding *when* to cancel (slash-command
  abort, bot-side timeout) is SkillBot's call.

## Decisions

| Topic | Decision | Rationale |
|---|---|---|
| Terminal state | New `CANCELLED` (not reuse `EXPIRED`) | Preserves abort-vs-timeout intent for the Arc 2 ops/observability plane; the motivating point of the issue. |
| `cancelled_at` | Add the column | Matches the per-terminal-timestamp convention (`committed_at`/`failed_at`); records *when*, which is the point of distinguishing intent. |
| Already-`EXPIRED` | `409`, not idempotent `200` | Consistent with `commit`; an expired reservation is honestly `EXPIRED`, not `CANCELLED`. |
| Endpoint shape | One generic `POST /{operation_id}/cancel` | Cancel is kind-agnostic -- no payload, no workspace mutation -- unlike per-kind `commit`. |
| Scope / errors | Reuse `BotWrite`, `OperationNotFoundError`, `OperationNotPendingError` | No new trust surface or error type. |
| Response model | New lean `OperationCancelResponse` | Mirrors `TransitionCommitResponse`; avoids returning the heavy `plan`. |
| commit↔cancel concurrency | Not row-locked (accepted) | Both `commit` and `cancel` read via `session.get` and flush an unguarded UPDATE, so a concurrent commit and cancel of the *same* operation id can lose the terminal-label write (the row may end `CANCELLED` though the workspace was created, or vice-versa). Capacity is unaffected -- it counts `StudentWorkspace` rows plus `PREPARED` operations, both robust to the divergence -- so only the audit label diverges. Cancel deliberately mirrors the existing unlocked `commit` path; hardening both with row locks is deferred. |

## Testing

- **API** (`tests/api/test_bot_operations_endpoint.py`, service monkeypatched, no DB): `401`
  unauthenticated, `403` on a `BotRead`-only token, `404` and `409` mapping, `200` response shape,
  idempotent-replay passthrough.
- **DB** (`tests/db/test_bot_operations_service.py`, `@pytest.mark.db`): `PREPARED -> CANCELLED`
  sets `cancelled_at` and frees the slot (a fresh `prepare` for the same `(guild, subject, kind)`
  then succeeds); idempotent replay on `CANCELLED`; `409` on `COMMITTED` and on `EXPIRED`; the
  lazy-expiry branch (`PREPARED` past TTL -> `EXPIRED` + `409`); `404` on unknown id.

## Contract

`openapi.json` is regenerated with `just openapi` and committed (ADR 0001); CI enforces no drift.

## Dependency

Builds on the guardian (Arc 1) and the ops read plane (`ops-read-plane.md`); shares the same
operation substrate. Independent of the batch-lookups work (#52).
