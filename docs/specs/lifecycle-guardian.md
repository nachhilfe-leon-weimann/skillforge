# Spec: Lifecycle Guardian (self-healing for jobs & operations)

> Status: Superseded by [bot-decoupling.md](bot-decoupling.md) (2026-09); was Implemented | Arc 1 of the platform capability roadmap
> Prerequisite for Arc 3 (outbox/eventing) and Arc 4 (integration sync), which build on the same job/operation substrate.

## Capability arcs (roadmap context)

- **Arc 1 - Guardian:** Self-healing for jobs & operations. *This document.*
- **Arc 2 - Ops plane:** Read/observability layer (queue depth, funnel views, audit search).
- **Arc 3 - Eventing:** Outbox + webhooks/events outward, idempotency key on the service API.
- **Arc 4 - Integration sync:** Job/worker pattern made generic for Clockodo/sevDesk/Microsoft.

## Problem statement

SkillForge orchestrates SkillBot through a job queue (`bot.job`) and two-phase transitions (`bot.operation`).
Neither state machine can **clean up after itself** today: if the worker dies between `claim` and
`complete`/`fail`, the job stays in `CLAIMED` forever (`claim_jobs` in [`jobs.py`](../../app/services/bot/jobs.py)); if the bot crashes after
`prepare`, the operation stays in `PREPARED` forever - `EXPIRED` is only set lazily on the commit attempt
(`_load_prepared_operation` in [`transitions.py`](../../app/services/bot/transitions.py)). Every deploy, OOM, or network drop can thus silently
strand work that nobody sees. With every planned integration (arc 3/4) this risk multiplies.

## Goals

1. **No job stays silently stuck.** A job whose worker dies during `CLAIMED` is automatically made
   deliverable again (or cleanly moved to the dead letter), without human intervention.
2. **No zombie reservations.** Expired `PREPARED` operations are actively materialized to `EXPIRED`
   instead of piling up in the table.
3. **The dead letter is visible and fixable.** Permanently failed jobs (`FAILED`) are findable and can
   be requeued deliberately.
4. **First operational signal.** Every reaper run emits structured counters (reclaimed / dead-lettered / expired),
   so anomalies (e.g. the bot not committing) become visible early.
5. **The door to arc 3/4 stays open.** Terminal rows are retained and become the later event/sync substrate.

## Non-goals

- **Full ops/read plane (Arc 2).** Queue-depth dashboards, operation funnel views, audit search - separate.
  Here only structured logs as a seed.
- **Idempotency-key header on the service API (Arc 3).** Deliberately parked; belongs to the eventing backbone, not the reaper.
- **Per-`kind` leases (Arc 4).** A global lease is enough until long-running sync jobs sit next to short Discord ops.
- **Eventing / outbox / webhooks (Arc 3).** No outward push in this phase.
- **Retention / archiving / purge** of terminal rows. The sweeper *marks*, it does *not delete*. Cleanup is a
  deliberate later step.
- **Bot-side idempotency implementation.** Lives in the SkillBot repo. Forge only documents the contract (at-least-once).

## Decided defaults

| Topic | Decision | Rationale |
|---|---|---|
| **A - Lease** | **Global**, one config constant `JOB_LEASE = 5 min`. No new column - `claimed_at` is enough. Adjustable later if needed. | No migration needed; per-`kind` only in arc 4. |
| **B - Execution** | **Dedicated worker service in `compose.yml`** (same image, different entrypoint, `REAPER_INTERVAL = 30 s`). Uses the **pooler connection** (like the app). | Established pattern (one-shot `migrate` service); reclaim queries are concurrency-safe via `SKIP LOCKED` anyway. |
| **C - Idempotency** | Document the at-least-once contract **now**, park the idempotency-key header **until after arc 3**. | The reaper increases re-delivery - handler idempotency becomes mandatory, but the transport key belongs to the eventing arc. |
| **Attempt budget** | **A lease reclaim costs one attempt** (the increment on claim carries it). No separate budget. | Simple; "every delivery costs one attempt" is acceptable. |

## User stories

**Operator / on-call**
- As an operator I want stuck jobs to recover by themselves, so a nightly deploy crash doesn't lead to
  silently stranded work that I only notice days later.
- As an operator I want to list permanently failed jobs and requeue one deliberately, so a fixable error
  (e.g. a transient Discord outage) isn't lost forever.
- As an operator I want to see, after each reaper run, how many jobs were reclaimed and how many operations
  expired, so a systematic problem (the bot not committing) stands out early.

**Forge-internal workflow author**
- As the author of a Forge-internal workflow I want to trust that an enqueued job is delivered *at least once*,
  so I don't have to guard business logic against worker crashes myself.

**SkillBot (consumer)**
- As SkillBot I need to know that delivery is *at-least-once* and my handler must be idempotent, so a reclaimed
  job doesn't trigger a duplicate Discord action.

## Requirements

### Must-have (P0)

**P0-1 - Job reaper.** A periodic run finds jobs with `status = CLAIMED AND claimed_at < now() - JOB_LEASE` and
applies the existing `fail_job` reclaim transition.
- *Technique:* Reuse of the existing logic (`fail_job` in [`jobs.py`](../../app/services/bot/jobs.py)) - status -> `PENDING`,
  `available_at = now() + RETRY_BACKOFF`, `claimed_at`/`claimed_by` cleared; on exhausted attempts -> `FAILED`,
  `error = "lease expired"`.
