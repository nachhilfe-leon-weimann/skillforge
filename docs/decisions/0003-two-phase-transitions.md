# ADR 0003 - Two-phase `prepare`/`commit` operations for Discord state

Status: Superseded by [0009](0009-bot-owns-its-discord-workflows.md) (2026-09); accepted 2026-05

## Context

Discord state changes (create a tutor workspace, activate/stash/pop a student channel) touch two
systems: Forge's database and the Discord API. Forge **never touches the Discord API itself** -
that's SkillBot's job. Still, Forge must be the source of truth for the desired state and enforce
capacities (e.g. max student channels per tutor, archive slots) in a concurrency-safe way. A naive
single step would either pull Discord calls into the DB transaction or leave inconsistent state on
a bot crash.

## Decision

State transitions run in **two phases** via the `bot.operation` table
(`app/services/bot/transitions.py`):

1. **`prepare`** - validates preconditions, locks the affected rows (tutor workspace or archive
   categories) with `FOR UPDATE`, reserves capacity, and writes a `PREPARED` operation with a
   `plan` (JSONB) and `expires_at` (TTL **10 min**, `OPERATION_TTL`). Forge hands SkillBot an
   executable plan in return.
2. **`commit`** - SkillBot reports the confirmed Discord results; Forge persists the final
   workspace state and marks the operation `COMMITTED`.

The `operation` table deliberately has **no foreign keys** - it's a transient
operation/reservation log; `prepare` validates referenced entities explicitly. Capacity is counted
from committed workspaces **plus** outstanding `PREPARED` reservations (`expires_at > now()`), so
parallel prepares can't overbook.

## Consequences

- Forge stays decoupled from the Discord API; a bot crash between `prepare` and `commit` only
  leaves an expiring reservation, not a half-changed state.
- Concurrency is serialized via DB row locks - no app-level mutexes.
- Expired `PREPARED` operations need cleanup, which now happens two ways: lazily on the next
  `commit` attempt, and actively via the lifecycle-guardian reaper worker (`app/workers/reaper.py`),
  which sweeps expired reservations to `EXPIRED` on `REAPER_INTERVAL` - see the
  [lifecycle guardian spec](../specs/lifecycle-guardian.md).
- Terminal operation rows are retained and serve as event/sync substrate later (arc 3/4).
