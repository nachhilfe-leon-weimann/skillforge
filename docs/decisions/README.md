# Architecture Decision Records

Short, numbered notes that capture the **why** behind structural decisions - so that "why did we
do it this way?" doesn't get lost in commit messages and memory. They complement
[`ARCHITECTURE.md`](../ARCHITECTURE.md) (*what exists now*) and [`specs/`](../specs/)
(*what's coming*).

## Records

| # | Decision | Status |
|---|---|---|
| [0001](0001-openapi-as-contract.md) | `openapi.json` as the versioned API contract | Accepted |
| [0002](0002-pooled-vs-migration-url.md) | Separate DB URLs for the app (pooled) and migrations (direct) | Accepted |
| [0003](0003-two-phase-transitions.md) | Two-phase `prepare`/`commit` operations for Discord state | Superseded by 0009 |
| [0004](0004-forge-first-job-queue.md) | Forge-first job queue with at-least-once delivery | Superseded by 0009 |
| [0005](0005-multi-schema-db.md) | Multi-schema DB + explicit-DDL Alembic baseline | Accepted, amended by 0009 |
| [0006](0006-error-envelope.md) | One error envelope for every non-2xx response | Accepted |
| [0007](0007-crm-system-of-record.md) | CRM is the system of record; the bot is a consumer | Accepted, amended by 0009 |
| [0008](0008-user-authentication-and-reach.md) | SkillForge authenticates people itself; client grants have a mode; scopes carry a reach qualifier | Accepted, amended by 0009 |
| [0009](0009-bot-owns-its-discord-workflows.md) | The bot owns its Discord workflows; SkillForge keeps central data and pushes nothing | Accepted |

## When to write an ADR?

When a decision is hard to reverse, touches several components, or you'd ask "why did we do this?"
in six months. Trivia doesn't need an ADR.

## Format

One record per file, `NNNN-short-slug.md`, numbered sequentially. Keep it short:

```markdown
# ADR NNNN - Title

Status: Accepted, YYYY-MM

## Context
Which problem / constraint led to the decision?

## Decision
What was decided (and deliberately *not*)?

## Consequences
What gets better / what price is paid?
```

Status values: `Proposed`, `Accepted`, `Amended by NNNN`, `Superseded by NNNN`, `Deprecated`. An ADR is
not rewritten when it becomes obsolete - instead a new record supersedes or amends it, and only the older
record's status line changes.
