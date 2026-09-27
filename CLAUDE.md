# CLAUDE.md

Anchor for AI assistants and quick onboarding. Captures commands, layout, and conventions;
the deeper *why* lives in [`docs/`](docs/).

## Commands

Everything runs through [`just`](justfile) (which wraps `uv`):

- `just dev` - API with auto-reload (`http://localhost:8000`).
- `just check` - lint + format check + typecheck + `openapi-check` + tests without DB. **Keep
  green before every commit.** `just check-all` also includes DB tests.
- `just test`, `just test-db` (only `@pytest.mark.db`), `just test-without-db`,
  `just test-one <name>`, `just test-file <path>`.
- `just openapi` - regenerate `openapi.json`; `just openapi-check` checks for drift (CI).
- `just bootstrap-skillbot` - seed the initial auth state.
- `just bootstrap-client <client_id> --application "<scopes>" --delegated "<scopes>"` - seed an application
  client with grants in both modes.
- `just bootstrap-admin --party-id <uuid> --email <address>` - ensure an enabled admin account for a person party
  and print its invitation (or, once it has a password, a reset token): the break-glass command.

DB tests provision an ephemeral Postgres via testcontainers (needs Docker; skipped without it).
Set `TEST_DB__URL` to run them against an existing database. Running the API locally needs a
Postgres reachable via `DB__URL`.

## Layout

```
app/
  main.py            FastAPI entry point (root route + app wiring)
  api/system/        health.py (/health + /health/live, /health/dependencies[/{name}], /health/workers[/{name}])
  api/v1/            endpoints: auth/ (token, revoke, clients, me, users, password, discord_links, params),
                     bot/ (runtime, jobs, operations, command_envs, students, tutors, users, authz), crm/ (parties,
                     persons, companies, roles, contact_infos, relations, subjects; params + schemas); common/
                     (shared API vocabulary: error envelope + handlers, error_responses, Page/PageParams, OpenAPI
                     hooks; re-exports DBSession)
  services/auth/     accounts, users, clients, scopes (grants per mode), tokens (the grants), sessions,
                     action_tokens, discord_links, secrets, roles, bootstrap, audit, results, errors
  services/bot/      business logic: transitions, operations, jobs, principals, provisioning,
                     authz, command_envs, contexts, profile, reaper, views, errors
  services/crm/      system of record: parties (PARTY_GRAPH, load_party, saved), persons, companies,
                     roles, contact_infos, relations, subjects, inputs, errors
  services/system/   health aggregation + worker heartbeats (backs /health)
  workers/           reaper.py (lifecycle guardian: job reaper + operation sweeper)
  cli/               deadletters.py (dead-letter list/requeue operator commands), bootstrap.py (behind the
                     `just bootstrap-*` recipes)
  core/              auth/ (OAuth2 scheme, JWT, principals, scopes, roles, reach, guards), db/ (engine, DBSession,
                     models/<schema>/), logging/, config.py, errors.py (HTTP-agnostic error taxonomy),
                     unset.py (the services' UNSET sentinel)
migrations/          Alembic (env.py creates schemas; baseline = explicit DDL)
tests/               api/, auth/, db/ (db/crm/, db/auth/: the CRM and auth apps against Postgres), workers/
                     (DB tests via @pytest.mark.db)
.github/             workflows: ci.yml, build.yml, release.yml (release-please -> build -> publish -> deploy),
                     deploy.yml and triage.yml - both only call the platform's shared workflows in
                     skill-platform-workflows (`@v1`): the Dokploy deploy with its script, and the project
                     intake (issues/PRs -> org project + Module + author as assignee; closed issue/PR ->
                     current iteration; issue closed without a type -> comment; module = repo variable
                     PROJECT_MODULE). Change their behavior there, not here.
scripts/             coverage_summary.py, dump_openapi.py
```

