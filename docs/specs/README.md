# Specs

A spec is the work order for one arc: why it exists, what is decided, and slices an agent can implement without
asking back. It is also the plan - progress shows only in its checkboxes and its status block. The reference specs
are [`user-authentication.md`](user-authentication.md), [`crm-api.md`](crm-api.md),
[`api-conventions.md`](api-conventions.md) and [`release-flow.md`](release-flow.md).

## Shape

```text
# Spec: <Arc> (<what it covers, in a few words>)

> Status: Draft | Accepted | In progress - <slices done> | Implemented (<date>) | <domain arc>
> **Amended YYYY-MM-DD:** <what changed and why, one sentence>.
> Builds on <ADRs, specs>. Written to be executed by coding agents: every requirement names its symbols, files
> and checkable criteria.

## Problem statement
## Goals
## Non-goals
## Decided defaults          | Topic | Decision | Rationale |, one lettered row per decision (A, B, ...)
## Verified behavior         | Question | Result |, what was probed and how
## <Design sections>         target shape, route map, error catalog - what a slice's Technique points to
## Success metrics
## Requirements
### Must-have (P0)           standing criteria first, then P0-1, P0-2, ...
### Nice-to-have (P1)
### Future considerations (P2)
## Decided (formerly open questions)
## Open questions
## Timeline / phasing        waves, stack order, shared files
## Rules for implementing agents
```

A slice:

```markdown
**P0-3 - <Name>.**
- _Technique:_ the symbols, files and steps - `resolve_token_scopes` in `scopes.py`, one Alembic revision.
- _Tests that change:_ the existing tests this slice may edit, by name; every other test stays as it is.
- _Plan:_ [task plan](../plans/<spec>-<phase>.md), only when a phase needs one.
- _Acceptance criteria:_
  - [ ] a statement a reviewer can check, with the command or the test that shows it.
```

## What makes a spec work for agents

- **Decisions are closed.** The decision table gives each choice its rationale, so an agent applies it instead of
  reopening it; *Non-goals* mark what is left out on purpose, not by oversight.
- **Facts are separated from assumptions.** *Verified behavior* records what was probed (framework behavior,
  GitHub, a live API) and how; anything not verified says so.
- **Every requirement names its symbols and files** and has criteria a reviewer can check, ticked in the PR that
  fulfils them.
- **Edits to tests are declared.** *Tests that change* lists the tests a slice may touch, so a changed assertion
  elsewhere is a finding, not a fix.
- **Standing criteria** hold for every slice (an import boundary, no secret in a log, `just check-all` green) and
  are ticked with the last one.
- **The order is explicit.** The timeline names waves, the stack order, the files two slices share and how to
  resolve them - `openapi.json` is regenerated, never merged by hand.
- **Closed catalogs stop the agent.** If a requirement seems to need a new error class, scope or decision, the
  rules say stop and report.

## Rules

- A spec's *Rules for implementing agents* apply in addition to [`AGENTS.md`](../../AGENTS.md); if they contradict
  it, ask.
- Progress shows only in the checkboxes and the status block: a slice's last PR ticks its boxes, and a change of
  course gets an **Amended** line with its date. Moved code gets a status line saying what moved where.
- No tracking issues. Existing `> Tracking:` lines stay as history.
- A task plan for a phase lives versioned in `docs/plans/`, linked from the slice as `_Plan:_`; it replaces tool
  defaults such as `docs/superpowers/`. Briefs and handoffs never become files ([`agent-work.md`](../agent-work.md)).
- A spec that is also the decision record says so in its status block; otherwise a lasting decision gets an ADR in
  [`docs/decisions/`](../decisions/).
