# Spec: Off-boarding transitions (tear down student & tutor workspaces)

> Status: Superseded by [bot-decoupling.md](bot-decoupling.md) (2026-09); was Implemented
> Tracking: [#47](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/47)
> Counterpart of [principals & provisioning](principals-and-provisioning.md), whose
> "(de)activation ownership" decision explicitly hands *flipping `active` off + teardown* to this arc.
> Builds on the two-phase substrate of [ADR 0003](../decisions/0003-two-phase-transitions.md).

## Problem statement

The bot lifecycle is **one-directional**. `OperationKind`
([`operation.py`](../../app/core/db/models/bot/operation.py)) covers only `tutor_activate`,
`student_activate`, `student_stash`, `student_pop` - the system can **onboard but never off-board**.

- **No way to end a student relationship.** Stash/pop only move a student channel between the tutor
  category and an archive category ([`transitions.py`](../../app/services/bot/transitions.py)); they
  never end the relationship or remove the `StudentWorkspace`. A student channel, once created, can
  only shuttle back and forth forever.
- **No way to retire a tutor.** There is no transition to tear down a `TutorWorkspace` (its category +
  command channel). A tutor who leaves leaves a permanent, capacity-holding workspace behind.
- **Capacity leaks.** `_assert_tutor_capacity` counts committed `TUTOR_CATEGORY` workspaces against the
  tutor's `student_channel_capacity`. With no teardown, a slot occupied by an ended relationship is
  never reclaimed.
- **Identity never deactivates.** The provisioning arc only ever sets `DiscordUser.active = true`; the
  matching off direction was deliberately parked for this arc.

Every wind-down is therefore an out-of-band DB edit - the opposite of the spec-first, two-phase
discipline the rest of the bot lifecycle follows.

## What already exists (reuse, do not rebuild)

- **Two-phase machinery.** `_create_operation` / `_load_prepared_operation` / `_mark_committed`,
  `OPERATION_TTL`, the `bot.operation` reservation log, and the `prepare`/`commit` HTTP surface
  ([`students.py`](../../app/api/v1/bot/students.py), [`tutors.py`](../../app/api/v1/bot/tutors.py)).
  New kinds slot straight in.
- **Capacity counting.** `_assert_tutor_capacity` already counts committed workspaces plus outstanding
  `PREPARED` inbound reservations. Teardown reuses the same count to enforce the tutor-refuse policy.
- **Error -> HTTP mapping.** `TransitionValidationError` (422) and `TransitionConflictError` (409) are
  sufficient; no new error types.
- **Provisioning upsert.** Re-onboarding after a teardown goes through the existing idempotent
  `upsert_discord_user` (`active = true`) - no special "reactivate" path needed here.

## Goals

1. **Off-board a student.** A two-phase `student_deactivate` tears down a `StudentWorkspace` and hands
   SkillBot a Discord delete plan, from either channel state (tutor category or archive).
2. **Retire a tutor.** A two-phase `tutor_deactivate` tears down a `TutorWorkspace`, **refusing** while
   any student workspace still hangs under the tutor.
3. **Reclaim capacity.** Once a student is deactivated, the freed slot is immediately available to the
   tutor again.
4. **Retain the audit trail, not the live state.** The teardown deletes the *operational* workspace
   row but never the *identity*; the `COMMITTED` operation row is the retained terminal record.
5. **Flip identity off, don't delete it.** Off-boarding sets `DiscordUser.active = false`; party/CRM
   data is never touched.
6. **Contract-first.** Endpoints land in `openapi.json`; consumers regenerate clients (ADR 0001).

## Non-goals

- **Discord-side deletion.** Forge plans the delete; SkillBot executes it. Forge never calls Discord
  (ADR 0003 holds).
- **Cascading tutor teardown.** A `tutor_deactivate` does **not** mass-delete its students; the tutor
  refuses while occupied (see decision table). Cascade can be a later enhancement.
- **Purging party / CRM data.** Person, party, contact-info rows are CRM-owned and untouched. Only the
  `active` flag flips.
- **Reaping orphaned channels.** Reconciling Discord channels that drift from Forge's view is the
  lifecycle-guardian / ops-plane arc, not this one.
- **A dedicated "reactivate" transition.** Re-onboarding reuses the provisioning upsert + the existing
  activation flow.
- **Workspace soft-delete columns.** No `deactivated_at`/status on the workspace tables (see decisions).

## Decisions

| Topic | Decision | Rationale |
|---|---|---|
| **Workspace retention** | **Hard-delete the workspace row** on commit; the `COMMITTED` operation row is the retained terminal record. | Consistent with ADR 0003 ("terminal operation rows are retained as event/sync substrate"). No schema bloat; keeps re-onboarding clean (the `(guild_id, subject)` PK is free again). The workspace is *live* state mirroring Discord, not the audit log. |
| **Channel rows** | Commit **deletes the `DiscordChannel` rows** the teardown destroys (student channel; tutor category + command channel) - the symmetric inverse of activation's `_ensure_channel`. | The channel is gone in Discord; a dangling row would misrepresent topology. Activation creates the row on commit, deactivation removes it on commit. |
| **Identity** | Off-boarding sets the subject's **`DiscordUser.active = false`**; never deletes the user or party. | Honors the provisioning spec handoff ("flipping active off + teardown is owned by #47"). Re-onboarding flips it back via the idempotent provisioning upsert. |
| **Tutor-with-students policy** | **Refuse.** `tutor_deactivate` `prepare` *and* `commit` raise `TransitionConflictError` while any student workspace - in **either** channel state - still references the tutor, or any inbound reservation is outstanding. | Conservative and explicit; avoids a surprising mass channel-deletion. Students must be off-boarded first. Re-checking on commit closes the prepare->commit race. |
| **Student state coverage** | `student_deactivate` works from **both** `tutor_category` and `archive_category`. | A relationship can end whether or not the student is currently stashed; off-boarding must not require a pop first. |
| **Capacity timing** | A `PREPARED` `student_deactivate` does **not** pre-free the slot; capacity is reclaimed only when the workspace row is deleted on **commit**. | The workspace is still live until commit; freeing early would let a concurrent activation overbook if the deactivate then expires. Deactivate kinds are *not* counted as inbound reservations. |
| **HTTP shape** | Path-based, mirroring stash/pop: `POST /students/{guild_id}/{student_discord_id}/deactivate/{prepare,{op}/commit}` and `POST /tutors/{guild_id}/{tutor_discord_id}/deactivate/{prepare,{op}/commit}`. Commit bodies are empty (no Discord results to record - the plan is a pure delete). | The subject is an existing workspace addressed by its keys, exactly like stash/pop; activation's body-based static route is for not-yet-existing subjects. |
| **Migration** | Add the two enum values with `ALTER TYPE ... ADD VALUE IF NOT EXISTS`; downgrade recreates `bot.operation_kind` without them (rename-old / create-new / re-cast column / drop-old). | Postgres can't drop an enum label in place. The recreate keeps the chain reversible for the `downgrade base` -> `upgrade head` migration roundtrip test. |

## User stories

- *As SkillBot* I want a `student_deactivate` plan when a tutoring relationship ends, so I can delete
  the channel and Forge reclaims the tutor's slot - without a manual DB edit.
- *As SkillBot* I want `tutor_deactivate` to refuse while students still hang under the tutor, so I
  never accidentally orphan student channels by retiring their tutor first.
- *As an operator* I want off-boarding to flip `active` off and keep the operation record, so a
  wind-down is auditable and the identity can be re-onboarded later without resurrecting stale state.
- *As a tutor* I want a freed slot to be immediately reusable after a student leaves, so my capacity
  reflects reality.

## Requirements

### Must-have (P0)

**P0-1 - `student_deactivate` (two-phase).**
- *prepare(`guild_id`, `student_discord_id`)*: require the student workspace exists; write a `PREPARED`
  operation with a delete plan `{action: "delete_student_channel", guild_id, channel_id}`.
- *commit(`operation_id`)*: re-load the workspace (conflict if gone), delete the `StudentWorkspace` row,
  delete its `DiscordChannel` row, set the student's `DiscordUser.active = false`, mark `COMMITTED`.
- *Acceptance criteria:*
  - [ ] Given a student workspace (in either channel state), prepare returns a `PREPARED` operation
        whose plan carries the channel to delete.
  - [ ] After commit, the `StudentWorkspace` row is gone, the operation is `COMMITTED`, and the
        student's `DiscordUser.active` is `false`.
  - [ ] Prepare on a non-existent workspace raises `TransitionValidationError` (422).
  - [ ] A double commit / expired operation is rejected (`OperationNotPendingError`, 409) - reusing the
        existing `_load_prepared_operation` guard.

**P0-2 - `tutor_deactivate` (two-phase, refuse-while-occupied).**
- *prepare(`guild_id`, `tutor_discord_id`)*: lock + require the tutor workspace; **refuse** (conflict)
  if any student workspace references the tutor or any inbound reservation is outstanding; else write a
  `PREPARED` operation with `{action: "delete_tutor_workspace", guild_id, category_channel_id,
  command_channel_id}`.
- *commit(`operation_id`)*: re-load the workspace (conflict if gone), **re-check** no students remain,
  delete the `TutorWorkspace` row, delete the command + category `DiscordChannel` rows, set the tutor's
  `DiscordUser.active = false`, mark `COMMITTED`.
- *Acceptance criteria:*
  - [ ] Given a tutor workspace with **no** students, prepare+commit removes the workspace and its two
        channels, and sets the tutor's `active = false`.
  - [ ] Given **any** student workspace under the tutor (tutor category *or* archive), both prepare and
        commit raise `TransitionConflictError` (409).
  - [ ] An outstanding `PREPARED` inbound reservation (student_activate/pop) for the tutor blocks
        prepare.

**P0-3 - Capacity is freed.**
- *Acceptance criteria:*
  - [ ] After a `student_deactivate` commit, a tutor at full capacity can prepare a new
        `student_activate` (the freed slot is counted as available).

**P0-4 - Contract & docs.**
- *Acceptance criteria:*
  - [ ] The four endpoints are in `openapi.json`; `just openapi-check` is green.
  - [ ] `OperationKind` (model, `DATABASE_SCHEMA.md`, `ARCHITECTURE.md`) lists the two new kinds.

### Nice-to-have (P1) - fast follow

- **P1-1 - Cascading tutor teardown.** An opt-in `tutor_deactivate` variant that off-boards its
  students in one plan, for the "tutor left, clean everything" case.
- **P1-2 - Bulk/relationship views.** A read path listing the students under a tutor, to drive an
  operator off-boarding workflow.

## Timeline / phasing

One cut: **P0-1 + P0-2 + P0-3 + P0-4** together - the model enum value, the migration, the four service
functions, the four endpoints, the OpenAPI/doc sync, and tests. The teardown is small and symmetric to
the existing activation flow; splitting it would ship a half-usable off-boarding.

**Depends on:** the provisioning arc ([#4](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/4),
shipped) for the `active`-flag write surface used to re-onboard. **Relates to:** the lifecycle guardian
(expired teardown reservations are swept by the existing operation sweeper - no new reaper logic).