DB schemas: `core`, `geo`, `ext`, `bot`, `auth`, `system` - see
[`docs/DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md).

## Conventions

- **Write everything in English** - docs, comments, specs, configs. The codebase is not German.
- **Reference code by symbol, not line number** - in Markdown docs, link to the file and name the
  function, class, or constant (e.g. `MAX_PAGE_LIMIT` in `pagination.py`), never a bare
  `file.py:<line>` anchor. Line anchors rot on the next edit.
- **`openapi.json` is generated** - never edit it by hand. After API changes, run `just openapi`
  and commit ([ADR 0001](docs/decisions/0001-openapi-as-contract.md)).
- **Migrations** use the direct DB URL (`DB__MIGRATION_URL`), the app uses the pooled one
  ([ADR 0002](docs/decisions/0002-pooled-vs-migration-url.md)). Drop enum types explicitly on
  downgrade; schemas are created in `migrations/env.py`.
- **Endpoints follow the API conventions** ([spec](docs/specs/api-conventions.md),
  [ADR 0006](docs/decisions/0006-error-envelope.md)): guard with `require_scopes(...)` on the
  decorator; services raise taxonomy errors (`app/core/errors.py`) and endpoints neither catch them
  nor raise `HTTPException` - declare them with `responses=error_responses(...)`; lists take a
  `PageParams` subclass and return `Page[T]`; new schemas derive from `ApiModel`. Never hand-write
  401/403 docs or an `operation_id`. A route that serves people restricted to their own data takes an
  `Access` from `require_access(...)` instead and filters by it - never by hand
  ([ADR 0008](docs/decisions/0008-user-authentication-and-reach.md)).
- **The CRM is the system of record** ([spec](docs/specs/crm-api.md),
  [ADR 0007](docs/decisions/0007-crm-system-of-record.md)): nothing under `app/services/crm` or
  `app/api/v1/crm` imports the bot domain, and no CRM write looks at Discord state. Every write
  service ends with `saved(...)` and `load_party(...)`; a `from_model` mapper touches only what
  `PARTY_GRAPH` loads - extend the graph, never add an ad-hoc load. The error catalog is closed.
- **Frontends pull; SkillForge pushes nothing** ([ADR 0009](docs/decisions/0009-bot-owns-its-discord-workflows.md)):
  a feed item carries `updated_at`, consumers ask with `updated_since` minus an overlap and compare in full now and
  then. SkillForge keeps no frontend's state - the bot runs its Discord workflows in its own database. Never add a
  route, table or job that exists for one frontend's workflow.
- **Spec-first** for larger arcs: first a document in [`docs/specs/`](docs/specs/)
  (problem/goals/non-goals/decision table), then implement. Reference:
  [`user-authentication.md`](docs/specs/user-authentication.md).
- **Decisions** with lasting impact go into [`docs/decisions/`](docs/decisions/) as an ADR.
- **History on `main`: one PR per slice** ([spec](docs/specs/release-flow.md), decision D). A slice is one
  requirement of a spec (`P0-3`) - not the whole spec, not a single fixup: an arc lands as ~6-8 commits, not
  ~40 and not 1-2, so `git bisect` and the story both survive. Every commit on `main` is green on its own and
  conventional. Fold fixups and `docs(specs): tick` commits into the slice they belong to: tick a spec's
  checkboxes in the PR that fulfils them, never in a follow-up. A squash merge does the folding by itself;
  before a `git ship`, fold by hand.
- **Releases are release-please PRs** ([spec](docs/specs/release-flow.md)): never bump the version, edit
  `CHANGELOG.md` or create a tag by hand - ship the `chore(main): release X.Y.Z` PR. Commit messages on
  `main` feed the changelog and the version bump, so conventional types matter (`feat`, `fix`, `!`). Nothing
  deploys on a plain push to `main`; only prod exists.
- **Merge with `gh pr merge <n> --squash --delete-branch --auto`** (short: `-sd --auto`). It waits for the
  required `check`, then squash-merges: one GitHub-signed commit per PR, subject = PR title + PR number, so
  the PR title must be the conventional message (for a single-commit PR GitHub takes that commit's subject
  instead). `git ship` (local fast-forward, keeps Leon's signature) only works when the branch tip already
  carries a green `check` - `main` rejects an unpushed, still-running or cancelled tip. Never the rebase or
  merge-commit button (unsigned commits, non-linear history). Dependent PRs form a GitHub stack (`gh stack`),
  never a hand-made one - after a squash, a branch stacked by hand conflicts with `main`. A stack merges with
  `gh stack merge <n> --squash` (GitHub's legacy merge endpoints cannot merge one): bottom-up to #n, one squash
  commit per PR, once their `check`s are green; GitHub rebases the PRs above, and `gh stack sync` follows.
- Commit style: conventional with PR number, e.g. `feat(api): ... (#34)`.

## Orientation

Read [`docs/PROJECT.md`](docs/PROJECT.md) first: what SkillForge is for, its borders and principles. An ADR
that contradicts it is superseded by a new ADR, a contradicting spec is updated - or the sketch is
changed first, deliberately. The current structure is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the *why* of single decisions in
[`docs/decisions/`](docs/decisions/).
