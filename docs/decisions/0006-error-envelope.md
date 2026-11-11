# ADR 0006 - One error envelope for every non-2xx response

Status: Accepted, 2026-09; amended by [0010](0010-outbound-integrations.md) (point 10)

## Context

Error handling grew per endpoint: each route catches its service exceptions, raises an
`HTTPException` with a hand-picked status and string, and repeats both in `responses={...}` for the
docs (`complete_job_endpoint` in [`jobs.py`](../../app/api/v1/bot/jobs.py),
`transition_http_exception` in the since-removed `app/api/v1/bot/_transitions.py`). Three
places must stay in sync by hand, and a new domain (CRM) would copy the pattern a third time.

The contract has two concrete defects today:

- **422 has two shapes.** Domain validation failures return `{"detail": "<string>"}`
  (`PREPARE_RESPONSES` documents 422 as `ErrorResponse`), but FastAPI's own request validation on
  the same endpoints returns `{"detail": [{...}]}`. A generated client sees one status code with two
  bodies, only one of them documented.
- **Errors are only human-readable.** `ErrorResponse` carries a single `detail` string. A consumer
  that wants to branch on *which* 404/409 it got would have to string-match prose.

Context for the timing: `openapi.json` is the contract ([ADR 0001](0001-openapi-as-contract.md)) and
SkillBot's source already builds on the generated `skillforge-client`, but **neither SkillForge nor
SkillBot is live yet**. Contract changes cost a SkillBot source update today, not a broken
deployment - the cheapest moment to settle the error contract. SkillBot's code branches on
**status codes only** and never parses error bodies.

## Decision

**Every non-2xx response under this API has the same body, `ErrorResponse`:**

| Field | Type | Meaning |
|---|---|---|
| `detail` | `str` | Human-readable message. Safe to show; never contains internals. |
| `code` | `str` | Stable, machine-readable identifier in `snake_case` (e.g. `party_not_found`). Clients branch on this, never on `detail`. |
| `errors` | `list[FieldError] \| null` | Present only for request-validation failures: one entry per offending field (`loc`, `message`, `type`). |

Supporting decisions:

- **Domain errors are HTTP-agnostic and categorized.** Services raise subclasses of a small
  taxonomy in `app/core/errors.py` (`NotFoundError`, `ConflictError`, `DomainValidationError`). They
  carry `code` and a safe default `message`, but no status code.
- **One mapping table in the API layer** (`STATUS_BY_ERROR`: 404 / 409 / 422) drives both the
  global exception handlers *and* the `responses=` documentation helper. Runtime and docs cannot
  drift, and endpoints contain no `try/except` for mapped errors.
- **422 keeps its status for both meanings, but gets one shape.** A custom
  `RequestValidationError` handler emits the envelope with `code = "validation_error"` and the field
  list in `errors`. FastAPI's auto-generated `HTTPValidationError` schema is replaced by
  `ErrorResponse` in the OpenAPI output.
- **`HTTPException`s pass through the envelope too** (status-derived `code` such as
  `unauthorized`, `not_found`; headers like `WWW-Authenticate` preserved), so unknown routes and
  auth failures match the contract.
- **Existing status codes do not change.** Migration of the `bot` and `auth` domains is
  status-preserving; only bodies gain `code`.

Deliberately *not*:

- **No RFC 9457 (`application/problem+json`) yet.** The envelope is a forward-compatible subset:
  `detail` already has RFC 9457 semantics, and `type`/`title`/`status` can be added additively. The
  media-type switch is a consumer-visible change that nothing needs today.
- **No moving domain validation to 400** to dodge the 422 collision. It would change status codes
  (the one thing SkillBot's code actually reads) to fix what is a body-shape problem.
- **No status codes on service exceptions.** HTTP knowledge stays in `app/api/`.

## Consequences

- Clients get a single error-parsing path and a stable `code` to branch on; generated models shrink
  to one error type (`HTTPValidationError` / `ValidationError` disappear from the contract).
- Endpoints lose their `try/except` + `HTTPException` boilerplate; declaring which errors an endpoint
  can produce stays a one-line, per-endpoint `responses=error_responses(...)` - that is information
  FastAPI cannot infer, not duplication.
- The body of framework-level 422s changes (`detail` was a list, becomes a string; the list moves to
  `errors`). This is a breaking change for any consumer parsing that list; none exists today.
- `code` values become part of the contract. They default to the snake-cased exception class name,
  so **renaming an error class is a contract change**. It is visible in the `openapi.json` diff
  because documented examples carry the code; set `code` explicitly to rename a class safely.
- Until the `bot` and `auth` domains are migrated, their errors flow through the generic
  `HTTPException` handler and carry coarse, status-derived codes (`not_found`, `conflict`).
- The OAuth2 token endpoint builds its error bodies by hand and bypasses the handlers; it needs an
  explicit follow-up to emit the envelope (its natural codes are the RFC 6749 ones:
  `invalid_client`, `invalid_scope`, `unsupported_grant_type`).

Implementation is specified in [`api-conventions.md`](../specs/api-conventions.md) (P0-3).
