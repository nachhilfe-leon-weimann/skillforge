# ADR 0009 - The bot owns its Discord workflows; SkillForge keeps central data and pushes nothing

Status: Accepted, 2026-09. Supersedes [0003](0003-two-phase-transitions.md) and
[0004](0004-forge-first-job-queue.md); amends [0005](0005-multi-schema-db.md),
[0007](0007-crm-system-of-record.md) and [0008](0008-user-authentication-and-reach.md).

## Context

The [project sketch](../PROJECT.md) says SkillForge is the hub for central data, identity,
permissions and domain rules - not the backend of any frontend. Every service owns its data, and
SkillForge pushes nothing.

About a third of SkillForge is the opposite: the backend of SkillBot.

- The `bot` schema (14 tables, 9 enum types) holds Discord topology, workspaces, command
  environments, a member cache with its own `role`, and a grant engine beside the scopes.
- 36 operations under `/api/v1/bot` (`bot:read`, `bot:write`) plan Discord changes in two phases
  ([ADR 0003](0003-two-phase-transitions.md)) and hand out jobs ([ADR 0004](0004-forge-first-job-queue.md)).
- A reaper worker and a dead-letter CLI keep jobs and operations alive.

None of it is used. The bot never adopted prepare/commit, nothing enqueues a job, several tables have
no writer, and the flows cannot run end to end. Only three things in it are central:

- **A Discord user is a person only through an active link** (`ext.discord_account`). Once the token
  exchange of ADR 0008 exists, that link is a login credential - and today any `bot:write` client can
  point any Discord user at any party, silently.
- **Only a student's own tutor may act on the student** - checked today at student activation only.
- **A tutor does not see who pays** - today a filter in the bot's profile view.

ADR 0007 describes the CRM's consumers through mechanisms that now go away: the `bot` schema,
reconciliation by two-phase transitions, the bot writing `ext.discord_account`, and "no eventing yet".

## Decision

**The bot runs its Discord workflows itself and keeps their state in its own database. SkillForge
keeps central data, identity, permissions and domain rules; frontends pull what changed.**

1. **SkillForge gives up** the `bot` schema and its models, `/api/v1/bot`, jobs, operations and the
   lifecycle guardian, permission groups and grants, the scopes `bot:read` and `bot:write`,
   `bootstrap-skillbot` and the dead-letter CLI.
2. **SkillForge keeps** the CRM, user accounts, Discord links, scopes and reach, and the token
   exchange. The worker plane stays as SkillForge's housekeeping (expired sessions and one-time
   tokens); `/health` and the deploy gate are unchanged.
3. **The bot's data lives in the bot's database** on the same Postgres server, with its own
   migrations. No cross-database reads, no foreign key into SkillForge, no `CONNECT` on SkillForge's
   database; `party_id` is a plain key taken from the API. What the bot holds can be rebuilt from
   SkillForge and never flows back.
4. **The CRM is the trigger.** The tutor role and `TUTOR_OF` are the intended state. The bot pulls them
   and brings Discord in line - onboarding and off-boarding alike. Off-boarding never touches CRM data
   and never blocks a CRM write. A Discord command that changes central data is one more interface for
   the same CRM write, sent with the person's token; SkillForge decides.
5. **Consumers pull.** A feed item carries `updated_at` (the start of the writing transaction);
   consumers ask with `updated_since` minus an overlap and run a periodic full comparison, which also
   finds deletions. The feeds are the party list and the Discord link feed; one contract, kept in
   [`ARCHITECTURE.md`](../ARCHITECTURE.md) under "Change signals". Pulling is the decision, not a
   stopgap.
6. **Commands run as the person.** The bot exchanges the Discord user who sent a command for a token
   of that person (`amr: ["discord"]`) and calls SkillForge with it. Its application token only reads,
   for the sync. A token obtained without a password carries only the scopes in `VOUCHED_SCOPES`, so
   account and admin actions keep demanding a password login.
7. **Discord links are identity, written only through auth.** An admin links (`auth:users:manage`,
   audited, person parties only); later the person redeems a one-time link code in the bot. The bot
   never writes a link as itself, and its client never holds `auth:users:manage`. The link stays on
   the party; only an active link counts, for the exchange and for the CRM's delete guard.
