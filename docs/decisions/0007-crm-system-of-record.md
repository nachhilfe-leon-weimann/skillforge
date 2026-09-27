# ADR 0007 - CRM is the system of record; the bot is a consumer

Status: Accepted, 2026-09; amended by [0009](0009-bot-owns-its-discord-workflows.md) (2026-09)

## Context

Two parts of Forge describe overlapping facts about the same people:

- The **`core` schema** holds the business view: parties, the `student` / `tutor` roles, contact
  data, and `party_relation` (`PARENT_OF`, `TUTOR_OF`, `PAYS_FOR`).
- The **`bot` schema** holds the Discord view: `DiscordUser.role`, tutor and student workspaces, and
  the tutor a student was activated under (`tutor_discord_id` on `prepare_student_activation` in
  [`transitions.py`](../../app/services/bot/transitions.py)).

So "who tutors whom" and "who is a student" each exist twice, and nothing says which side wins when
they disagree. The question became concrete with the CRM API
([`crm-api.md`](../specs/crm-api.md)): should `TUTOR_OF` be writable there at all, and may a CRM write
be refused because of Discord state?

What already points one way:

- Nothing in the codebase creates parties; the provisioning spec
  ([`principals-and-provisioning.md`](../specs/principals-and-provisioning.md)) states that the bot links
  to an _existing_ party and never creates one.
- `DELEGATION_RELATION_TYPES` in [`authz.py`](../../app/services/bot/authz.py) already treats
  `PartyRelation` as the truth for delegation.
- There is no foreign key from the `bot` schema into `core`; the bot reads `core` through
  [`profile.py`](../../app/services/bot/profile.py).

## Decision

**The CRM is the system of record. It holds the intended state of the business; the bot is one
consumer of it, albeit a special one.**

- **Ownership.** Parties, persons, companies, roles, contact infos, subjects and party relations are
  created and changed only through the CRM services (`app/services/crm`). The bot never writes them.
- **Intended vs. observed.** `core` says what _should_ be true (this person is a student, this tutor
  teaches them). The `bot` schema mirrors what _is_ true in Discord plus the workflows that change it.
  `DiscordUser.role` and workspace assignments are observations, not decisions.
- **One-way dependency.** `app/services/bot` may import from `app/services/crm`; the reverse is
  forbidden. No foreign key from `bot` into `core` is added.
- **The CRM never asks the bot for permission.** A CRM write is validated against CRM rules only. It
  is never refused, delayed, or altered because of Discord state (an existing workspace, an active
  Discord user).
- **Divergence is the consumer's job.** When the intended state moves away from Discord reality (a
  student role is removed while a workspace exists), reconciling is bot work, expressed through its
  existing two-phase transitions ([ADR 0003](0003-two-phase-transitions.md)).

Identity links in the `ext` schema stay with the integration that owns the external identity: the
bot's provisioning flow keeps writing `ext.discord_account`. They link a party, they are not CRM facts.

Deliberately _not_:

- **No synchronous cross-checks** between the domains in either direction: no CRM write asks the bot,
  and no bot write is a condition for a CRM write. The bot validating _its own_ transition against the
  intended state (a student activation against `TUTOR_OF`) is not such a check - it is a consumer
  reading the CRM.
- **No write-back** from the bot into `core`, not even "to keep things in sync".
- **No eventing yet.** How consumers learn about CRM changes (outbox, jobs per
  [ADR 0004](0004-forge-first-job-queue.md)) is a later arc. Until then the party aggregate's
  `updated_at` is the only change signal.

## Consequences

- CRM writes stay simple, fast and testable without any Discord fixture; `TUTOR_OF` is an ordinary
  writable relation.
- The two views can disagree for a while. That is accepted and visible rather than prevented:
  reconciliation needs the bot to read the CRM, not the CRM to know the bot.
- Follow-up work in the bot domain, none of it part of the CRM API - done with P1-2 of
  [`crm-api.md`](../specs/crm-api.md):
  - `prepare_student_activation` took the tutor from the request and checked bot state only; it now
    validates the pair against `TUTOR_OF` (`_require_tutor_of`).
  - The party graph loaded in `profile.py` duplicated the CRM's loader; it now builds on `PARTY_GRAPH`.
  - `Party.discord_account` was a scalar relationship although several accounts per party are
    allowed; it is the collection `Party.discord_accounts` now.
- The future customer portal is a second consumer under the same rule: it reads and writes through
  the CRM API and gets no private path into `core`.
