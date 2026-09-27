# SkillForge

The central service of the skill-platform. SkillForge keeps the data the tutoring business runs on -
people, organisations and how they relate - and decides who may do what with it. SkillBot and the web
portal (skillsite) are frontends: they come to SkillForge for central data and decisions.

What SkillForge is for, where its borders are and what comes next is in the
[project sketch](docs/PROJECT.md). Some of today's code is on its way out: SkillBot's state, the job queue
and the two-phase operations move to the bot ([where we are](docs/PROJECT.md#where-we-are)).

## Development

Needs `uv`, `just` and Docker. Python 3.14, FastAPI, async SQLAlchemy 2, Alembic, PostgreSQL (Neon in prod).

| Command        |                                                                                          |
| -------------- | ---------------------------------------------------------------------------------------- |
| `just dev`     | run the API on http://localhost:8000, API docs at `/docs`                                |
| `just check`   | lint, format check, typecheck, OpenAPI drift and tests without DB - keep green to commit |
| `just test`    | all tests; the DB tests get a throwaway Postgres via testcontainers                      |
| `just openapi` | regenerate `openapi.json` after an API change                                            |

Everything else is in the [`justfile`](justfile).

`just dev` needs a Postgres at `DB__URL`, migrated and with the auth state seeded:

```bash
docker run -d --name skillforge-pg -p 5432:5432 \
  -e POSTGRES_USER=skillforge -e POSTGRES_PASSWORD=skillforge -e POSTGRES_DB=skillforge \
  postgres:17
cp .env.example .env
uv run alembic upgrade head
just bootstrap-skillbot
```

`GET /health` shows whether the database and the background workers are fine.

## API

[`openapi.json`](openapi.json) is the contract the frontends build on. It is generated, never edited by hand.
Every release publishes a matching Python client,
[`skillforge-client`](https://pypi.org/project/skillforge-client/).

## Releasing

Merging the release PR (`chore(main): release X.Y.Z`) is the release: tag, image, client and deploy follow,
and the deploy fails unless `GET /health` reports the new version. Never bump the version or tag by hand -
the conventional commits on `main` (`feat`, `fix`, `!`) drive both. The why is in
[`release-flow.md`](docs/specs/release-flow.md).

`compose.yml` names the version prod runs; the release PR moves it forward.

If a `Release` run fails after the release exists, fix the cause and use **Re-run failed jobs**, never
_Re-run all jobs_: that finds the release already made, skips everything and still turns green. To rebuild
only the image, dispatch `Build` for the tag; to deploy a release again, dispatch `Deploy` with its version.

### Rolling back

There is no automatic rollback. To go back to an earlier release:

1. Restore the earlier tag's whole `compose.yml` (`git show vX.Y.Z:compose.yml > compose.yml`) in a PR
   titled `chore(deploy): roll back to vX.Y.Z` (a `chore` makes no release) and merge it. Restoring only
   the `image:` lines is not enough: an older image may lack the module the current `worker` command names.
2. Dispatch `Deploy` with that version.

The next release PR moves the tags forward again. This rolls back the app, not the database: if a release
in between shipped a migration, the older image's `migrate` service does not know the database's revision
and the deploy fails. Fix forward instead, or first run `alembic downgrade <revision>` from the newer image.

## Docs

- [`PROJECT.md`](docs/PROJECT.md) - what SkillForge is for, its borders and principles
- [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) - how it is built today
- [`DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) - the database
- [`decisions/`](docs/decisions/) - why things were decided (ADRs)
- [`specs/`](docs/specs/) - work orders, one per arc
- [`CLAUDE.md`](CLAUDE.md) - commands and conventions for AI assistants
