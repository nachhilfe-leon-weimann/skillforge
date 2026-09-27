# Spec: Bot decoupling (the bot owns its Discord workflows)

> Status: In progress | Arc `bot`
> Tracking: [#166](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/166)
> Builds on the [project sketch](../PROJECT.md), [ADR 0009](../decisions/0009-bot-owns-its-discord-workflows.md),
> [ADR 0008](../decisions/0008-user-authentication-and-reach.md) with its spec's
> [Designed for the bot arc](user-authentication.md#designed-for-the-bot-arc), [`crm-api.md`](crm-api.md) and
> [`api-conventions.md`](api-conventions.md).
> Written to be executed by coding agents: every requirement names its symbols, files and checkable criteria.

## Problem statement

The project sketch makes SkillForge the hub for central data, identity, permissions and domain rules - not the
backend of any frontend. A third of SkillForge is still SkillBot's backend: the `bot` schema (14 tables, 9 enum
types), 36 operations under `/api/v1/bot` guarded by `bot:read` / `bot:write`, two-phase transitions
([ADR 0003](../decisions/0003-two-phase-transitions.md)), a job queue
([ADR 0004](../decisions/0004-forge-first-job-queue.md)), a grant engine beside the scopes, the reaper and a
dead-letter CLI - about 4,500 lines of code and 6,000 lines of tests.

None of it runs. SkillBot calls no SkillForge route, nothing enqueues a job, the guild, grant and role-binding
tables have no writer, and the flows cannot run end to end. What the bot really needs from SkillForge does not
exist:

- **It cannot act for a person.** The token exchange of ADR 0008 is designed, not built.
- **Discord links have no owner.** `ext.discord_account` is written only by bot routes: any `bot:write` client can
  point any Discord user at any party, unaudited - and once the exchange exists, a link is a login credential.
- **It cannot pull reliably.** The party list has no `updated_at`, and no written contract says how to pull.

Nothing of the bot is used in production, so this arc removes and rebuilds freely.

## Goals

1. **SkillForge holds nothing of the bot's:** no `bot` schema, no `/api/v1/bot`, no `bot:*` scope, no jobs, no
   operations, no grant engine.
2. **Discord links are identity:** one writer in auth, written by admins (later by the person with a one-time
   code), audited, readable as a feed.
3. **The bot acts as the person:** a token exchange gives the bot a token for the person who sent a command, under
   the same rules as the portal.
4. **Frontends can pull:** every feed item carries `updated_at`, and one written contract says how to pull.
5. **Nothing central is lost:** the rules the portal needs too stay in or move into SkillForge; the rules only the
   bot needs are written into skillbot's docs before the code leaves.
6. **The worker lives on as housekeeping** of SkillForge's own data ([#159](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/159)).

## Non-goals

- **Tutor reach** ("only the student's own tutor may act on the student"), rules per reach basis and scope, and the
  restricted views of relations and subjects (#160, #161). They form their own reach arc; the portal needs them as
  much as the bot.
- **The bot's rebuild** - its database, pull loop, saga and workflows. That is skillbot's arc; this spec states only
  the contract and the handover ([Handover to skillbot](#handover-to-skillbot)).
- **Discord OAuth in the portal** and self-service link codes (`POST /auth/me/discord-link-code`).
- **Accounts created by tutors.**
- **Webhooks, an outbox, tombstones, a relation feed, keyset paging.**
- **Exposing `ext.discord_account.is_primary`.**
- **Moving `bot` rows into the bot's database** - it starts empty.
- **A generic job queue** for later integrations.

## Decisions

| Topic                                | Decision                                                                                                                                                                                                                                       | Rationale                                                                                                                                                                  |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A - Scope of the arc**             | Remove the bot domain and build what the bot needs from SkillForge: Discord links, pull signals, the token exchange. Tutor reach is its own arc with #160 and #161.                                                                            | Tutor reach is a reach rule the portal needs too; it needs rules per basis and per scope, not a bot deadline.                                                              |
| **B - Order**                        | Remove first: links move to auth, the worker becomes housekeeping, the bot API goes, the schema and scopes go; then pull signals, the exchange and the link code.                                                                              | Nothing is live. New code is built on a third less code, never beside `bot:*`, and `ext.discord_account` always has exactly one writer.                                    |
| **C - Trigger**                      | The CRM. The tutor role and `TUTOR_OF` are the intended state; the bot pulls them and brings Discord in line, onboarding and off-boarding. Later Discord convenience commands (enabling a student, say) write the CRM with the person's token. | Principles 3 and 4 of the sketch. SkillForge decides once, for every frontend.                                                                                             |
| **D - Where a link lives**           | On the party, in `ext.discord_account` as today (`discord_id` key, `party_id` with `ON DELETE CASCADE`, `active`, `is_primary`). `app/services/auth/discord_links.py` is its only writer. A link needs no account; the exchange needs one.     | Links to outside accounts are identity (sketch); after the exchange a link is also a credential.                                                                           |
| **E - Who links**                    | P0: an admin (`auth:users:manage`) by Discord user ID. P1: the person, by redeeming a one-time code in the bot. Never the bot as itself; the `skillbot` client never holds `auth:users:manage`.                                                | Whoever writes a link decides for whom the bot gets tokens.                                                                                                                |
| **F - Several links per person**     | A person party may have several active links. `is_primary` stays in the table, unexposed: no route sets it, and every write that activates, moves or unlinks a row writes `false`.                                                             | Links stay as today; how the bot treats several Discord accounts of one person is the bot's choice.                                                                        |
| **G - Conflicts**                    | A Discord user actively linked to another party is `409 discord_account_already_linked`: unlink first. An inactive row moves to the new party, audited with both parties.                                                                      | Silently re-pointing a credential is an account takeover.                                                                                                                  |
| **H - Delete guard**                 | Only an **active** link blocks `delete_party` (`party_in_use`); inactive rows cascade with the party.                                                                                                                                          | Otherwise a person who ever linked Discord could never be erased. "Unlink, then delete" stays visible to the bot through the feed.                                         |
| **I - Discord IDs on the wire**      | Decimal strings (`^[0-9]{1,19}$`, at most 2^63 - 1), parsed to `int` at the boundary; the column stays `BIGINT`.                                                                                                                               | Snowflakes exceed 2^53: JavaScript - the portal, release-please's rewrite of `openapi.json` - corrupts such integers. Discord's own API sends strings for the same reason. |
| **J - Reading links**                | `GET /auth/discord-links` (a feed, unlinked rows included) and `GET /auth/discord-links/{discord_user_id}` need the new `auth:discord-links:read`: in the admin role, granted to skillbot in `application` mode, grantable in both modes.      | The bot's sync needs the mapping; `auth:users:manage` is far too broad for it.                                                                                             |
| **K - Housekeeping**                 | The reaper becomes the `housekeeping` worker: it deletes sessions and one-time tokens 30 days after `expires_at` (#159). Its own slice, before the bot API goes.                                                                               | The worker never runs empty; `/health` and the deploy gate stay green.                                                                                                     |
| **L - Migration chain**              | `0001_baseline` creates the `bot` schema itself (`IF NOT EXISTS`) and drops it on downgrade. `get_schemata()` counts only packages (folders with `__init__.py`). `migrations/env.py` is unchanged.                                             | `alembic upgrade head` from an empty database keeps working without `models/bot/`. The smallest change; a leftover `bot/__pycache__/` can no longer bring the schema back. |
| **M - Retiring the schema**          | One revision, `0013_retire_bot`: an audited delete of every `bot:*` grant, the two scope rows, 14 tables, 9 enum types, `DROP SCHEMA bot` without `CASCADE`. The downgrade restores the empty 0011 structure and the scope rows, no data.      | An unknown object fails the upgrade instead of vanishing silently; the README rollback keeps working.                                                                      |
| **N - Change signals**               | The party list item gains `updated_at`; the link feed has one. One pull contract, kept in [`ARCHITECTURE.md`](../ARCHITECTURE.md) "Change signals". No relation feed, no tombstones: a periodic full comparison finds deletions.               | Every relation write already moves both parties, a delete moves every related party, and soft delete is a non-goal of `crm-api.md`.                                        |
| **O - `TUTOR_OF` follows its roles** | Removing the tutor role deletes the party's outgoing `TUTOR_OF`; removing the student role deletes its incoming `TUTOR_OF`. Both sides are moved. Amends decision J of `crm-api.md`.                                                           | Every `TUTOR_OF` is a valid pair for every consumer; no consumer re-implements a pair rule, and tutor reach later needs no role join.                                      |
| **P - Exchange grant**               | Extension grant `urn:skillforge:params:oauth:grant-type:discord-user` on `POST /auth/token` with `discord_user_id` and an optional `scope`. The client needs `auth:users:exchange` (`application`, client-only). No refresh token, no session. | RFC 8693 expects a token as the subject; the bot holds an ID.                                                                                                              |
| **Q - Exchange lookup**              | Active link -> party -> account -> `active`. Every break is the same `invalid_grant`. `locked_until`, `failed_login_count` and `last_login_at` are neither read nor written.                                                                   | The bot learns only "no identity"; the lockout guards password guessing, which does not happen here.                                                                       |
| **R - Vouched scopes**               | `VOUCHED_SCOPES = {crm:read, crm:read:own, crm:write}`. A token obtained without a password carries no other scope - enforced at issuance and at validation.                                                                                   | Account and admin actions keep demanding a password login, whatever a client is granted by mistake (sketch: "changes to an account can demand a password login").          |
| **S - Principal**                    | `UserPrincipal.login: PasswordLogin \| DiscordLogin` replaces `session_id` and `auth_methods`. `pwd` requires `sid`; `discord` forbids it.                                                                                                     | A typed sum type instead of optional fields; password tokens stay byte-identical.                                                                                          |
| **T - Login vs. exchange clients**   | `grant_client_scopes` refuses to give one client both `auth:users:login` and `auth:users:exchange`.                                                                                                                                            | The portal can never vouch, the bot can never take passwords - by code, not by discipline.                                                                                 |
| **U - Commit types**                 | Plain `feat` / `refactor` / `docs`, no `!` and no `BREAKING CHANGE`: nothing is used in production. The PR bodies name what disappears from the contract.                                                                                      | Version 1.x comes with production use.                                                                                                                                     |
| **V - Handover**                     | skillbot's docs record the Discord rules (capacity, archive, teardown, reservations, command environments) before P0-4 removes the code. Every reference points to tag `v0.5.0`, which keeps the code, its tests and the six bot specs.        | These rules live only in code, tests and commit messages today.                                                                                                            |

## Discord links

### The link and its rules

- **Table** `ext.discord_account` keeps its shape and indexes (decision D).
- **One writer.** `app/services/auth/discord_links.py` (new) writes the table; nothing else in `app/` does. It
  imports models, `audit` and `errors` only - the exchange and the link code import it, never the other way round.
- **Person parties only.** An unknown party is `422 unknown_link_party`, a company `422 link_party_not_a_person`
  (422 because `party_id` is a body reference, decision I-1 of `crm-api.md`). No account is required.
- **Party lock.** Every write that leaves a link active first locks the target party with
  `select(Party.type).where(Party.id == party_id).with_for_update(key_share=True)` - the idiom of
  `create_user_account` in [`users.py`](../../app/services/auth/users.py). It serializes link writes to one party and
  makes `delete_party` wait. A re-activation moves no foreign key, so decision H needs the explicit lock.
- **Linking** (`link_discord_account(session, *, discord_user_id, party_id, actor)`): lock the party, insert with
  `ON CONFLICT (discord_id) DO NOTHING`, lock the row (`FOR UPDATE`, `populate_existing`), then decide by the row as
  found:

  | Row as found          | Outcome                                                 | Audit event                               |
  | --------------------- | ------------------------------------------------------- | ----------------------------------------- |
  | inserted now          | active link                                             | `discord_link.added`                      |
  | active, same party    | nothing - no UPDATE, `updated_at` unchanged             | none                                      |
  | active, other party   | `DiscordAccountAlreadyLinkedError` (409), row unchanged | none                                      |
  | inactive, same party  | `active = true`                                         | `discord_link.reactivated`                |
  | inactive, other party | `party_id = <new>`, `active = true`                     | `discord_link.moved`, naming both parties |

- **Unlinking** (`unlink_discord_account(session, *, discord_user_id, actor)`): an active link gets
  `active = false`, the row stays, `discord_link.removed`; an inactive one is left alone and records nothing; an
  unknown one is `DiscordLinkNotFoundError`. No party lock.
- **`is_primary`** is written `false` by every activation, move and unlink (decision F).
- **No CRM signal.** A link write calls no `saved()` and never changes `core.party.updated_at`; the link feed is the
  signal.
- **One home for "active link":** `active_link_party_id(discord_user_id)`, a scalar subquery over active rows. The
  exchange uses it.
- **Reading:** `get_discord_link(session, discord_user_id)` and
  `list_discord_links(session, *, limit, offset, updated_since=None, party_id=None, active=None)`, ordered by
  `discord_id`, `updated_at >= updated_since`.

### Audit

- `AuditEventType` gains `DISCORD_LINK_ADDED = "discord_link.added"`, `DISCORD_LINK_REACTIVATED`,
  `DISCORD_LINK_MOVED`, `DISCORD_LINK_REMOVED`.
- The subject is the Discord user: `principal_type = "discord_user"` from a new `AuditSubjectType(StrEnum)` in
  [`audit.py`](../../app/services/auth/audit.py) (not a `PrincipalType`), `principal_id` the Discord user ID.
- `write_discord_link_audit_log(session, discord_user_id, event_type, what, *, actor)` mirrors
  `write_user_account_audit_log`: `detail` reads `<what> by <actor>.` and names the party, both parties on a move.
- A no-op records nothing.

### Routes and the feed

Router `app/api/v1/auth/discord_links.py`, prefix `/discord-links`, tag `auth`:

```
GET    /api/v1/auth/discord-links                    auth:discord-links:read  200 Page[DiscordLink]
GET    /api/v1/auth/discord-links/{discord_user_id}  auth:discord-links:read  200 DiscordLink   404
PUT    /api/v1/auth/discord-links/{discord_user_id}  auth:users:manage        200 DiscordLink   422 (2), 409
DELETE /api/v1/auth/discord-links/{discord_user_id}  auth:users:manage        204               404
```

- **`DiscordUserId`** in [`app/core/auth/inputs.py`](../../app/core/auth/inputs.py): a decimal string on the wire
  (JSON schema `type: string`, `pattern: ^[0-9]{1,19}$`), at most `2**63 - 1`, held as `int`, serialized as a string
  (decision I). A leading sign, whitespace, an exponent or a value above the bound is a `422`, never a `500`. The
  path alias `DiscordUserIdPath` lives in [`app/api/v1/auth/params.py`](../../app/api/v1/auth/params.py).
- **`DiscordLinkRequest(ApiModel)`:** `party_id: UUID` - a person party; no account needed.
- **`DiscordLink(ApiModel)`:** `discord_user_id` (string), `party_id`, `active`, `created_at`, `updated_at`, with
  `from_model`. Every field is described: `active` is false once unlinked, the row stays and a `PUT` reactivates it;
  `created_at` survives a move; `updated_at` moves on link, move and unlink and is what `updated_since` compares. No
  `is_primary`.
- **The `PUT`** answers the link, the same body on a repeat. Writes take the actor from `ManageUsers`, which moves
  from `users.py` into `params.py`.
- **`DiscordLinkListParams(PageParams)`:** `updated_since: UpdatedSince = None`, `party_id: UUID | None`,
  `active: bool | None`. The feed returns unlinked rows too, so a consumer sees deactivations.
- **`UpdatedSince`** in the new `app/api/v1/common/changes.py`:
  `Annotated[AwareDatetime | None, Field(description=...)]`. Its description carries the generic pull rules and
  points to "Change signals" in `ARCHITECTURE.md`; P0-2 creates that section with the generic rules and the link
  feed, P0-6 adds the party half and moves `PartyListParams.updated_since` onto the alias.
- **Scope** `Scope.AUTH_DISCORD_LINKS_READ = "auth:discord-links:read"`, described "Read Discord links - which
  Discord account belongs to which person party - including unlinked ones.", added to `ROLE_SCOPES[Role.ADMIN]`.

### The delete guard

`EXTERNAL_LINKS` in [`parties.py`](../../app/services/crm/parties.py) gets a "still linked" condition per kind:
`DiscordAccount.active.is_(true())` for Discord, always true for the five other tables, used by
`_external_link_kinds`. The comment above `EXTERNAL_LINKS` says the Discord table belongs to auth and a deactivated
link is history that keeps no party alive; the lock comment in `delete_party` says linking locks the party
explicitly. The `PartyInUseError` docstring and the `delete_party` route docstring say "an active Discord link". The
kind name `discord_account` stays in the contract.

### One-time link code (P1-1)

- **Purpose** `UserActionTokenPurpose.DISCORD_LINK = "discord_link"`. Its revision adds the enum label; the
  downgrade deletes the `discord_link` rows and recreates the type without the label (rename aside, create, cast,
  drop).
- **Setting** `AuthSettings.discord_link_code_expire_hours: PositiveInt = 24`, plus its `.env.example` line.
- **Issue:** `POST /auth/users/{user_id}/discord-link-code` (`auth:users:manage`) answers `201 ActionTokenResponse`.
  The account must be `active` (`409 user_account_state`); a new code invalidates the unused ones;
  `discord_link_code.issued` on the account.
- **Redeem:** `POST /auth/discord-links/redeem` with `{token, discord_user_id}` answers `200 DiscordLink`. The
  caller is an application holding `auth:users:exchange`
  (`ExchangeClient = Annotated[ApplicationPrincipal, require_application_scopes(Scope.AUTH_USERS_EXCHANGE)]`).
  - It links the party of the code's account under the rules above; the account must be `active`.
  - The code is single-use. A 409 rolls back, so the code stays live.
  - Every refused code - unknown, used, invalidated, expired, wrong purpose, account gone or disabled, party gone -
    is the same `422 invalid_action_token`.
  - Purposes stay apart both ways: a link code never sets a password, an invitation never links.
  - Audit: `discord_link_code.redeemed` on the account, plus the link event.
- **Modules.** The redemption lives in a new `app/services/auth/discord_link_codes.py`; putting it into
  `discord_links.py` would close the import cycle accounts -> discord_links -> action_tokens -> accounts.
  [`action_tokens.py`](../../app/services/auth/action_tokens.py) splits its lookup and its spend
  (`find_live_action_token(..., purposes)`, `spend_action_token`) so both redemptions share them; the password
  redemption's contract is unchanged - a disabled account can still redeem an invitation and keeps its status.
- **The bot** replies to `/link <code>` ephemerally, applies a per-user cooldown and always sends
  `interaction.user.id`.

## Housekeeping worker

- **What goes.** A session or one-time token whose `expires_at` lies more than `RETENTION_AFTER_EXPIRY = 30 days`
  in the past, whether it is revoked, rotated, used or invalidated. Only `expires_at` counts. With 30-day sessions a
  session is gone 60 days after login at the latest; until then a replayed refresh token is still denied as one of
  an ended session, naming the account.
- **Where.** `app/services/auth/housekeeping.py` holds the constant and `delete_expired_sessions(session, *, limit,
now=None)` / `delete_expired_action_tokens(...)` over one private `_delete_expired`:
  `DELETE ... WHERE id IN (SELECT id ... WHERE expires_at < :cutoff ORDER BY expires_at LIMIT :n FOR UPDATE SKIP
LOCKED)`. Housekeeping never waits on a request, so it cannot deadlock with one. It writes no audit entry: the
  history is in `auth_audit_log`, which has no foreign key to either table.
- **Worker.** `git mv app/workers/reaper.py app/workers/housekeeping.py`; the loop, signals, backstop and heartbeat
  stay. `run_cycle` runs one batch (`DELETE_BATCH_LIMIT = 1000`) per pass and cycle, each in its own transaction,
  every 30 s, and logs one `housekeeping_cycle` line with `sessions_deleted`, `action_tokens_deleted` and
  `duration_ms`. A failing pass marks the heartbeat `DEGRADED` and leaves the other pass alone. The passes are a
  `PASSES: dict[str, Pass]`, so a later table of expiring rows (the link codes are covered already) registers a pass.
- **Health.** `WorkerName.REAPER = "bot-ops-reaper"` becomes `WorkerName.HOUSEKEEPING = "housekeeping"`;
  `record_worker_heartbeat`, `read_worker_heartbeat` and `check_worker_health` take a `WorkerName`. The stale
  `bot-ops-reaper` heartbeat row is left alone.
- **Indexes.** Revision `0012_auth_expiry_indexes` adds `ix_user_session_expires_at` and
  `ix_user_action_token_expires_at` (declared on the models too), so the idle pass reads only the index.
- **Deploy.** The compose service keeps the name `worker` (a rename could leave the v0.5.0 container running as an
  orphan), runs `python -m app.workers.housekeeping` and gets `healthcheck: {disable: true}` - the image's
  healthcheck probes port 8000, which the worker never serves. `just worker-reaper` becomes
  `just worker-housekeeping`.
- **Rollback.** An older image lacks `app.workers.housekeeping`, so the README rollback and the rollback line of
  `release-flow.md` restore the tag's whole `compose.yml`, not only its `image:` lines.
- **In the same commit** the bot passes go: `app/services/bot/reaper.py`, its exports, `tests/db/test_bot_reaper_service.py`.

## Removal

### The bot API and services (P0-4)

Deleted: `app/api/v1/bot/`, `app/services/bot/`, `app/cli/deadletters.py`, the justfile's dead-letter recipes
(`dead-jobs`, `requeue`) and the bot tests: `tests/api/test_bot_*` (8 files), `tests/db/test_bot_*_service.py`,
`tests/db/test_bot_service.py`, `tests/test_bot_errors.py`. The models and `test_bot_models.py` stay until P0-5.

Cross-domain edits:

- [`router.py`](../../app/api/v1/router.py) mounts `auth` and `crm` only; [`openapi.py`](../../app/api/v1/common/openapi.py)
  drops the `bot` tag, and its `operation_id` docstring example becomes `crm_list_parties`.
- The comment on `DELEGATION_RELATION_TYPES` in [`reach.py`](../../app/core/auth/reach.py): the relations that let a
  party act for another - the `guardian` basis of reach and the derived `guardian` role; `TUTOR_OF` is absent on
  purpose, tutoring does not make a guardian.
- `app/services/crm/__init__.py` says the package imports no other domain (ADR 0009).
- The FastAPI and `pyproject.toml` descriptions follow principle 1: "the platform's hub for central data, identity,
  permissions and domain rules".

Tests that change:

- **`test_crm_architecture.py`** becomes an allow-list: a CRM module's `app.` imports start with `app.core`,
  `app.api.v1.common`, `app.services.crm` or `app.api.v1.crm`. `test_the_crm_never_imports_the_bot_domain` becomes
  `test_the_crm_imports_nothing_but_the_core_and_itself`; its self-test rejects auth, system and worker imports. The
  file stays - `test_auth_architecture.py` imports `REPO_ROOT` and `_imported_modules` from it.
- **`test_auth_architecture.py`:** `FORBIDDEN_FOR_THE_SERVICES = ("app.api", "app.services.crm", "app.services.system")`.
- **`test_error_envelope.py`** keeps its 11 items against `GET /api/v1/auth/users/{user_id}` and
  `GET /api/v1/auth/users`, with the seam `users_service.load_user_account`; the challenge becomes
  `Bearer scope="auth:users:manage"`.
- **`test_error_taxonomy.py`:** `{"app.services.auth.errors", "app.services.crm.errors"} <= set(error_modules)`.
- **`test_openapi_contract.py`:** `OPERATION_ID_PATTERN = r"(auth|crm|system)_[a-z0-9_]+"`, the spot check on
  `crm_list_parties`, the forbidden-scope test on `GET /api/v1/crm/subjects`, `PAGED_ENDPOINTS` without the bot rows
  and with `/api/v1/crm/parties` and `/api/v1/crm/subjects`, no bare-array 2xx at all.
- **`test_openapi_security_scheme.py`** drops the bot pins.
- **`test_resolve_reach.py`** asserts the exact reach instead of parity with the bot's delegation check: self is
  `SELF`, a `PARENT_OF` or `PAYS_FOR` target is `GUARDIAN`, a `TUTOR_OF` target and a stranger are absent. The reach
  arc flips the `TUTOR_OF` case.

### The schema and the scopes (P0-5)

- **Models.** `app/core/db/models/bot/` and `tests/db/models/test_bot_models.py` go, with their exports. The ORM
  relationship pair `Party.discord_accounts` / `DiscordAccount.party` goes too: its last reader left with P0-4, and
  ADR 0008 gives `Party` no relationship to identity rows.
- **Scopes.** `Scope.BOT_READ` and `Scope.BOT_WRITE` go; the admin role becomes exactly `crm:read`, `crm:write`,
  `auth:users:manage`, `auth:clients:manage`, `auth:discord-links:read`. `seed_default_scopes` stays additive.
- **Bootstrap.** `bootstrap_skillbot`, its subcommand and `just bootstrap-skillbot` go; skillbot's client is set up
  with `just bootstrap-client` when the bot needs it ([Operating](#operating)). Parameters only
  `bootstrap_skillbot` used go with it.
- **Sample scopes in tests.** 18 test files use `bot:read` / `bot:write` as sample scopes: `bot:read` becomes
  `auth:clients:manage`, `bot:write` becomes `auth:users:manage`; where such a scope sits on a fixture client named
  `skillbot`, the client is renamed (`operator`, `portal`, `integration`). No assertion gets weaker, and the test
  count only drops by the deleted skillbot bootstrap tests.
- **Migration chain (decision L).** `0001_baseline.py` starts `upgrade()` with `CREATE SCHEMA IF NOT EXISTS bot` and
  ends `downgrade()` with `DROP SCHEMA IF EXISTS bot` (no `CASCADE`); prod never reruns it (precedent #25).
  `get_schemata()` in [`schemata.py`](../../app/core/db/models/schemata.py) counts only folders with an
  `__init__.py`.

**Revision `0013_retire_bot`** - a frozen snapshot that imports no app code:

```python
def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.execute("LOCK TABLE bot.<every table> IN ACCESS EXCLUSIVE MODE")
    # 1. every bot:* grant, one scope_grant.removed audit row each (detail as revoke_application_client_scope writes it)
    # 2. DELETE FROM auth.permission_scope WHERE key IN ('bot:read', 'bot:write')
    # 3. drop the 14 tables, children first
    # 4. DROP TYPE for the 9 enum types
    # 5. DROP SCHEMA bot  -- no CASCADE: an unknown object fails the upgrade and changes nothing
```

- Tables, children first: `student_workspace`, `tutor_workspace`, `command_env_channel`, `archive_category`,
  `discord_user_permission_group`, `discord_role_binding`, `discord_channel`, `discord_user`, `discord_guild`,
  `permission_group`, `permission_grant`, `app_command_audit_log`, `job`, `operation`.
- Types: `member_role`, `discord_channel_type`, `student_channel_state`, `command_env_kind`,
  `permission_subject_type`, `permission_grant_effect`, `job_status`, `operation_kind`, `operation_status`.
- The audit rows need `gen_random_uuid()` (`auth_audit_log.id` has no default).
- **Downgrade:** `CREATE SCHEMA bot`, the 9 types with their 0011 labels, the 14 tables parents first with every
  column, default, check, foreign key and index of 0011 under its original name (derived from
  `pg_dump --schema-only --schema=bot` at 0011), and the two scope rows (`ON CONFLICT DO NOTHING`). No data, no
  grants; data only from the backup branch. The docstring says so.

## Change signals

### The pull contract

P0-2 writes it into [`ARCHITECTURE.md`](../ARCHITECTURE.md) under "Change signals" - its one living home; this spec,
`crm-api.md`, skillbot's docs and the `UpdatedSince` description link to it. It covers every SkillForge feed: the
party list and the Discord link feed.

1. **Signals, not events.** An item means "look again" and carries current state. Bring the whole current state of
   what it names in line; never apply it as a delta. Reconcilers are idempotent.
2. **`updated_at` is the start of the writing transaction.** A change can appear behind newer stamps, and an item's
   stamp can move backwards; compare two stamps of one item only for equality.
3. **`updated_since` is inclusive.**
4. **One cursor per feed:** the newest `updated_at` seen. Ask from `cursor - overlap` and keep `updated_since` fixed
   while paging. An empty page leaves the cursor alone; an error (5xx, timeout) never advances it. Without a cursor,
   start with a full comparison.
5. **Overlap:** at least the longest writing transaction plus one poll pass, 5 minutes by default. SkillForge keeps
   writing transactions short; a change delayed longer is repaired by the next full comparison. A data migration
   that writes parties is announced, so consumers run a full comparison afterwards.
6. **Filter only by what never changes** - `type=person`, never `role`, `subject_id` or `q`: a party that stops
   matching a filter drops out silently.
7. **Pages are not a snapshot.** Stop at a page shorter than `limit`, never by `total`. A full first page: page to
   the end, then run a full comparison.
8. **Full comparison** at start, after a full first page and periodically (default every 30 minutes): first the link
   feed without `updated_since`; then `GET /crm/parties?type=person`, handling every item whose (`id`, `updated_at`)
   differs from the stored one like a feed item; then `GET /crm/parties/{id}` for every stored party that did not
   appear - `404` means deleted, `200` means skipped (bring it in line). A 404 is conclusive only with the
   unrestricted application `crm:read`.
9. **Relations.** For each changed person read `GET /crm/parties/{id}/relations?type=tutor_of`, paged to a short
   page, and re-evaluate every pair touching that person.
10. **Deletions.** A deleted party is never reported; every party related to it is moved. A party with an active
    Discord link cannot be deleted. The full comparison catches the rest.
11. **Not in any feed:** subject titles (compare `GET /crm/subjects` in full if shown), account state and the admin
    role - the exchange and `GET /auth/me` decide those per command. Disabling an account off-boards nobody from
    Discord; removing the role, the `TUTOR_OF` or the link does.
12. **Nothing flows back.** The sync only reads. CRM changes from commands go through the CRM routes with the
    person's token.

Consumer defaults (skillbot's configuration): poll every 60 s, overlap 5 minutes, full comparison every 30 minutes,
skip an item whose (`id`, `updated_at`) equals the stored one.

### What moves what

| Write                           | Route                                                          | Moves `updated_at` of                                        |
| ------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------ |
| Create                          | `POST /crm/persons`, `/companies`                              | the new party                                                |
| Names                           | `PATCH /crm/persons/{id}`, `/companies/{id}`                   | the party, whenever a field is sent (an empty body: nothing) |
| Give or change a role           | `PUT /crm/persons/{id}/student`, `/tutor`                      | the person, on a real change                                 |
| Take a role away                | `DELETE /crm/persons/{id}/student`, `/tutor`                   | the person and every other side of the `TUTOR_OF` it removed |
| Contact infos                   | `POST` / `PATCH` / `DELETE /crm/parties/{id}/contact-infos...` | the party                                                    |
| Relate / unrelate               | `PUT` / `DELETE /crm/parties/{id}/relations/{type}/{to}`       | both parties (a repeated `PUT`: nothing)                     |
| Delete a party                  | `DELETE /crm/parties/{id}`                                     | every party related to it                                    |
| Subjects                        | `/crm/subjects...`                                             | no party                                                     |
| Discord links                   | `/auth/discord-links...`                                       | the link row only (the link feed)                            |
| Accounts, admin role, disabling | `/auth/users...`                                               | nothing                                                      |

### Pull signals in the CRM (P0-6)

- `PartyListItem` in [`schemas.py`](../../app/api/v1/crm/schemas.py) gains a required, described `updated_at`
  (placed last): "When anything in the party last changed - the person or company, a role, a contact info, a
  relation - or a party related to it was deleted. The start of the writing transaction: pull as `updated_since`
  describes." `RelationResponse.party` carries it automatically; `PARTY_GRAPH` is unchanged.
- `PartyListParams.updated_since` moves onto `UpdatedSince`.
- **Decision O** in [`roles.py`](../../app/services/crm/roles.py): `remove_tutor_role` deletes the party's outgoing
  `TUTOR_OF`, `remove_student_role` its incoming `TUTOR_OF`, and both end with `saved(session, party_id,
*other_sides)`.
- `crm-api.md`: "List and search" says "from the newest `updated_at` seen minus an overlap" and links the contract;
  decision J is amended by P0-1.

## Token exchange

### On the wire

```
POST /api/v1/auth/token                (form; client authentication: Basic or form, as for every grant)
grant_type=urn:skillforge:params:oauth:grant-type:discord-user
discord_user_id=123456789012345678     [&scope=crm:read:own]

200 {"access_token": "...", "token_type": "bearer", "expires_in": 900, "scope": "crm:read:own"}
claims: sub=user:<account>, azp=<client>, party_id, roles, amr=["discord"] - no sid
```

| Failure                                                           | Answer                       | Audit                                              |
| ----------------------------------------------------------------- | ---------------------------- | -------------------------------------------------- |
| unknown `grant_type`                                              | 400 `unsupported_grant_type` | -                                                  |
| client credentials or `discord_user_id` missing or malformed      | 422 `invalid_request`        | -                                                  |
| unknown, disabled or over-long client; wrong secret               | 401 `invalid_client`         | `token.denied` (application)                       |
| client without `auth:users:exchange`                              | 400 `unauthorized_client`    | `token.denied`, "Client lacks auth:users:exchange" |
| no or inactive link, no account, disabled account                 | 400 `invalid_grant`          | `token.denied` (user), naming the Discord ID       |
| scope not granted, above the ceiling or not vouched; empty result | 400 `invalid_scope`          | `token.denied` (user), naming the Discord ID       |

### Technique (P0-7)

- **Scopes** ([`scopes.py`](../../app/core/auth/scopes.py)): `AUTH_USERS_EXCHANGE = "auth:users:exchange"`, described
  "Obtain a person's token for the Discord user they linked, and redeem Discord link codes; the client's secret
  speaks for every linked person up to its delegated ceiling. Client-only: never part of a person's token."
  `CLIENT_ONLY_SCOPES = {AUTH_USERS_LOGIN, AUTH_USERS_EXCHANGE}`. `VOUCHED_SCOPES` (decision R), documented as "every
  other scope - `account:self`, every `auth:*` and every scope added later - needs a password login until it is added
  here on purpose".
- **Principal** ([`principal.py`](../../app/core/auth/principal.py)): `AuthMethod.DISCORD = "discord"`; frozen
  dataclasses `PasswordLogin(session_id)` and `DiscordLogin()`, `type Login = PasswordLogin | DiscordLogin`;
  `UserPrincipal` carries `party_id`, `roles` and `login`.
- **Claims** ([`tokens.py`](../../app/core/auth/tokens.py)): `_UserClaims.sid: uuid.UUID | None = None` with a
  `mode="before"` validator refusing an explicit `null`; `amr` and `sid` map to a `Login` (`[pwd]` + a UUID,
  `[discord]` + none, anything else is invalid); a validator refuses a `discord` token carrying a scope outside
  `VOUCHED_SCOPES`. `amr` stays a list on the wire; `sid` is written only for `PasswordLogin`.
- **Lookup** (`accounts.py`): `find_user_account_by_discord_user(session, discord_user_id)` reads the account (roles
  loaded) whose `party_id` is `active_link_party_id(discord_user_id)`. No row lock.
- **Service** (`app/services/auth/tokens.py`): `exchange_discord_user(session, settings, *, client_id, client_secret,
discord_user_id, requested_scopes=None, now=None) -> ExchangeResult` with
  `type ExchangeResult = CreatedAccessToken | TokenDenial`:
  1. authenticate the client and require `auth:users:exchange` - `_authenticate_login_client` generalizes to
     `_authenticate_client_for(session, required, ...)`;
  2. look up the account; none or `disabled` is `invalid_grant`;
  3. `resolve_token_scopes(requested, granted=<delegated grants>, ceilings=[scopes_for(roles), VOUCHED_SCOPES])`;
  4. mint through a shared `_mint_user_token(..., login=DiscordLogin(), ...)`, which the password and refresh grants
     use with `PasswordLogin`. `token.issued` reads "Exchanged Discord user <id> through client <client_id>."
     Denials are returned, never raised.
- **Endpoint** ([`token.py`](../../app/api/v1/auth/token.py)): `GrantType.DISCORD_USER`, a frozen
  `DiscordUserGrant(discord_user_id: int)`, a `discord_user_id` form field declared `str | None` and parsed with
  `TypeAdapter(DiscordUserId)`, a seam `get_exchange_discord_user`, a `match` arm. The `create_token` description
  names the grant, its scope, "no refresh token" and "Swagger UI's Authorize dialog cannot use this grant; try it with
  curl". The security scheme is unchanged.
- **Decision T** in `grant_client_scopes` ([`scopes.py`](../../app/services/auth/scopes.py)): granting
  `auth:users:exchange` to a client holding `auth:users:login` (or the reverse) is refused with
  `InvalidClientScopeError`, the answer a client-only scope gets in `delegated` mode.
- `last_login_at` is described as "When the person last logged in with their password".
- **#158** stays independent: whichever lands second adapts (`match principal.login: case PasswordLogin(...)`).
  Discord tokens never carry `account:self`, so #158 needs no new error.

### The bot's side of the contract

- Cache tokens per (`discord_user_id`, requested scope) until shortly before `expires_in`, single-flight per key, in
  memory only; re-exchange once on a `401`. Cap concurrent exchanges - each costs one Argon2 check of the client
  secret.
- Exchange only for `interaction.user.id`, never for an ID from command options, and only for users with an active
  link in the local copy. Evict on a deactivated link.
- `invalid_grant` means "offer only what needs no identity"; `invalid_scope` means "not permitted".
- The application token is for the sync only.

## Route map

```
# Added - auth
GET    /api/v1/auth/discord-links                       auth:discord-links:read      P0-2
GET    /api/v1/auth/discord-links/{discord_user_id}     auth:discord-links:read      P0-2
PUT    /api/v1/auth/discord-links/{discord_user_id}     auth:users:manage            P0-2
DELETE /api/v1/auth/discord-links/{discord_user_id}     auth:users:manage            P0-2
POST   /api/v1/auth/token  (grant discord-user)         client: auth:users:exchange  P0-7
POST   /api/v1/auth/users/{user_id}/discord-link-code   auth:users:manage            P1-1
POST   /api/v1/auth/discord-links/redeem                client: auth:users:exchange  P1-1

# Changed
GET    /api/v1/crm/parties                              items gain updated_at         P0-6
DELETE /api/v1/crm/persons/{id}/tutor|student           also removes the TUTOR_OF    P0-6
GET    /health/workers/{worker_name}                    housekeeping                  P0-3

# Removed
/api/v1/bot/users/{discord_id}/account (PUT, DELETE)                                  P0-2
/api/v1/bot/... (the other 34 operations)                                            P0-4
```

## Error catalog

The auth catalog is closed and grows by exactly these codes, under a new base `DiscordLinkManagementError` in
[`errors.py`](../../app/services/auth/errors.py):

| Class                              | Status | `code`                           | Raised when                                    |
| ---------------------------------- | ------ | -------------------------------- | ---------------------------------------------- |
| `DiscordLinkNotFoundError`         | 404    | `discord_link_not_found`         | GET or DELETE of an unknown Discord user ID    |
| `DiscordAccountAlreadyLinkedError` | 409    | `discord_account_already_linked` | PUT or redeem while the ID is active elsewhere |
| `UnknownLinkPartyError`            | 422    | `unknown_link_party`             | PUT: no such party                             |
| `LinkPartyNotAPersonError`         | 422    | `link_party_not_a_person`        | PUT: a company                                 |

Reused: `invalid_action_token` for every refused redemption; `user_account_not_found` and `user_account_state` when
a code is issued; the token endpoint's OAuth errors. Not reused: `unknown_account_party` and
`account_party_not_a_person` - their messages name user accounts, and a link needs none. Removed: every code of
`app/services/bot/errors.py` - two in P0-2 (`AccountLinkConflictError`, the bot's `DiscordAccountNotFoundError`), the
rest in P0-4.

## Security rules

- **The `skillbot` client never holds** `auth:users:manage`, `auth:clients:manage` or `auth:users:login`, in any
  mode; its delegated grants never include `auth:*` or `account:self`.
- **No link is written by the bot as itself.** P1 redemption is proof of possession by the person.
- **No plaintext secret** - link code, token, password - in a log, an audit `detail` or an error `detail`. Discord
  user IDs appear in audit rows, never in the request log or in claims.
- **Before `auth:users:exchange` is granted** in prod, an admin reviews every row of `ext.discord_account` and
  deactivates every link nobody can vouch for.
- **skillbot's secret is rotated** with its first new grant: the deployed v0.1.0 holds the current one.

## Operating

**Grants.**

- After the release that carries P0-2: grant the `operator` client `auth:discord-links:read` in `delegated` mode; its
  documented line in `user-authentication.md` becomes `--delegated "account:self crm:read crm:write auth:users:manage
auth:clients:manage auth:discord-links:read"`. Admins log in again.
- When skillbot's pull loop is ready: rotate its secret (`POST /auth/clients/skillbot/secrets`, then delete the old
  ones) and grant `application` `crm:read auth:discord-links:read`.
- After the release that carries P0-7 and the link review: add `application` `auth:users:exchange` and `delegated`
  `crm:read`. Check with `GET /auth/clients/skillbot` that nothing from the security rules is granted and that no other
  client holds `auth:users:exchange`.

**Releases.** One cleanup release after P0-5 (P0-1 to P0-5). Then as the bot needs them: P0-6 unblocks its pull loop,
P0-7 its commands, P1-1 `/link`; they may be combined.

**Pre-flight** before merging the release PR that carries P0-5 - read-only, results recorded in the epic:

1. A Neon backup branch of prod (`pre-bot-retire-YYYY-MM-DD`), kept 14 days after verification.
2. A rehearsal branch: `alembic upgrade head`, `downgrade 0011_grant_mode_user_accounts`, `upgrade head` with the prod
   migration role, at the release PR's head. Delete it afterwards.
3. The queries:

```sql
-- P1 revision and role
SELECT version_num FROM public.alembic_version;            -- expect 0011_grant_mode_user_accounts
SELECT current_user, current_setting('server_version');
-- P2 schemas and owners
SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace
WHERE nspname IN ('auth','bot','core','ext','geo','system','skillbot','public') ORDER BY 1;
-- P3 everything in schema bot: expect only the 14 tables and 9 types
SELECT pg_describe_object(classid, objid, objsubid) FROM pg_depend
WHERE refclassid = 'pg_namespace'::regclass AND refobjid = 'bot'::regnamespace ORDER BY 1;
-- P3b owners other than the migration role: expect 0 rows
SELECT 'relation', relname, pg_get_userbyid(relowner) FROM pg_class
 WHERE relnamespace = 'bot'::regnamespace AND relowner <> '<migration role>'::regrole
UNION ALL SELECT 'type', typname, pg_get_userbyid(typowner) FROM pg_type
 WHERE typnamespace = 'bot'::regnamespace AND typowner <> '<migration role>'::regrole;
-- P4 nothing outside bot depends on bot: 0 rows each
SELECT conrelid::regclass, confrelid::regclass, conname FROM pg_constraint
WHERE contype = 'f' AND connamespace <> 'bot'::regnamespace
  AND confrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'bot'::regnamespace);
SELECT a.attrelid::regclass, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace <> 'bot'::regnamespace AND NOT a.attisdropped
  AND a.atttypid IN (SELECT oid FROM pg_type WHERE typnamespace = 'bot'::regnamespace);
-- P5 what the drop loses, and the links
SELECT 'job', count(*) FROM bot.job UNION ALL SELECT 'operation', count(*) FROM bot.operation;  -- ... all 14
SELECT count(*), count(*) FILTER (WHERE active), count(*) FILTER (WHERE is_primary) FROM ext.discord_account;
-- P6 every client holding bot scopes, and live sessions carrying them
SELECT c.client_id, c.status, g.scope_key, g.mode FROM auth.application_client_scope_grant g
JOIN auth.application_client c ON c.id = g.application_client_id WHERE g.scope_key LIKE 'bot:%' ORDER BY 1, 3, 4;
SELECT count(*) FROM auth.user_session
WHERE revoked_at IS NULL AND expires_at > now() AND ' ' || scope || ' ' LIKE '% bot:%';
-- P7 the legacy skillbot schema (run it now, independent of the arc)
SELECT relname, relkind, reltuples::bigint FROM pg_class WHERE relnamespace = to_regnamespace('skillbot') ORDER BY 1;
SELECT conrelid::regclass, confrelid::regclass, conname, confdeltype FROM pg_constraint
WHERE contype = 'f' AND connamespace = to_regnamespace('skillbot');
-- P8 heartbeat rows
SELECT worker_name, last_beat_at, expires_at, last_status FROM system.worker_heartbeat;
```

If P7 finds the legacy `skillbot` schema, and only with the owner's go: `pg_dump --schema=skillbot --format=custom`,
then drop its tables and the schema by hand without `CASCADE`; record it in the epic. After the deploy:
`alembic_version` is the head, there is no `bot` namespace, `/health` reports `ok` with
`workers.checks == {"housekeeping": "ok"}`.

## Handover to skillbot

Before P0-4 merges, skillbot has a `docs/` (specs and decisions) that records, with references to SkillForge tag
`v0.5.0`:

- **Discord medium rules:** tutor capacity 0..49 (students in the tutor category plus live activate/pop reservations;
  stashed students do not count, a prepared deactivation frees nothing); archive categories 1..50, first fit by
  `archive_no`, a clear answer when none is configured and when all are full; tutor teardown refused while any
  student or inbound reservation exists, re-checked under lock; a child channel deleted before its category.
- **Reservation semantics:** the natural key (guild, subject, kind); a retry replays, a different tutor conflicts; an
  expired but unswept reservation is reclaimed; stash locks the workspace before the idempotency lookup; cancelled
  differs from expired; `OPERATION_TTL` 10 minutes; the partial unique index and the savepoint insert that catch the
  race (commit `072c3b7`); the lease and sweeper of `lifecycle-guardian.md`.
- **Command environments:** at most one per kind per owner per guild, keyed by `party_id`; `teacher_cmd` becomes
  `tutor_cmd`.
- **Topology and roles:** keyed by `party_id` and Discord role IDs; roles project from SkillForge to Discord only,
  never back - `_fallback_role_from_discord` (a role _name_ granting admin) goes.
- **Off-boarding:** never touches CRM data; a removed role, `TUTOR_OF` or active link triggers teardown; archive vs.
  delete and retention are the bot's decision.
- **Open for the business:** one tutor per student or several (the CRM allows several `TUTOR_OF`); an explicit deny
  for one person has no successor.
- **Contracts with SkillForge:** the pull contract, the exchange contract above, error handling on the ADR 0006
  `code`, the `/link` command, and "no tutor command acts on a student through SkillForge until the reach arc".
- **Reference material at `v0.5.0`:** `tests/db/test_bot_transitions_service.py` (55 items, concurrency included),
  `test_bot_operations_service.py`, `test_bot_reaper_service.py`, `test_bot_jobs_service.py`, the six bot specs and
  ADRs 0003 and 0004. Closed issues #4, #47, #48, #50 and #52 carry unticked criteria - their rules are not done.

## Requirements

### Must-have (P0)

**Standing criteria - they hold for every slice and are ticked with P0-7.**

- [ ] `just check-all` is green; `openapi.json` is regenerated with `just openapi`, never edited.
- [ ] Nothing under `app/services/crm` or `app/api/v1/crm` imports anything but `app.core`, the shared API vocabulary
      and the CRM itself; nothing under `app/services/auth` imports `app.api`, `app.services.crm` or
      `app.services.system`.
- [ ] `ext.discord_account` has exactly one writer in `app/` at every commit.
- [ ] `/health` stays `ok` across every release; `WorkerName` is never empty.
- [ ] No plaintext secret in a log, an audit `detail` or an error `detail`; no Discord user ID in the request log or
      in claims.
- [ ] Every new schema property and every new path and query parameter is described; new schemas derive from
      `ApiModel`; operation IDs are derived; no Discord ID appears as a JSON integer in `openapi.json`.
- [ ] Commit and PR titles are conventional without `!` (decision U); a spec's boxes are ticked in the PR that
      fulfils them.

**P0-1 - Spec and ADR.** _Docs only._ PR `docs: decide that the bot owns its Discord workflows`.

- _Technique:_ this spec; ADR 0009; status lines - 0003 and 0004 `Superseded by 0009`, 0005, 0007 and 0008 `amended
by 0009` - and the decisions README (rows, new row 0009, the status value `Amended by NNNN`); the six bot specs
  (`lifecycle-guardian`, `off-boarding-transitions`, `operation-cancel`, `ops-read-plane`,
  `principals-and-provisioning`, `batch-lookups`) get `Status: Superseded by [bot-decoupling.md](bot-decoupling.md)
(2026-09)`, bodies untouched; `user-authentication.md` marks superseded its non-goal "Any change to the `bot`
  schema or the grant engine", the criterion "SkillBot keeps working", "The bot's delegation check answers exactly as
  before", the `bootstrap-skillbot` criterion of P0-4, the admin row's `bot:read`, "Open: whether `admin` also
  carries `bot:write`" and the agent rule "Do not touch the `bot` schema, the grant engine or `delete_party`", and
  points "Designed for the bot arc" to this spec and to the reach arc; `crm-api.md` amends decision J (decision O)
  and adds the active-link qualifier to its delete rules (decision H); `CLAUDE.md` drops "Discord state changes run
  in two phases" and "Jobs are at-least-once", adds "Frontends pull; SkillForge pushes nothing (ADR 0009)", takes
  `user-authentication.md` as the spec-first reference and `MAX_PAGE_LIMIT` in `pagination.py` as its symbol
  example; `ARCHITECTURE.md` drops "Roadmap: capability arcs" and points to the sketch's roadmap; `PROJECT.md`
  roadmap item 2 loses tutor reach, which becomes its own item with #160 and #161.
- _Acceptance criteria:_
  - [x] This spec and ADR 0009 are on `main`; the decisions index lists 0009 and the new status values.
  - [x] No accepted ADR's body changed; only status lines.
  - [x] The epic exists, links this spec and the ADR, and holds #159 and the skillbot issues (handover, rebuild).

**P0-2 - Discord links in auth.** PR `feat(auth): manage Discord links as identities`.

- _Technique:_ [Discord links](#discord-links) without the link code: `discord_links.py` (service and router),
  `DiscordUserId`, `changes.py` with `UpdatedSince`, the scope and the admin role, `AuditSubjectType` and the four
  events, the four errors, `ManageUsers` in `params.py`, the delete guard, "Change signals" in `ARCHITECTURE.md`
  (generic contract and link feed), the operator line in `user-authentication.md`. Removes the two bot link routes,
  their schemas, `provisioning.link_discord_account` / `deactivate_discord_account` and the two bot errors.
- _Tests:_ `tests/db/auth/test_discord_links_service.py` ports the six link tests of
  `tests/db/test_bot_provisioning_service.py` (with snowflake-sized IDs) and adds the rest; an endpoint test with the
  service seam; an authz test over every `/auth/discord-links` operation (401 challenge, 403 without the scope, each
  scope alone insufficient for the other side); a concurrency test in two sessions. `PAGED_ENDPOINTS` gains the feed;
  `test_roles.py` the admin scope; `test_openapi_security_scheme.py` loses the two link pins;
  `test_crm_party_delete_api.py::test_an_inactive_discord_account_still_guards_the_party` becomes "a deactivated
  Discord link goes with its party". The seven link tests of `tests/api/test_bot_users_endpoint.py` go.
- _Acceptance criteria:_
  - [ ] A `PUT` with a person party answers 200 with an active link; a repeat answers the same body, leaves
        `updated_at` unchanged and writes no audit row.
  - [ ] An unknown party is 422 `unknown_link_party`, a company 422 `link_party_not_a_person`, an ID active
        elsewhere 409 `discord_account_already_linked` with the row unchanged.
  - [ ] A person may hold several active links; `is_primary` is `false` after every write.
  - [ ] An inactive link is reactivated (same party) or moved (other party), each with its own event; a move names
        both parties.
  - [ ] `DELETE` of an active link answers 204, keeps the row with `active = false` and writes `discord_link.removed`;
        of an inactive one 204 and nothing; of an unknown one 404.
  - [ ] Every change writes exactly one audit row with `principal_type = "discord_user"`; no link write changes
        `core.party.updated_at`.
  - [ ] A Discord ID is a string in `openapi.json`; `-1`, `+5`, `2**63` and `1e3` are 422 in path and body, never 500.
  - [ ] The feed pages `Page[DiscordLink]` ordered by `discord_id`, includes inactive links, keeps
        `updated_at >= updated_since`, filters by `party_id` and `active`.
  - [ ] Reads need `auth:discord-links:read`, writes `auth:users:manage`; the scope is in the admin role and
        grantable in both modes.
  - [ ] `delete_party` answers 409 `party_in_use` naming `discord_account` while an active link exists, and 204 with
        only inactive links, which cascade.
  - [ ] A re-activation racing `delete_party` never ends in a 200 whose link then vanishes; two `PUT`s of one ID for
        two parties end in one 200 and one 409.
  - [ ] The two bot link routes, their schemas and the two bot errors are gone; `discord_links.py` is the only
        writer.

**P0-3 - Housekeeping worker.** PR `feat(workers): delete expired sessions and one-time tokens in a housekeeping
worker`, `Closes #159`.

- _Technique:_ [Housekeeping worker](#housekeeping-worker); revision `0012_auth_expiry_indexes`; `ARCHITECTURE.md`
  replaces "Lifecycle guardian" with a housekeeping section; `DATABASE_SCHEMA.md` gains the indexes; `CLAUDE.md`'s
  workers line; `user-authentication.md` P1-3 records the implementation; the README and `release-flow.md` rollback
  restore the whole `compose.yml`; #159's text names the new paths.
- _Tests:_ `tests/db/auth/test_auth_housekeeping.py` (boundary to the microsecond, revoked sessions go by
  `expires_at`, every `UserActionTokenPurpose`, the limit, no audit and no side effects, a held row skipped without
  waiting); `test_reaper_cycle.py` becomes `test_housekeeping_cycle.py`; a no-DB test ties the compose command to an
  importable module; `test_health.py`, `test_worker_heartbeat_service.py` and `test_system_models.py` use the new
  name; `test_bot_reaper_service.py` goes.
- _Acceptance criteria:_
  - [ ] A session or action token more than 30 days past `expires_at` is deleted, one on the boundary or inside it
        stays - live, rotated, revoked, used or invalidated alike.
  - [ ] A call deletes at most `limit` rows, oldest first; a row another transaction holds is skipped, not waited
        on, and deleted later. No audit row is written.
  - [ ] Each pass runs one batch per cycle in its own transaction; a failing pass marks the heartbeat `DEGRADED` and
        the cycle still logs one `housekeeping_cycle` line with exactly its three counters.
  - [ ] `WorkerName` has exactly `HOUSEKEEPING`; `GET /health/workers/housekeeping` reports it.
  - [ ] compose's `worker` runs `python -m app.workers.housekeeping` with its healthcheck disabled;
        `just worker-housekeeping` exists, `worker-reaper` does not.
  - [ ] The revision adds both indexes, `alembic check` is clean, the downgrade drops them.
  - [ ] `app/services/bot/reaper.py` is gone; no `reaper` remains in `app/` outside `app/services/bot/`, in
        `tests/`, `compose.yml`, the justfile, `CLAUDE.md`, `ARCHITECTURE.md` or `DATABASE_SCHEMA.md`.

**P0-4 - Remove the bot API.** PR `feat(api): remove the bot API`; the body lists the 34 removed operations and says
the published client loses `api.bot`.

- _Technique:_ [The bot API and services](#the-bot-api-and-services-p0-4); `CLAUDE.md` layout; `ARCHITECTURE.md`
  ("What is SkillForge?", layers, "The bot as a consumer" becomes "Frontends pull"); the README's "on its way out"
  sentence.
- _Acceptance criteria:_
  - [ ] No `/api/v1/bot` path, no `bot` tag, no bot schema in `openapi.json`; every operation ID matches
        `(auth|crm|system)_...`.
  - [ ] `git grep -nE "services\.bot|api\.v1\.bot|/api/v1/bot" -- app tests` is empty; `just --list` shows no
        `dead-jobs` or `requeue`.
  - [ ] The allow-list test passes and its self-test rejects auth, system and worker imports.
  - [ ] `test_error_envelope.py` runs its 11 items against `/api/v1/auth/users`; no bare-array 2xx remains.
  - [ ] The exact reach is asserted, `TUTOR_OF` lending none.
  - [ ] skillbot's handover docs are merged and reference `v0.5.0`.

**P0-5 - Drop the bot schema, retire the bot scopes.** PR `feat(db): drop the bot schema and retire the bot scopes`.

- _Technique:_ [The schema and the scopes](#the-schema-and-the-scopes-p0-5); `DATABASE_SCHEMA.md` without the bot
  schema; `CLAUDE.md` (commands without `bootstrap-skillbot`, the schema line, the migration convention of ADR 0009);
  `ARCHITECTURE.md`'s schema list; the README's dev setup; runbook step 1 of `user-authentication.md` without
  `bot:read`.
- _Tests:_ `test_migration_apply.py` - the chain (after `upgrade head` the application schemas are exactly `auth`,
  `core`, `ext`, `geo`, `system`; after `downgrade base` none), the two historical enum tests pinned to 0011, a
  reversibility test (seed `bot:*` grants in both modes plus an unrelated grant and a `bot.job` row at 0011; upgrade;
  assert no `bot` namespace, only the unrelated grant, one `scope_grant.removed` per deleted grant; downgrade to 0011;
  assert the catalog fingerprint - columns, constraints, indexes, enum labels - equals the recorded one), and a
  refusal test (`CREATE TABLE bot.stray` makes the upgrade fail and change nothing). The sample-scope swap; the
  skillbot bootstrap tests go.
- _Acceptance criteria:_
  - [ ] `alembic upgrade head` from an empty database works without `models/bot/`, also with a leftover
        `bot/__pycache__/`.
  - [ ] Upgrading a 0011 database leaves no `bot` namespace, no `bot:*` scope row or grant, one audit row per
        deleted grant and every other grant untouched; an unknown object in `bot` fails the upgrade and changes
        nothing; the downgrade restores the 0011 catalog and the scope rows, no grants.
  - [ ] `Scope` has no `bot:` member; the admin role is exactly the five scopes; neither OAuth2 flow lists `bot:`.
  - [ ] `git grep -nE "bot:(read|write)|BOT_(READ|WRITE)|bootstrap.skillbot" -- app tests justfile README.md CLAUDE.md ':!tests/db/test_migration_apply.py'` is empty.
  - [ ] `Party` has no relationship to `DiscordAccount`.
  - [ ] The pre-flight's queries and the rehearsal are recorded in the epic before the release PR merges.

**P0-6 - Pull signals.** PR `feat(crm): signal changes for pulling frontends`; the body states that removing a role
now removes its `TUTOR_OF`.

- _Technique:_ [Pull signals in the CRM](#pull-signals-in-the-crm-p0-6); "Change signals" in `ARCHITECTURE.md` gains
  the party half and "What moves what".
- _Tests:_ every item carries its party's `updated_at` (distinct stamps with a non-zero microsecond part); an item's
  `updated_at` passed back as `updated_since` returns it again; a related bystander stays unmoved by every write in
  `WRITES`; subject writes move no party; removing the tutor role removes the outgoing `TUTOR_OF` and moves both
  sides, removing the student role the incoming one; the OpenAPI test pins `updated_at` as required `date-time`.
- _Acceptance criteria:_
  - [ ] `PartyListItem.updated_at` is required and described and equals the detail's value.
  - [ ] Every CRM write moves exactly the parties "What moves what" names.
  - [ ] After removing a tutor or student role no `TUTOR_OF` names the party on that side, and both sides moved.
  - [ ] `updated_since` on the party list and the link feed comes from the one `UpdatedSince` alias.

**P0-7 - Token exchange.** PR `feat(auth): exchange a Discord user for a person token`.

- _Technique:_ [Token exchange](#token-exchange); `ARCHITECTURE.md` (four grants, `login`, `VOUCHED_SCOPES`);
  `user-authentication.md` (claims, `UserPrincipal`, the P0-5 wording on `sid`, decision Q, goal 7).
- _Tests:_ `tests/db/auth/test_auth_exchange_api.py` (the happy path with no session row and a working
  `GET /auth/me`; a Discord-only account; the four broken links answering one `invalid_grant`, each audited; two
  active links of one person both exchange; a locked account exchanges with its counters untouched; the scope matrix
  for student, tutor, guardian and admin; `VOUCHED_SCOPES` holding even when `account:self` or `auth:*` is
  delegated; login-only and exchange-only clients; Basic and form authentication; no Discord ID or e-mail in the
  captured log); `test_auth_token_grants.py` (malformed IDs are `invalid_request` before any lookup);
  `test_person_tokens.py` and `test_auth_me_endpoint.py` (`discord` with `sid`, `pwd` without or with `sid: null`, a
  mixed or unknown `amr` are invalid); `test_scopes.py` (`CLIENT_ONLY_SCOPES` has two members, `VOUCHED_SCOPES` is
  pinned); the mechanical `UserPrincipal(...)` edits.
- _Acceptance criteria:_
  - [ ] With `application` `auth:users:exchange`, an active link to an active account answers 200 with exactly four
        keys and opens no session; the token has `amr ["discord"]`, no `sid`, and validates to `DiscordLogin`.
  - [ ] Password and refresh tokens are byte-identical to before.
  - [ ] Every broken link answers the same `invalid_grant`; a locked account exchanges and no login field changes.
  - [ ] With `delegated` `crm:read` a non-admin gets `crm:read:own`, an admin `crm:read`; no Discord token carries a
        scope outside `VOUCHED_SCOPES`.
  - [ ] A client without the scope is `unauthorized_client`; the scope cannot be granted in `delegated` mode or beside
        `auth:users:login`.
  - [ ] The link review is recorded in the epic before the grant; all standing criteria are ticked.

### Nice-to-have (P1)

**P1-1 - One-time link code.** PR `feat(auth): link a Discord account with a one-time code`.

- _Technique:_ [One-time link code](#one-time-link-code-p1-1); `.env.example`; #163 lists the new secret-returning
  route. It closes the epic and flips the rows "Bot state", "Bot permissions" and "Change signals" of the sketch's
  "Where we are".
- _Acceptance criteria:_
  - [ ] Issuing returns the code once (201), invalidates earlier unused codes and answers 409 for a disabled account.
  - [ ] Redeeming with `auth:users:exchange` links the code's party and spends the code; a person's token gets 403;
        every refused code gets the same 422; after a 409 the code is still live.
  - [ ] `/auth/password/redeem` with a link code answers 422 and sets no password; an invitation on a disabled
        account still answers 204 and leaves the status.
  - [ ] Redeeming against `delete_party` does not deadlock, and of two concurrent redemptions exactly one wins.
  - [ ] No plaintext code in a log or an audit row.

### Future considerations (P2)

- Self-service link codes (`POST /auth/me/discord-link-code`, needs a password login and #158).
- Discord OAuth in the portal as a way to link.
- Exposing `is_primary`, if the bot needs a person's main Discord account.
- A retention for `auth_audit_log`, which now also records every exchange.

## Timeline / phasing

One PR per requirement, all in one GitHub stack (`gh stack`), bottom to top in the order of the requirements:

| Slice    | Needs                                   | Notes                                                            |
| -------- | --------------------------------------- | ---------------------------------------------------------------- |
| **P0-1** | nothing                                 | skillbot's handover docs start in parallel                       |
| **P0-2** | P0-1                                    | before P0-4, so `ext.discord_account` never lacks a writer       |
| **P0-3** | P0-1                                    | independent of P0-2 in code                                      |
| **P0-4** | P0-2, P0-3; skillbot's handover merged  | the cross-repo gate lives in the PR's criteria, not in the stack |
| **P0-5** | P0-4                                    | the release after it needs the pre-flight                        |
| **P0-6** | P0-2 (`UpdatedSince`, "Change signals") | can be written beside P0-3 to P0-5                               |
| **P0-7** | P0-2                                    | coordinate with #158                                             |
| **P1-1** | P0-7 (`auth:users:exchange`)            |                                                                  |

- Revisions: `0012_auth_expiry_indexes` (P0-3), `0013_retire_bot` (P0-5), the link-code purpose at the next free
  number (P1-1). P0-2, P0-4, P0-6 and P0-7 have none.
- **`openapi.json` is never merged by hand.** After every rebase a slice takes either side, reruns `just openapi` and
  `just check-all`.
- The stack needs signed PR commits; a run in the cloud needs a re-sign pass before the stack merges.

## Rules for implementing agents

- The rules of [`api-conventions.md`](api-conventions.md), [`crm-api.md`](crm-api.md) and
  [`user-authentication.md`](user-authentication.md) apply (English only, symbol references in docs, never hand-edit
  `openapi.json`, `just check` green before every commit, conventional commits without `!`).
- Agents may write the CRM parts of this arc (the delete guard in P0-2, all of P0-6), under the CRM's conventions:
  every write service ends with `saved(...)` and `load_party(...)`, `PARTY_GRAPH` is extended, never bypassed.
- The auth error catalog grows by exactly the four codes above.
- **Never grant** the `skillbot` client `auth:users:manage`, `auth:clients:manage` or `auth:users:login`, in any
  test fixture that models it or in any runbook line.
- **Never edit an accepted ADR's body**; P0-1 changes status lines only.
- Edit `0001_baseline.py` only for the two schema lines.
- Keep the compose service name `worker`.
- No tombstones, no webhooks, no relation feed.
- The code that leaves stays reachable at tag `v0.5.0`; take nothing back from it into SkillForge.

## Follow-ups outside this arc

- **skillbot's rebuild arc:** handover docs; cleanup (grant engine, the role-name fallback, `CustomerResolver`, the
  commented stubs, `tree.on_error`, narrower intents, tutor instead of teacher, `skillforge-client` 0.6 or later);
  its own database (same server, own role without `CONNECT` on SkillForge's database, Alembic, a `migrate` service);
  the pull loop; person tokens; the tutor and student workspaces; guild setup. Until its cleanup, Dependabot bumps of
  `skillforge-client` are ignored - the cleanup release removes `api.bot`.
- **skillbot v0.1.0 runs in prod** (deployed 2026-09-21) with commands that crash; stop it or unsync its commands
  until its cleanup.
- **The legacy `skillbot` schema:** run P7 now - a `RESTRICT` foreign key into `core.party` may block CRM deletes
  today.
- **skillsite's privacy text** before the bot goes live: the bot's copy of CRM data and its lifetime, Discord as a
  login, Discord IDs in `auth_audit_log` and in unlinked rows, session and token retention.
- **The reach arc:** tutor reach with rules per basis and scope, #160, #161, `VOUCHED_SCOPES` for new `:own` writes.
- **SkillForge issues:** `auth_audit_log` retention; the Dockerfile `HEALTHCHECK` on `/health/live`;
  `transaction_timeout` on the app role once Neon's Postgres version is confirmed; #164 learns that the bot's
  savepoint sites leave.
