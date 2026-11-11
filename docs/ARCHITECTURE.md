# Architecture

This file describes the **living structure** of SkillForge - what exists *now* and how it fits
together. What SkillForge is *for*, its borders and principles, is in the [project sketch](PROJECT.md);
the *why* behind larger decisions lives in [`decisions/`](decisions/) (ADRs), forward-looking
design in [`specs/`](specs/).

## What is SkillForge?

The central service of the skill-platform: the **hub for central data, identity, permissions and domain
rules** ([project sketch](PROJECT.md), principle 1). Frontends - SkillBot (the Discord bot) and the portal
(skillsite) - reach it through one channel, the **REST API** (`/api/v1`, OAuth2-protected), and keep their
own state themselves. SkillForge **pushes nothing**: frontends pull what changed
([Change signals](#change-signals), [ADR 0009](decisions/0009-bot-owns-its-discord-workflows.md)). It never
touches the Discord API.

## Layers

```
HTTP -> app/api/system    liveness + health probes (dependencies, workers)
        app/api/v1        endpoints, request/response schemas, scope checks
        app/services/auth identity: accounts, clients and grants, tokens, sessions, Discord links
        app/services/crm  system of record: parties, roles, contact infos, relations, subjects
        app/services/billing  lessons and their life cycle, students' rates, payers, the calendar import
        app/services/system  health aggregation + worker heartbeats
        app/integrations  the only way out to other systems (guarded HTTP transport)
        app/core          cross-cutting: auth, db, logging, config
                          `-> Postgres (schemas: core/geo/ext/auth/system/billing)
```

- **`app/main.py`** - FastAPI entry point. Mounts the top-level `app.api` router (which aggregates
  the `system` and `v1` routers), registers request logging and the exception handlers, exposes
  `GET /` (welcome), and finishes with `customize_openapi(app)`.
- **`app/api/system/`** - `health.py`: `GET /health` (aggregate over all dependencies **and**
  workers; `200` healthy / `503` unhealthy / `500` on error), plus `/health/live`,
  `/health/dependencies[/{name}]`, and `/health/workers[/{name}]`.
- **`app/api/v1/`** - `router.py` with prefix `/api/v1` aggregates three areas, which share the
  vocabulary in `common/` (see [API conventions](#api-conventions)):
  - `auth/` - `token.py` (OAuth2 token endpoint, four grants), `revoke.py` (logout), `password.py`
    (redeem a one-time token), `users.py` (accounts), `discord_links.py` (Discord links, their feed, redeeming
    a link code), `clients.py` (client management), `me.py`, `params.py` (shared parameter aliases).
  - `crm/` - one module per resource: `parties.py` (list, detail, guarded delete), `persons.py` &
    `companies.py` (typed create and update), `roles.py`, `contact_infos.py`, `relations.py`,
    `subjects.py`. `params.py` holds the path and query vocabulary, `schemas.py` the read and write
    models with their `from_model` mappers (see [CRM](#crm)).
  - `billing/` - `lessons.py` (the lesson routes), `rates.py` (a student's rates), `payers.py` (payers, sevDesk
    contact links, manual billing), `calendar.py` (the calendar import, its occurrences and the aliases),
    `params.py`, `schemas.py` (see
    [Billing](#billing)).
- **`app/services/auth/`** - the auth services, same shape as the CRM's: `clients.py`, `scopes.py` (grants
  per mode, `resolve_token_scopes`), `secrets.py` (client secrets), `tokens.py` (the four grants),
  `sessions.py`, `accounts.py`, `users.py`, `action_tokens.py` (invitations, resets and Discord link codes),
  `discord_links.py` (the one writer of `ext.discord_account`), `discord_link_codes.py` (redeeming a link code),
  `housekeeping.py` (deletes expired sessions and one-time tokens), `roles.py` (`derive_roles`), `bootstrap.py`,
  `audit.py`, `results.py`, `errors.py`. Never imports the API or another domain.
- **`app/services/crm/`** - the CRM services (function modules, `session` first, no commits, no
  `app.api` imports): `parties.py` (`PARTY_GRAPH`, `load_party`, `saved`, list, delete), `persons.py`,
  `companies.py`, `roles.py`, `contact_infos.py`, `relations.py`, `subjects.py`, `payers.py` (`resolve_payer`, a
  read for billing), `inputs.py` (enums, input dataclasses and `normalize_contact_value`, shared with the API),
  `errors.py` (the error catalog). Imports no other domain.
- **`app/services/billing/`** - the billing services, in the CRM's shape: `lessons.py`, `rates.py`, `payers.py` (the
  payer of a lesson, the one writer of `ext.sevdesk_contact` and of `billing.manual_payer`), `students.py` (the
  student check), `locks.py` (`hold_party`, the party lock every write takes before a row names a party),
  `calendar_rules.py` (what a calendar occurrence is; pure), `calendar.py` (the import), `calendar_aliases.py`,
  `errors.py` (the error catalog). Imports only `app.core`, `app.integrations`, the CRM services and itself.
- **`app/services/system/`** - health aggregation (`health_service.py`) and worker liveness
  (`heartbeat_service.py`), backing the `/health` tree.
- **`app/core/`** - `auth/` (what validates a request: OAuth2 scheme, JWT, principals, scopes, roles, reach,
  guards; plus the secret, password and e-mail primitives),
  `db/` (async engine, sessions, models), `logging/` (structured logging via `skillcore`), `errors.py`
  (HTTP-agnostic error taxonomy), `config.py` (settings).

## Integrations

`app/integrations/` is the only place SkillForge calls another system; its rules for agents are in
[`app/integrations/AGENTS.md`](../app/integrations/AGENTS.md). Services call it synchronously: write a pending
row, commit, call, record the outcome - there is no job queue.

- **One guarded transport.** `IntegrationHttp` in `transport.py` is the only module in `app/` that reaches for the
  network. Every request passes `_GuardedTransport`, which refuses a URL outside the base URL and the host allowlist
  (https, default port, no userinfo) and, under pytest, any real network unless the contract run targets a `trial`
  account (`guards.py`). Tests use `httpx.MockTransport`; a socket guard in `tests/conftest.py` backs that up.
  No retry (`NETWORK_RETRIES`), no redirect followed.
- **Modes** (`IntegrationMode` in `config.py`): `dry_run`, the default, sends reads and returns a write as the
  `PlannedRequest` it would have sent; `live` sends both. Under pytest `live` is refused except in the contract
  run (`SKILLFORGE_CONTRACT_RUN=1`) against a `trial` account (`IntegrationAccount`).
- **No retry.** A failed read is `IntegrationUnavailableError` and may be repeated by its caller; a write that may
  have reached the remote system is `OutcomeUnknownError` and is never repeated - the service reconciles it. A
  4xx is `IntegrationRejectedError`; an error carries the response's `Retry-After` for a caller that repeats a
  read. These errors (`errors.py`) are internal, no `DomainError`: the service decides what an API client sees.
- **Nothing leaks.** The credential is passed as `credentials`, redacted in every `Exchange`; errors never chain
  the httpx exception, which can carry the request and a header value. One `integration_request` log line per call
  names the integration, method, path template, status and duration - never a header, a query or a body; httpx's
  own request log is silenced. A log line or an error message names a path as its template (`path_template`:
  `/Contact/{id}`), never an object's id.
- **Layering.** `app/integrations/` imports only `app.core` and itself, and `app/core/` imports no integration
  (`tests/integrations/test_integrations_architecture.py`) - with one exception: `app/core/config.py` imports
  `app.integrations.sevdesk.config` and `app.integrations.graph.config`, so that `Settings` nests
  `SevdeskSettings` and `GraphSettings`.
- **sevDesk** (`sevdesk/`): settings `SEVDESK__*` (`SevdeskSettings`, all optional, an empty id means unset, an
  empty token is refused; account-specific ids are configuration). A `SevdeskClient` comes from `connect`, which
  first confirms the account (`verify_account` in `identity.py`): the `SevClient` the token belongs to must have
  `SEVDESK__EXPECTED_ACCOUNT_ID` and, for a `trial` account, a company name containing `TRIAL` - anything else, a
  failed read included, is an `IdentityCheckError`, and an unconfirmed client refuses every other call. A listing
  is read whole or refused (`countAll` total, no repeated id, at most `MAX_PAGES`). The models (`models.py`) parse
  sevDesk's string-typed numbers and booleans into strict types, hold amounts in whole cents and read no field
  SkillForge does not use; `invoices.py` builds the `saveInvoice` body of a draft under § 19 UStG (`taxRule` 11)
  with its delivery date or period, from integer cents and minutes sent as exact decimal strings - never a float;
  a position keeps the quarter-hour rule (minutes in steps of 15, cents per hour divisible by 4).
- **Microsoft Graph** (`graph/`): the read of the one lesson calendar. Settings `GRAPH__*` (`GraphSettings`, all
  optional; the certificate and its key are PEM texts base64-encoded into one line, as secrets). The app token
  (`credential.py`) is a client-credentials grant with a client assertion signed PS256 by `pyjwt[crypto]`, sent
  through the guarded transport as a read (`read_form`) and cached in memory until shortly before it expires. A
  `GraphClient` comes from `connect`, which confirms the calendar first (`verify_calendar` in `identity.py`): the
  calendar named `GRAPH__CALENDAR_NAME` among the mailbox's calendars must exist once and be owned by
  `GRAPH__MAILBOX` - the app has no directory permission to read the mailbox itself; anything else, a failed read
  included, is an `IdentityCheckError`. `calendar_view` reads `calendarView` in UTC with only the fields of
  fixture G1 (`EVENT_FIELDS`), following `@odata.nextLink` on the Graph host, and refuses a listing longer than
  `MAX_PAGES` instead of cutting it short; the client repeats a `429` as its `Retry-After` says, bounded
  (`MAX_ATTEMPTS`, `MAX_RETRY_AFTER_SECONDS`), and nothing else. One read - a `connect` block with its token
  request, identity check and pages - ends within `READ_DEADLINE_SECONDS` (120 s): no send starts and no wait is
  taken without time left, a send still running is cut, and the read ends as `IntegrationUnavailableError` (inside
  the identity check, as `IdentityCheckError`). Graph has no trial:
  the client treats its tenant as a `live` account, which no test run reaches. Log lines and errors name paths by
  their templates (`CALENDAR_VIEW_TEMPLATE`), never the mailbox or a calendar id.
- **Tooling** for the maintainer, all calling sevDesk and none in CI: `just sevdesk-doctor trial|live`
  ([`app/cli/sevdesk_doctor.py`](../app/cli/sevdesk_doctor.py)) checks the configured ids read-only and, with
  `--discover` - the one read before the account is confirmed - prints the `sevClient` ids that `GET /SevClient`,
  `GET /SevUser` and `GET /ContactCustomFieldSetting` report and whether the company name carries the trial marker;
  `just contract-sevdesk` runs the `contract` tests against the trial, skipping them once the trial expired;
  `just sevdesk-spike` ([`scripts/sevdesk_spike.py`](../scripts/sevdesk_spike.py), not in the image) walks the
  trial step by step. The trial recipes load the trial's env file and refuse (`scripts/sevdesk_trial_env.sh`) a
  shell that has `SEVDESK__*` variables of its own, in any letter case, and an env file inside the repository or
  any other git work tree (symlinks resolved) or with whitespace in its path; `.gitignore` keeps out any `*.env`
  file. The spike's exchanges go through the `Recorder` (`recording.py`), which scrubs them into fixtures under
  `tests/fixtures/sevdesk/<run id>/` once the identity check passed.

## Housekeeping worker

A dedicated worker ([`app/workers/housekeeping.py`](../app/workers/housekeeping.py), the `worker`
service in `compose.yml`) runs every pass in `PASSES` every `HOUSEKEEPING_INTERVAL` (30 s), each in
its own transaction:

- **Expired sessions** and **expired one-time tokens** - `delete_expired_sessions` and
  `delete_expired_action_tokens` in
  [`app/services/auth/housekeeping.py`](../app/services/auth/housekeeping.py) delete rows whose
  `expires_at` lies more than `RETENTION_AFTER_EXPIRY` (30 days) in the past - live, revoked,
  rotated, used or invalidated alike. Each pass deletes one `SKIP LOCKED` batch of at most
  `DELETE_BATCH_LIMIT` rows per cycle, oldest first, and writes no audit row: the history stays in
  `auth_audit_log`.

Every cycle logs exactly one `housekeeping_cycle` line (`sessions_deleted`, `action_tokens_deleted`,
`duration_ms`), then beats for `/health` under `WorkerName.HOUSEKEEPING`, which
`GET /health/workers` reads; a failing pass marks the beat `DEGRADED` and leaves the other pass
alone. Design details: [`bot-decoupling.md`](specs/bot-decoupling.md#housekeeping-worker).

## CRM

The CRM is the **system of record** for who exists and how people relate
([ADR 0007](decisions/0007-crm-system-of-record.md)): `core` holds the intended state; what is true in
Discord is the bot's to track ([ADR 0009](decisions/0009-bot-owns-its-discord-workflows.md)). The CRM
imports nothing but `app.core`, the shared API vocabulary and itself, and a CRM write is validated against
CRM rules only, never against Discord state. The full design is in the [CRM API spec](specs/crm-api.md).

- **Route form.** A polymorphic read side (`GET /parties`, `GET /parties/{party_id}`, whose
  `PartyDetail` is a discriminated union of `PersonDetail` and `CompanyDetail`) and a typed write
  side (`/persons`, `/companies`) over one ID space. Roles are idempotent singletons
  (`PUT` / `DELETE /persons/{party_id}/student|tutor`), contact infos owned children with their own
  ID, relations associations addressed by their natural key
  (`/parties/{party_id}/relations/{type}/{to_party_id}`). Every route has exactly one target party.
- **Aggregate root.** `Party` is the root: every write inside the aggregate ends with `saved(...)`, which moves
  `party.updated_at` - the one change signal consumers get ([Change signals](#change-signals)) - and a relation
  touches both parties. A request that changes nothing (an empty `PATCH`, a repeated `PUT`) is not a write. Taking a
  role away takes the `tutor_of` it anchored along (`remove_tutor_role` and `remove_student_role` in `roles.py`);
  every write of a `tutor_of` - its `PUT` and `DELETE` and a role removal - first locks both parties in ID order, so
  no `tutor_of` slips in beside a removal and no two of these writes deadlock - but for two rare races, documented
  in `_lock_pair` and `_lock_with_tutor_of`.
- **One loading path.** Async SQLAlchemy cannot lazy-load, so `PARTY_GRAPH` in `parties.py` names
  everything a representation may touch, `load_party` applies it with `populate_existing`, and every
  write returns through it. A `from_model` mapper touches only what `PARTY_GRAPH` loads.
- **Frontends pull.** The CRM knows none of its consumers. The bot, the portal and operator tools read it
  through its routes, learn what changed from `updated_at` ([Change signals](#change-signals)) and bring
  their own state in line; no CRM write waits for them or asks them first.
- **Uniqueness by constraint.** Subject titles (`uq_subject_title_lower`) and contact infos
  (`uq_contact_info`) are decided by the database: the change is made and flushed *inside* a
  SAVEPOINT and an `IntegrityError` becomes the domain error (`_unique_title`,
  `_unique_per_party`). `begin_nested()` flushes pending state first, so a change made before it
  would fail in the enclosing transaction.
- **Errors.** A missing thing named in the path is a 404, one referenced in the body a 422; what
  needs no database is checked by the request model. The catalog is closed at 15 classes in
  `app/services/crm/errors.py`, each pinned per route by `tests/api/test_crm_error_contract.py`.
- **Personal data stays out of logs.** Validation and domain messages never repeat a name or a
  contact value, text Postgres cannot store is rejected by the request models
  (`require_storable_text`), and the engine runs with `hide_parameters=True`.
- **Deleting a party** is refused (`party_in_use`) while a row of `EXTERNAL_LINKS` names it: an active
  Discord link, another `ext` link, or a billing row: a lesson or a rate, a lesson it pays for, its mark as billed
  by hand, or a calendar alias naming it - a deactivated Discord link goes with the party. The `ext` foreign keys
  into `core.party` cascade, so the guard - taken under a row lock - is what keeps external systems from being
  orphaned; the billing ones are `RESTRICT`, and the guard answers 409 before the database refuses. The CRM reads
  these tables there and nowhere else.
- **Commit before the response.** The CRM routes use `DBSession`, whose `scope="function"` ends the
  request transaction before the response is sent: a failing commit is a 500, not a 201.

## Billing

Lessons are central data: SkillForge knows which lessons took place, for whom and how long. The full design is in the
[billing spec](specs/billing.md); the routes live under `/api/v1/billing` (tag `billing`), reads need `billing:read`,
writes `billing:write`.

- **A lesson's life.** A lesson starts `planned`; the tick (`PATCH /billing/lessons/{lesson_id}`, `status: held`)
  makes it billable. Entering a status sets its default `billable` - only `held` is billable by itself, a
  `cancelled` one only after the maintainer switches it on - and a `billable` sent along wins; `billable` on a
  `planned` or `not_held` lesson is `lesson_not_billable` (`billable_after` in `lessons.py`), so
  `ck_lesson_billable_status` never answers. A `PATCH` locks the lesson first.
- **No delete route.** "Remove" is `not_held`; a lesson keeps its history.
- **Times** are stored in UTC and filtered by days in `Europe/Berlin` (`BILLING_TIME_ZONE`, `berlin_midnight`):
  `from` and `to` of `GET /billing/lessons` are Berlin days, both included.
- **Uniqueness by constraint.** One lesson per student and start (`uq_lesson_student_start`), translated to
  `lesson_already_exists` inside a SAVEPOINT (`_unique_start`), as the CRM does.
- **Rates.** A student's own rate holds from its day (`valid_from`) until the next one's
  (`/billing/students/{party_id}/rates`). `PUT` on a day is an upsert in one statement (`put_rate` in `rates.py`):
  the same amount and note write nothing, another amount replaces it. Rule G lives in `require_quarter_hour_rate`,
  for a student's rate and a lesson's own alike, so `rate_not_quarter_hour` answers before the database's CHECK.
  Listing and removing need only the party, so a former student's rates stay readable and removable; setting one
  needs the student role.
- **Payers.** Who pays is the CRM's answer (`resolve_payer` in `app/services/crm/payers.py`): the one party with
  `pays_for` to the student - a company included - or the student itself; several are ambiguous. A lesson stores
  that payer when it becomes billable (`_resolves_payer` in `lessons.py`, `lesson_payer` in `payers.py`), and again
  when a billable lesson changes its student or its Berlin day; it keeps it through every other edit, so a later
  change of `pays_for` leaves it as it was until `POST /billing/lessons/{lesson_id}/resolve-payer`. A lesson that is
  not billable has no payer, and neither has one ticked while its student had several - the tick is never refused.
  The resolved payer is held (`hold_party`) before the lesson names it; one deleted meanwhile took its `pays_for`
  along, and the CRM is asked again.
  `GET /billing/students/{party_id}/payer` answers the payer of today, whether it has a sevDesk contact and whether
  it is billed by hand.
- **sevDesk contacts and manual billing.** `PUT`/`DELETE /billing/parties/{party_id}/sevdesk-contact` and
  `.../manual-billing`, idempotent. `payers.py` is the one writer of `ext.sevdesk_contact` (architecture test): a
  contact id is stored as given, nothing is looked up in sevDesk, and one `INSERT ... ON CONFLICT DO NOTHING`
  decides a link, so a contact linked elsewhere is `sevdesk_contact_already_linked` also for racing requests. A
  contact id is accepted only in its canonical form (a positive integer without leading zeros), so one contact
  cannot be linked twice under two spellings.
- **Party locks.** Every billing write holds each party it names - the student, the payer, the party of a link or
  a mark - with `hold_party` (`locks.py`, `FOR KEY SHARE`) before it writes, so a concurrent party delete either
  makes it wait and find no party (a domain error) or waits for it and answers `party_in_use`, never a 500. The
  lock blocks nothing else: neither another billing write nor any other CRM write on the party.
- **Reading the CRM.** Whether a party is a student is read through the CRM's `load_party` (`require_student` in
  `students.py`): a party without the student role is `unknown_student`; a party in a path that does not exist is the
  CRM's `party_not_found`, one named in a body `unknown_student`.
- **Calendar rules.** `calendar_rules.py` decides what an occurrence of the lesson calendar is - a `planned` or
  `cancelled` lesson of one student, or no lesson with its reason - by the spec's table, first match wins
  (`propose`). It is pure: it takes plain data (`Occurrence`, `Roster`), imports only an allowlist of the standard
  library and reads no clock (architecture test in `tests/test_billing_calendar_rules.py`), and holds the match
  tokens (`mit`, `Kennenlernen`, `Telefon`, `Abgesagt`). Durations are measured between instants, so a lesson across
  a clock change keeps its minutes; one longer than a day (`MAX_LESSON_DURATION`, the bound of the lesson routes) is
  `duration`. A location is a phone number when, stripped, it is an optional leading `+` followed only by digits,
  spaces and `/ ( ) -`, with 5 to 15 digits (`is_phone_number`); letters - a room, an address, Teams - never make
  one. A name matches through an alias first - only one that names a student of the roster - then exactly, then as
  the one student whose name tokens contain all of its at least two tokens (`match_name`), after `normalize_name`;
  several candidates are ambiguous, and a name that normalises to nothing matches nobody. It logs nothing and keeps
  titles out of `repr`.
- **Calendar aliases.** `GET`/`POST /billing/calendar/aliases`, `DELETE .../{alias_id}` (`calendar_aliases.py`):
  `POST` takes the name as written and stores it normalised, so every spelling that normalises alike is
  `calendar_alias_already_exists` - one `INSERT ... ON CONFLICT DO NOTHING` decides, racing requests included. A
  name with nothing left once normalised is a `422`; the student is held `FOR KEY SHARE` first
  (`require_named_student`).
- **Calendar import.** `POST /billing/calendar/import` (`import_calendar` in `calendar.py`) reads a range of at most
  92 Berlin days (`MAX_IMPORT_DAYS`, a bound of the import, not of the spec) through the Graph client and answers with
  counts only. The read comes first, outside any lock and before any statement: every integration error of it is
  logged by its class and status and becomes `calendar_unavailable` (`502`), with no row written - the endpoint names
  no integration error (architecture test). Then a transaction-level advisory lock serializes the import with assign
  and dismiss, so overlapping imports wait for each other. Each occurrence is upserted by Graph's `id` into
  `billing.calendar_event`, a snapshot of start, end, cancellation and subject; one without a lesson gets
  `propose`'s result - a lesson linked by `lesson.calendar_event_id`, or its reason. When in doubt the import flags
  `changed` and changes nothing: an occurrence with a lesson is judged by `propose` again at every import, and only
  while it still gives this student and this subject does an untouched lesson - `planned`, no note, no own rate, at
  the start and length of its occurrence as last seen - follow a change (or go with its vanished occurrence). Any
  other difference is flagged, a vanishing once (`vanished`); a dismiss or an assignment acknowledges the occurrence
  as last seen until it changes again. An occurrence that could not get its lesson - its student has one at that
  start - stays flagged and is not proposed again; a student who lost the role is `unmatched`. A title with a NUL
  character is read as no title. The roster is read through the CRM's `list_parties`.
  `GET /billing/calendar/events` lists the occurrences that wait or are flagged; `.../{event_id}/assign` creates the
  lesson (optionally storing an alias) and `.../{event_id}/dismiss` takes an occurrence off the list. PD13:
  `title_name` is kept only for the reasons that wait for an assignment (`ck_calendar_event_title_name`) and is
  cleared on assign and dismiss; occurrences without a lesson are deleted 90 Berlin days after their start, with the
  next import. No log line holds a title or a name.
- **Demo data.** `just seed-demo` ([`app/cli/seed_demo.py`](../app/cli/seed_demo.py)) writes a synthetic family
  with two siblings at different rates, a student who pays for themselves, and lessons in every status, through the
  CRM and billing services. The lessons fall in the current Berlin month and the one before: a second run in the
  same month changes no row, a run in a later month adds that month's lessons. It checks the configured database URL
  before it connects (`local_host_class`): only `postgresql+asyncpg` without query parameters, and only `localhost`,
  a loopback address or a single-label host name (a compose service) pass.
- **Backfill.** `just billing-backfill <file> [--apply]`
  ([`app/cli/billing_backfill.py`](../app/cli/billing_backfill.py)) is the maintainer's data step: it applies a mapping
  CSV - `student_party_id`, `payer_party_id`, `sevdesk_contact_id`, `manual_billing`, `rate_cents_per_hour`,
  `rate_valid_from`; the module docstring holds the format - through `put_relation`, `link_sevdesk_contact`,
  `put_manual_payer` and `put_rate`. Parties are named by id, never by name; a contact id passes the API's
  `SevdeskContactId`, so it is canonical. It has no host guard - it is meant for production - so the safety is
  elsewhere: the file must lie outside every git work tree, symlinks resolved, and is refused before it is read or
  anything connects (`check_path`); every row is checked against the file and the database before the first write
  (`plan_mapping`), and every problem is reported by line and reason code; without `--apply` the check reads one
  snapshot (`REPEATABLE READ`) in a transaction that is rolled back; with it, the whole file is checked and written in
  one `SERIALIZABLE` transaction or nothing (`Database.session(isolation_level=...)`). An API write committed between
  check and write that collides with what the apply writes - a party row both `saved`, a key its insert meets, a payer
  deleted under its foreign key - fails the run ("run it again"), caught by the snapshot's write conflicts; one that
  collides with nothing ends as if it had come right after the run, and nothing is overwritten. `SERIALIZABLE` adds the
  protection against a second backfill at the same time. It only adds - a row that contradicts the database is a
  problem, never an update - so a second run changes no row. The output holds counts, line numbers and reason codes,
  never an id, a cell, the path or the database URL - a failure prints its error code or class, never its message. The
  payer already stored on a lesson (PD1) is not touched.
- **Errors.** The catalog is closed (the spec's "Error catalog"); its classes live in `app/services/billing/errors.py`
  and are pinned per route by `tests/api/test_billing_error_contract.py`.

## Database

One Postgres DB, six schemas by domain - details in
[`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md):

| Schema | Contents |
|---|---|
| `core` | Central business domain: party/person/company, students, tutors, subjects |
| `geo`  | Geographic reference data (PLZ/Ort) |
| `ext`  | Links from external system ids (Discord, sevDesk, Clockodo, Microsoft) to a `core.party` |
| `auth` | OAuth2 clients, secrets, scope grants, user accounts, roles, sessions, one-time tokens, audit |
| `system` | Runtime/operational state: background-worker liveness heartbeats |
| `billing` | Lessons, students' rates, payers billed by hand, calendar aliases and occurrences ([billing spec](specs/billing.md)) |

- **Async SQLAlchemy 2** over `asyncpg`; models under `app/core/db/models/<schema>/`, one
  `*Base` class per schema with `{"schema": ...}`.
- **Migrations** via Alembic (`migrations/`). The app uses the pooled connection, migrations the
  direct one - see [ADR 0002](decisions/0002-pooled-vs-migration-url.md). Schema/baseline
  convention: [ADR 0005](decisions/0005-multi-schema-db.md), amended by
  [ADR 0009](decisions/0009-bot-owns-its-discord-workflows.md): a schema only history knows (`bot`) is created
  and dropped by revisions, not by `migrations/env.py`.

## Auth

SkillForge is its own identity provider ([ADR 0008](decisions/0008-user-authentication-and-reach.md),
[spec](specs/user-authentication.md)). What validates a request is in `app/core/auth/`, the services are in
`app/services/auth/`, the data is in the `auth` schema.
Every grant at `POST /api/v1/auth/token` authenticates the client (`client_id` + Argon2-hashed secret):

| Grant | For | Result |
|---|---|---|
| `client_credentials` | the client itself | access token from its `application` grants |
| `password` | a person, through a login client | access + refresh token; opens a `user_session` |
| `refresh_token` | the same person, same client | new access token; the refresh token rotates |
| `urn:skillforge:params:oauth:grant-type:discord-user` | a person a client vouches for | access token only, no session |

The password and refresh grants need `auth:users:login` in `application` mode, the Discord-user exchange
`auth:users:exchange` - and no client holds both (`grant_client_scopes`). Their services (`issue_user_token`,
`refresh_user_token`, `exchange_discord_user` in `app/services/auth/tokens.py`) *return* a `TokenDenial`
instead of raising, so the failed-login counter, a revoked session and the audit entry commit; `create_token`
turns it into the OAuth2 error. `POST /auth/revoke` logs out. The exchange follows an active Discord link to
the party, its account and `active`; every break is the same `invalid_grant`, and the login lock is neither
read nor written.

- **Tokens:** a stateless JWT for an `ApplicationPrincipal` or a `UserPrincipal` (`principal.py`), claims
  declared once in `tokens.py`; a person's token names the client (`azp`), the party and how it was obtained:
  `UserPrincipal.login` is a `PasswordLogin` (`amr: ["pwd"]`, its session as `sid`) or a `DiscordLogin`
  (`amr: ["discord"]`, no `sid`).
- **Scopes** are computed at issuance (`resolve_token_scopes`): a grant is `application` (for the client)
  or `delegated` (the ceiling for people); a person gets `delegated ∩ role scopes` (on refresh also
  `∩` the session's scope, on the exchange also `∩ VOUCHED_SCOPES`). Purposes live on `Scope`;
  `CLIENT_ONLY_SCOPES` are application-only. A token obtained without a password carries only `VOUCHED_SCOPES`
  (`crm:read`, `crm:read:own`, `crm:write`), checked at issuance and at validation: `account:self`, every
  `auth:*` and every `billing:*` scope need a password login.
- **Roles** (`roles.py`) only produce scopes: `BASE_USER_SCOPES` for everyone, `ROLE_SCOPES` per role;
  `admin` is stored, the others are derived from the CRM.
- **Reach:** `x:own` limits `x` to the caller's reach (`reach.py`); `require_access(...)` hands a route
  an `Access`, `require_scopes(...)` demands the unqualified scope.
- **Sessions** (`app/services/auth/sessions.py`): the opaque refresh token is stored as a digest, rotates on every
  refresh and expires after `refresh_token_expire_days`; a rotated-out token ends the session unless it
  arrives within `REFRESH_REUSE_GRACE`. Wrong passwords lock the login per account
  (`login_lockout_threshold`, `login_lockout_max_minutes`). Argon2 runs off the event loop.
- **Discord links** (`app/services/auth/discord_links.py`, `/api/v1/auth/discord-links`): which Discord account
  speaks for which person party. The one writer of `ext.discord_account`; admins write (or the person, with a
  one-time link code), the bot reads the feed.
  A person links their own Discord account with a one-time link code: an admin issues it
  (`POST /api/v1/auth/users/{user_id}/discord-link-code`), the bot redeems it for the Discord user who typed it
  (`POST /api/v1/auth/discord-links/redeem`, `auth:users:exchange`, `discord_link_codes.py`). The request log
  redacts the Discord user ID in these paths (`REDACTED_PATH_SEGMENTS` in `app/core/logging/middleware.py`).
- **Never** in a log or an audit row: a password, a refresh or action token, an e-mail address. A Discord user ID
  appears in audit rows only - never in the request log, a token or an error.

`just bootstrap-client` and `just bootstrap-admin` ([`app/cli/bootstrap.py`](../app/cli/bootstrap.py)) seed the
first clients - skillbot's too, once the bot needs one - and the first admin.

## API contract

The committed **`openapi.json` is the contract**; consumers generate their clients from it.
`just openapi` regenerates it, `just openapi-check` (in CI) prevents drift. Never edit it by hand -
see [ADR 0001](decisions/0001-openapi-as-contract.md).

## API conventions

All `/api/v1` domains share one vocabulary (`app/api/v1/common/`), so an endpoint states only what
is special about it and both runtime behavior and the OpenAPI docs derive from the same
declaration. The full rules, with the *why*, are in the
[API conventions spec](specs/api-conventions.md) and
[ADR 0006](decisions/0006-error-envelope.md).

- **Operation IDs** are `{tag}_{function_name}` (`operation_id` in `openapi.py`); every route needs
  exactly one domain tag, set on its domain router.
- **Scope guards** are declared, never implemented per route: `dependencies=[require_scopes(...)]`
  on the decorator, or a parameter typed `Annotated[Principal, require_scopes(...)]` when the
  principal is needed. `customize_openapi` derives the `401`/`403` docs from the declaration.
- **Errors**: every non-2xx body is `ErrorResponse{detail, code, errors?}`. Services raise
  subclasses of the taxonomy in `app/core/errors.py` (`NotFoundError`, `ConflictError`,
  `DomainValidationError`, `UpstreamError` - another system failed, `502`); `STATUS_BY_ERROR` in
  `errors.py` maps them, the global handlers render them, and `responses=error_responses(...)`
  documents them - endpoints contain no `try/except` for mapped errors. Errors the API layer owns itself (OAuth2 codes, a local status mapping) are
  `ApiError` declarations. An endpoint whose transaction must commit although the request failed
  *returns* the error (`ApiError.response()`) instead of raising it - the token endpoint does, to
  keep its `TOKEN_DENIED` audit entry.
- **Lists** take a `PageParams` subclass (`limit`, `offset`, filters; unknown parameters are a
  `422`) and return `Page[Item]` - never a bare array.
- **Schemas** derive from `ApiModel`: a docstring under a field becomes its OpenAPI description.

## Release and deploy

One flow for the whole platform ([spec](specs/release-flow.md)): release-please maintains a release PR;
shipping it creates the tag and GitHub release, and the `Release` workflow builds the image, publishes
the Python client and calls `deploy.yml`, which hands over to the platform's shared deploy workflow
([`skill-platform-workflows`](https://github.com/Nachhilfe-Leon-Weimann/skill-platform-workflows), the same
for every repo). Its script triggers `compose.deploy` over the Dokploy API, waits for the deployment to
finish and then requires `GET /health` to answer `ok` with the released `version` - a failed migration or
a stale container is a red workflow. Dokploy runs the compose it stores, so the deploy first stores the
repo's [`compose.yml`](../compose.yml) of the released commit there (`compose.update`); its `image:` tags the
release commit pins to `vX.Y.Z`: `main` records what prod runs. There is no automatic rollback - an app
rollback would not roll back an Alembic migration; the manual procedure is in the
[README](../README.md#rolling-back).

## Change signals

SkillForge pushes nothing ([ADR 0009](decisions/0009-bot-owns-its-discord-workflows.md)): a frontend asks what
changed and brings its own state in line. The feeds are the party list (`GET /api/v1/crm/parties`, ordered by name)
and the Discord link feed (`GET /api/v1/auth/discord-links`, which returns unlinked rows too, ordered by Discord
user ID); every item of both carries `updated_at`. Every `updated_since` parameter is the shared `UpdatedSince` of
`app/api/v1/common/changes.py`, which points here.

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

### The party feed

A party's `updated_at` is the stamp of its whole aggregate ([CRM](#crm)): a CRM write ends with `saved(...)` for
every party it changed, and a request that changes nothing is no write. A list item's `updated_at`
(`PartyListItem`, also the `party` of a relation) is the detail's. A `tutor_of` stands on both roles: taking the
tutor role away removes the person's outgoing `tutor_of`, taking the student role away the incoming ones, and the
parties on their other side move too - so every `tutor_of` a consumer reads is a valid pair. `WRITES` in
`tests/db/crm/test_crm_write_services.py` pins the CRM rows of the table below, a related bystander included.

### What moves what

| Write | Route | Moves `updated_at` of |
|---|---|---|
| Create | `POST /crm/persons`, `/companies` | the new party |
| Names | `PATCH /crm/persons/{id}`, `/companies/{id}` | the party, whenever a field is sent (an empty body: nothing) |
| Give or change a role | `PUT /crm/persons/{id}/student`, `/tutor` | the person, on a real change |
| Take a role away | `DELETE /crm/persons/{id}/student`, `/tutor` | the person and every other side of the `TUTOR_OF` it removed |
| Contact infos | `POST` / `PATCH` / `DELETE /crm/parties/{id}/contact-infos...` | the party |
| Relate / unrelate | `PUT` / `DELETE /crm/parties/{id}/relations/{type}/{to}` | both parties (a repeated `PUT`: nothing) |
| Delete a party | `DELETE /crm/parties/{id}` | every party related to it |
| Subjects | `/crm/subjects...` | no party |
| Discord links | `/auth/discord-links...` | the link row only (the link feed) |
| Accounts, admin role, disabling | `/auth/users...` | nothing |

## Roadmap

The roadmap lives in the [project sketch](PROJECT.md#roadmap). The capability arcs this section used to list -
guardian, ops plane, eventing, integration sync - rested on the bot's job and operation substrate, which
[ADR 0009](decisions/0009-bot-owns-its-discord-workflows.md) retires: frontends pull, SkillForge pushes nothing.

## Where do I find...?

| Question | Location |
|---|---|
| How is the DB structured? | [`DATABASE_SCHEMA.md`](DATABASE_SCHEMA.md) |
| Why was X decided this way? | [`decisions/`](decisions/) |
| What is planned / design sketches? | [`specs/`](specs/) |
| Which commands exist? | [`../justfile`](../justfile), [`../AGENTS.md`](../AGENTS.md) |
| What does the API look like? | [`../openapi.json`](../openapi.json) |
| How do people get into the system? | [`specs/crm-api.md`](specs/crm-api.md), [CRM](#crm) |
