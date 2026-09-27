# ADR 0004 - Forge-first job queue with at-least-once delivery

Status: Superseded by [0009](0009-bot-owns-its-discord-workflows.md) (2026-09); accepted 2026-05

## Context

Forge needs to hand SkillBot work (e.g. trigger Discord actions) without calling the bot directly -
the bot isn't necessarily reachable, and Forge should remain the driving side. This needs a
mechanism that delivers work reliably, survives worker crashes, and avoids extra infrastructure
(a broker like Redis/RabbitMQ).

## Decision

A **Forge-first job queue in Postgres** (`bot.job`, `app/services/bot/jobs.py`):

- Forge `enqueue`s jobs (`kind` + `payload` JSONB); SkillBot **polls and claims** them.
- Claiming is atomic via **`SELECT ... FOR UPDATE SKIP LOCKED`** - multiple workers never grab the
  same job. Index `ix_job_claimable` covers the claim query (pending, ordered by `available_at`).
- Lifecycle: `PENDING -> CLAIMED -> COMPLETED | FAILED`. Failures requeue with backoff
  (`RETRY_BACKOFF = 60 s`) until `max_attempts` is exhausted -> `FAILED`.
- **At-least-once**: delivery is guaranteed at least once; SkillBot's handlers **must be
  idempotent**, since a reclaimed job can be redelivered.

Deliberately *no* external message broker - Postgres is enough for the current load and keeps the
stack small. No exactly-once.

## Consequences

- No extra infrastructure; queue, workspaces, and operations share one transaction/DB.
- Consumers carry the idempotency obligation - the contract is documented explicitly.
- A worker that dies between `claim` and `complete`/`fail` leaves the job stuck in `CLAIMED`. The
  lifecycle-guardian reaper (`app/workers/reaper.py`) now reclaims such jobs once their lease
  (`JOB_LEASE`) expires - requeuing with backoff, or dead-lettering to `FAILED` once attempts are
  exhausted. This ADR justifies the queue; the [lifecycle guardian spec](../specs/lifecycle-guardian.md)
  made it self-healing.
