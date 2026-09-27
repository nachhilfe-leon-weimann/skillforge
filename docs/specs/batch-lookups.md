# Spec: Batch principal & context lookups

> Status: Superseded by [bot-decoupling.md](bot-decoupling.md) (2026-09); was Implemented
> Tracking: [#52](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/52)
> Performance slice — collapses N single-id round-trips into one batched call.

## Problem statement

The bot resolves principals and contexts one Discord id at a time (`GET /runtime/principals/{id}`,
`GET /runtime/tutors/{guild}/{id}`, `GET /runtime/students/{guild}/{id}`). On startup or a large
guild sync this is N HTTP round-trips, and each principal resolution internally fans out into **four**
DB queries — so N members cost ~4N sequential queries behind N requests. A batch lookup collapses the
round-trips to one and the DB access to a bounded, id-set query. Lower priority: a latency
optimisation, not a correctness or functional gap.

## Goals

1. **Batch principals.** Resolve many discord ids in a single request.
2. **Batch contexts.** Resolve many tutor / student contexts within one guild in a single request.
3. **Bounded input.** A hard cap on batch size, so a request can never be unbounded.
4. **Batched DB access.** Each resolver issues id-set (`WHERE ... IN`) queries, so a batch of N costs
   a fixed small number of queries, not O(N).
5. **No new trust surface.** Reuse the existing `BotRead` scope; no new scope, error type, or migration.

## Acceptance criteria (issue #52)

- [x] Batch endpoint accepting multiple discord ids, returning the resolved principals (plus tutor &
  student contexts — the title's "context lookups").
- [x] Bounded batch size.
- [x] Read scope (`BotRead`) enforced.

## API

All endpoints require **`BotRead`**, live under `/api/v1/bot/runtime`, and are **`POST`** — a bounded id
list belongs in a request body (avoids URL-length limits and gives one place to enforce the cap). The
request body is shared across all three:

```json
{ "discord_ids": [111, 222, 333] }
```

`discord_ids`: **1–100 items, each ≥ 0**. Out of range (empty or over the cap) → `422` via Pydantic
validation; no custom error. Duplicate ids are de-duplicated.

The response is a **partial-result envelope** — a batch never 404s; unresolved ids are reported
explicitly, never silently dropped:

```json
{ "found": [ /* Resource, ... */ ], "missing": [ 333 ] }
```

`found` is ordered by first occurrence in the request; `missing` lists the requested ids
(de-duplicated, in request order) that did not resolve. Every `found` item already carries its
`discord_id`, so a caller can correlate without relying on order.

| Method & path | Body | Response |
|---|---|---|
| `POST /runtime/principals/batch` | `{ discord_ids }` | `{ found: BotPrincipal[], missing: int[] }` |
| `POST /runtime/tutors/{guild_id}/batch` | `{ discord_ids }` | `{ found: TutorContext[], missing: int[] }` |
| `POST /runtime/students/{guild_id}/batch` | `{ discord_ids }` | `{ found: StudentContext[], missing: int[] }` |

Item schemas (`BotPrincipal`, `TutorContext`, `StudentContext`) are unchanged and reused. For contexts
the guild is a path param (mirrors the single-id routes); an id is "missing" when no workspace exists
for `(guild_id, id)` in that guild.

Routing: the batch routes are `POST`, the single-id `{discord_id}` routes are `GET` — no path-capture
conflict (different methods; `batch` is a static segment).

## Service layer (batched, `WHERE ... IN`)

The resolvers become batch-native and the existing single-id functions **delegate** to them — one
resolution path, no semantic drift. The existing single-id DB tests act as the regression guard.

- **`principals.py`**
  - `get_principal_views(session, discord_ids) -> list[PrincipalView]` — found views only. Four id-set
    queries regardless of N:
    1. `select(DiscordUser).where(DiscordUser.discord_id.in_(ids))` → found users.
    2. group keys: the existing join filtered by `DiscordUserPermissionGroup.discord_id.in_(ids)`,
       regrouped into `dict[int, list[str]]` (order by key preserved).
    3. permission grants: **one** query over `USER` subjects (`subject_key.in_(str-ids)`) ∪ `GROUP`
       subjects (`subject_key.in_(all active group keys)`); winners resolved **per principal** in
       Python from that principal's own subject set.
    4. parties: `load_parties_for_discord_ids(session, ids) -> dict[int, Party]` — the existing
       eager-load graph over `DiscordAccount.discord_id.in_(ids)`, keyed back per id.
  - `get_principal_view(session, discord_id)` delegates: resolve `[discord_id]`; raise
    `PrincipalNotFoundError` if empty; else return the single view.
  - Winner resolution is extracted into a pure `_resolve_grant_winners(grants) -> list[str]` shared by
    single and batch (identical priority / DENY-wins-ties semantics — the one piece most at risk of
    drift if duplicated).
- **`contexts.py`**
  - `get_tutor_context_views(session, *, guild_id, tutor_discord_ids) -> list[TutorContextView]`:
    `select(TutorWorkspace).where(guild_id ==, tutor_discord_id.in_(ids))`, then a single batched
    `get_principal_views` for the workspace ids.
  - `get_student_context_views(...)` analogous, carrying each `party_id`.
  - Single-id `get_tutor_context_view` / `get_student_context_view` delegate to the batch (raise the
    matching `*NotFoundError` when empty).

Result: a batch of N principals ≈ 4 queries; a batch of N contexts ≈ 5 queries (1 workspace + 4
principal) — independent of N.

## Schemas (`schemas.py`)

- `DiscordIdBatchRequest`: `discord_ids: list[int]` with `Field(min_length=1, max_length=100)` and each
  item `ge=0`. Shared by all three endpoints (the guild is a path param, not part of the body).
- `PrincipalBatch { found: list[BotPrincipal]; missing: list[int] }`, plus `TutorContextBatch` and
  `StudentContextBatch` — typed found/missing envelopes (no opaque id-keyed map, consistent with the
  read-plane decision against open maps so consumer codegen gets named fields).

## Decisions

| Topic | Decision | Rationale |
|---|---|---|
| Method | `POST` with a body | A bounded id list belongs in a body; avoids URL-length limits; single enforcement point for the cap. `GET`-with-body is non-idiomatic. |
| Depth | Batch to the DB (`WHERE ... IN`) | The issue's cost is round-trips; batching the DB too turns a large sync into a handful of queries instead of ~4N. |
| Consolidation | Single-id resolvers delegate to the batch primitive | Single source of truth; the batch is the primitive, single is the `[id]` case. Existing single-id tests guard against drift. |
| Response | `{ found, missing }` typed envelope | Partial results are the point; explicit `missing` over silent gaps; typed lists over an opaque id-keyed map (matches the read-plane stance). |
| Cap | 100 items, `min_length=1` | Mirrors the existing `le=100` pagination cap and `REAP_BATCH_LIMIT=100`; over/empty → 422. Clients chunk larger syncs. |
| Order | `found` and `missing` in request order (de-duplicated) | Deterministic, easy to diff; items also self-identify by `discord_id`. |
| Scope / errors / migration | Reuse `BotRead`; no new error type; no migration | Purely additive read surface over existing tables/indexes. |

## Non-goals

- **Cursor pagination / paging the batch response.** The cap bounds it; the caller chunks larger syncs.
- **Cross-guild context batching.** One guild per request, mirroring the single-id routes.
- **Changing the item schemas.** `BotPrincipal` / `TutorContext` / `StudentContext` are reused as-is.
- **A shared/global batch-size setting.** Inline `max_length=100`, per the repo's inline-limit convention.
- **Writes of any kind.** Purely additive read surface.

## Testing

- **API tests** (`tests/api/test_bot_runtime_endpoint.py`, service layer monkeypatched — no DB):
  found/missing shape & ordering, de-duplication, over-limit and empty → 422, `BotRead` enforced
  (403 on wrong scope, 401 unauthenticated) — for all three endpoints.
- **DB service tests** (`@pytest.mark.db`, `tests/db/test_bot_service.py`): multi-id resolution
  including groups / permission winners (DENY-precedence at equal priority) / profile; unknown ids
  absent from the result; single-vs-batch parity for a rich principal; tutor/student batch with some
  ids lacking a workspace; `party_id` on students. Reuse the existing `_seed_rich_principal` helpers.

## Contract

Regenerate `openapi.json` with `just openapi` and commit (ADR 0001); CI enforces no drift.