8. **Crash safety of Discord side effects belongs to the bot.** Its reservations, capacity and
   teardown rules become an internal saga with its own locks. SkillForge offers no prepare/commit and
   no job queue.
9. **The CRM depends on no other domain.** Its services and routes import only `app.core`, the shared
   API vocabulary and themselves.

**Supersedes ADR 0003 and ADR 0004.** Their status line changes; their text stays as history.

**Amends ADR 0005.** The schema list loses `bot`: `core`, `geo`, `ext`, `auth`, `system`.
`migrations/env.py` still creates the schemas of today's models; a schema that only history knows is
created by the revision that first needs it (`0001_baseline` for `bot`) and dropped by the revision
that retires it.

**Amends ADR 0007.** Its core holds - the CRM is the system of record and never asks a consumer for
permission. These sentences no longer hold:

| ADR 0007 says                                                                                  | Now                                                                                                   |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| "The `bot` schema mirrors what _is_ true in Discord plus the workflows that change it."        | The bot's own database does.                                                                          |
| "`app/services/bot` may import from `app/services/crm`; the reverse is forbidden."             | There is no bot domain in SkillForge; the CRM imports nothing but the core and itself (point 9).      |
| "Divergence is the consumer's job ... through its existing two-phase transitions (ADR 0003)."  | Still the consumer's job, now through the bot's reconcile loop over the pulled state.                 |
| "the bot's provisioning flow keeps writing `ext.discord_account`"                              | `ext.discord_account` is identity, written only through auth (point 7). Other `ext` links unchanged.  |
| "No eventing yet ... Until then the party aggregate's `updated_at` is the only change signal." | Pulling is the decision (point 5).                                                                    |
| "never refused ... because of Discord state (an existing workspace, an active Discord user)"   | Still holds. The delete guard reads whether a Discord link is _active_ - identity, not Discord state. |

**Amends ADR 0008.** "For people linked to Discord the CRM's existing `party_in_use` guard refuses the
delete anyway" becomes: only an _active_ link refuses the delete; unlinked rows go with the party. And
"`amr` keeps password-only actions out of its reach" is made concrete by `VOUCHED_SCOPES` (point 6).

Deliberately _not_:

- **No webhooks or outbox - now.** Pushing stays an option (sketch, principle 3) for the day a poll
  window no longer fits one page, a full comparison takes more than about a minute, or a frontend
  needs less latency than one poll interval.
- **No cross-database reads** in either direction: no `CONNECT`, no foreign data wrapper, no shared
  tables or views.
- **No second rights system.** No bot-facing scopes that no route requires, no permission groups, and
  no Discord role grants a right - a role name least of all. An explicit deny for one person has no
  successor: disable the account or remove the role.
- **No generic job queue kept "for later".** A future integration sync (sevDesk, Clockodo, Microsoft)
  gets its own decision.
- **No migration of bot rows.** The bot's database starts empty.
- **No tutor reach here.** "Only the student's own tutor" becomes a SkillForge rule in its own arc,
  together with the restricted views of relations and subjects. Until then no bot command acts on a
  student through SkillForge, and the bot does not rebuild the rule from roles and 404 probes.

## Consequences

- SkillForge shrinks by about a third - code, tests and contract. The published `skillforge-client`
  loses `api.bot` and two scopes; nothing uses them.
- **Eventual consistency.** Discord lags the CRM by one poll interval plus the overlap; skips and
  deletions are repaired by the next full comparison. An exchanged token outlives an unlink, a
  disable or a role change by up to 15 minutes (ADR 0008, decision L).
- **The bot's secret speaks for every linked person,** up to its delegated ceiling and
  `VOUCHED_SCOPES`. Every link change is therefore audited, and link writes need an admin.
- **The bot needs its own infrastructure:** a database, migrations, a saga for Discord side effects.
  The rules it inherits are written into its docs before the code leaves SkillForge (tag `v0.5.0`
  keeps the code).
- Removing a tutor or student role now also removes the `TUTOR_OF` it anchored, so every `TUTOR_OF` is
  a valid pair for every consumer.

Implementation: [`bot-decoupling.md`](../specs/bot-decoupling.md).