- *Acceptance criteria:*
  - [x] Given a job is `CLAIMED` and `claimed_at` is older than `JOB_LEASE`, when the reaper runs, then the job is
        `PENDING` with `available_at` in the future and `claimed_by` cleared.
  - [x] Given a job is `CLAIMED` but within the lease window, when the reaper runs, then the job stays
        unchanged `CLAIMED`.
  - [x] Given a reclaimed job has `attempt >= max_attempts`, when the reaper picks it up, then it becomes `FAILED`
        (not `PENDING`) with `failed_at` and `last_error` set.
  - [x] `attempt` is **not additionally** incremented by the reaper (the increment happens only on `claim`).
  - [x] Two reaper instances running at the same time do not reclaim the same job twice (`SKIP LOCKED`).

**P0-2 - Operation sweeper.** A periodic run sets `PREPARED` operations with `expires_at <= now()` to `EXPIRED`.
- *Technique:* Pure materialization; the capacity slot is already passively free (the count uses `expires_at > now`).
- *Acceptance criteria:*
  - [x] Given an operation is `PREPARED` with `expires_at` in the past, when the sweeper runs, then it is
        `EXPIRED`.
  - [x] A `COMMITTED`/`FAILED`/`EXPIRED` operation is not touched by the sweeper.
  - [x] The lazy path in `commit` (`_load_prepared_operation` in [`transitions.py`](../../app/services/bot/transitions.py)) stays correct and
        does not collide with the sweeper (idempotent marking).

**P0-3 - Dedicated worker service.** A new service in `compose.yml` runs reaper + sweeper in a loop
(`REAPER_INTERVAL = 30 s`), same image as the app, its own entrypoint, uses the **pooler connection** (the same
DB URL as the app).
- *Acceptance criteria:*
  - [x] The worker starts independently in the compose stack and runs independently of app replicas.
  - [x] A crash of the worker doesn't stop the app and vice versa.
  - [x] Multiple worker replicas are safe (no double effects, thanks to `SKIP LOCKED`).

**P0-4 - Structured counters.** Every run logs structured: `jobs_reclaimed`, `jobs_dead_lettered`,
`operations_expired`, `duration_ms`. **Logs only - no alerting** in this phase.
- *Acceptance criteria:*
  - [x] Every reaper run produces exactly one structured log line with the four fields.
  - [x] A run with no hits logs zero values (no silent silence).

**P0-5 - At-least-once contract documented.** The API/job docs state: jobs are delivered at least once,
handlers must be idempotent; the reaper makes re-delivery real, not hypothetical.
- *Acceptance criteria:*
  - [x] The contract is captured in the job docs (and in the OpenAPI context, where consumers see it).

### Nice-to-have (P1)

**P1-1 - Dead-letter requeue.** An operator path (`just` command for v1) that resets a `FAILED` job to `PENDING`,
`attempt = 0`, `available_at = now()`.
- *Acceptance criteria:*
  - [x] Given a `FAILED` job, when the operator calls `requeue <job_id>`, then the job is `PENDING` with
        `attempt = 0` and immediately deliverable.
  - [x] A non-existent or non-`FAILED` job returns a clear error.

**P1-2 - Dead-letter list.** A simple read path (`just` command) that lists `FAILED` jobs with `kind`, `last_error`,
`failed_at`.
- *Acceptance criteria:*
  - [x] Lists all `FAILED` jobs with `kind`, `last_error`, `failed_at`.

### Future considerations (P2)

- **Per-`kind` lease** (Arc 4): different visibility timeouts per job type.
- **Metric export** instead of logs only (Arc 2): Prometheus endpoint / read API for queue depth & funnel.
- **Operation reclaim with plan replay** (Arc 4): re-prepare expired operations automatically, not just mark them.
- **Retention job**: archive terminal rows after N days - only sensible once arc 3 consumes them as events.

## Success metrics

**Leading (days)**
- *Stuck-job count:* number of jobs in `CLAIMED` older than `2 * JOB_LEASE`. **Target: ~0** in steady-state
  operation; measured via query / log. (Today: unbounded growth on crashes.)
- *Zombie-operation count:* `PREPARED` rows with `expires_at` older than 1 h. **Target: ~0.**
- *Recovery latency:* time from worker death to job back to `PENDING`. **Target: <= `JOB_LEASE + REAPER_INTERVAL`** (default ~5.5 min).

**Lagging (weeks)**
- *Manual queue interventions:* number of manual DB corrections to `job`/`operation`. **Target: toward 0.**
- *Dead-letter rate:* share of jobs ending `FAILED`. Stably low; a rise is an early signal for bot problems,
  not reaper problems.

## Decided (formerly open questions)

All blockers resolved - the spec is implementation-ready. Recorded decisions:

- **`JOB_LEASE = 5 min`** - adjustable later if needed.
- **A lease reclaim costs one attempt** - no separate budget.
- **`REAPER_INTERVAL = 30 s`.**
- **The worker uses the pooler connection** (like the app).
- **Requeue via `just` command** (P1-1) - no admin endpoint.
- **No alerting** - structured logs are enough in this phase.

## Timeline / phasing

No hard deadline. One cut, kept small:

1. **P0-1 + P0-2 + P0-3 + P0-4** together - the reaper isn't runnable without the worker service, and counters
   are practically free. That's the closeable self-healing core.
2. **P0-5** (contract docs) in parallel - a pure docs/OpenAPI touch.
3. **P1-1 + P1-2** as a fast follow, once the core runs in prod and the first real dead letter shows up.

**Dependency:** None external. **Blocks:** Arc 3 (outbox) and Arc 4 (integration sync) directly benefit from the
design discipline "terminal rows are retained".
