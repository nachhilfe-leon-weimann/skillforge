# Spec: API conventions (shared vocabulary for all `/api/v1` domains)

> Status: Implemented - P0-1 to P0-4 and P1 on `main` (2026-09); P0-5 superseded by [`crm-api.md`](crm-api.md) | Cross-domain arc (auth, bot, crm)
> Error contract decided in [ADR 0006](../decisions/0006-error-envelope.md) (Accepted). Written to be executed
> by coding agents: every requirement names its symbols, files, and checkable acceptance criteria.

## Problem statement

The API has three domains (`auth`, `bot`, and the new `crm`) that all need the same base behavior: scope-guarded
endpoints, list endpoints with paging, a consistent error contract, and Swagger docs that describe all of it.
Today each endpoint re-declares that behavior by hand, so it drifts:

- **Auth docs are forgotten.** Only the `bot` router sets `responses=auth_error_responses()`. 44 operations
  require a token but only 36 document 401/403. The generated client runs with
  `raise_on_unexpected_status=True`, so an *undocumented* 401 surfaces as `UnexpectedStatus` instead of a
  typed response.
- **Errors are wired three times per endpoint:** the service exception, a `try/except` raising
  `HTTPException`, and `responses={404: error_response(...)}` (`complete_job_endpoint` in
  `app/api/v1/bot/jobs.py`). 422 has two body shapes (see ADR 0006).
- **Operation IDs are unusable as client names:** `list_jobs_endpoint_api_v1_bot_jobs_get`. Per
  [ADR 0001](../decisions/0001-openapi-as-contract.md) these become the function names of the generated client.
- **List parameters are re-invented per endpoint** (`LimitQuery` in [`parties.py`](../../app/api/v1/crm/parties.py),
  inline `limit`/`offset` in `list_jobs_endpoint`), undocumented in Swagger, and the page envelope (`JobPage`) is
  not reusable.
- **Scope descriptions live beside the enum** (`DEFAULT_SCOPES`), and Swagger's Authorize dialog shows
  `BOT_READ` instead of a description because `oauth2_scheme` uses `scope.name`.

## Goals

1. **Declare once, derive twice.** Every fact (required scope, possible errors, paging bounds, field docs) is
   declared in exactly one place; runtime behavior *and* OpenAPI docs derive from it.
2. **An endpoint states only what is special about it.** No `try/except` for mapped errors, no inline
   `Annotated[...]`, no hand-written 401/403 docs.
3. **One error contract** across all domains (ADR 0006).
4. **Clean, stable contract names.** Operation IDs are short, predictable, and survive function renames.
5. **Uniform, evolvable lists.** Same parameters, same envelope, never a bare JSON array.
6. **Use the pre-launch window, then stay stable.** Neither SkillForge nor SkillBot is live yet, so contract
   renames (operation IDs, schema names) cost a SkillBot source update, not a broken deployment. Do all renames
   now, once. Paths, status codes and query parameter names stay as they are - nothing is gained by moving them.

## Non-goals

- **RFC 9457 `application/problem+json`.** Deferred by ADR 0006; the envelope is a forward-compatible subset.
- **Cursor/keyset pagination and a sort convention.** Offset + total is enough for current consumers.
- **Optimistic concurrency** (`version` / `If-Match`) for CRM writes. Worth its own spec once CRM has writers.
- **Finer-grained scopes** (`crm:parties:read`). `crm:read` / `crm:write` stay.
- **Changing any existing status code.** Includes `InvalidClientScopeError` staying 400 on the auth routes.
- **Restyling bot endpoints.** Function renames and decorator-style guards are opportunistic, not required.
- **Per-resource tags.** One tag per domain; it drives the client module and the operation-ID prefix.
- **Replacing `from_model`** with `from_attributes`. The explicit mapper is checked by `ty`; it stays.

## Decided defaults

| Topic | Decision | Rationale |
|---|---|---|
| **A - Operation IDs** | `{tag}_{function_name}`, with a trailing `_endpoint` and a leading `{tag}_` stutter stripped from the function name. | Short, unique across domains. Stripping the suffix means later function renames do **not** change the contract. |
| **B - Tags** | Exactly one domain tag per operation (`auth`, `bot`, `crm`, `system`), set on the domain router. An untagged route fails at import time. | The first tag is the generated client's module and the ID prefix. |
| **C - Scope guards** | `require_scopes(...)` returns the `Security` marker itself. Guard-only: decorator `dependencies=[...]`. Principal needed: typed parameter. | A guard is a precondition, not an input; no more `_: object` parameters. |
| **D - 401/403 docs** | Derived in an OpenAPI post-processing step from each operation's `security` requirement: 401 always, 403 where the requirement names a scope. Never declared by hand. Several requirements on one operation are alternatives, joined with `or` in the derived 403. | Derivable facts must not be declared; this is what got forgotten on `auth` and `crm`. |
| **E - Error envelope** | `ErrorResponse{detail, code, errors?}` for every non-2xx. | ADR 0006. |
| **F - Error -> status** | One table `STATUS_BY_ERROR` in the API layer feeds the exception handlers and the docs helper. | Runtime and docs cannot drift; services stay HTTP-agnostic. |
| **G - Pagination** | Offset-based: `limit` (1..100, default 50), `offset` (>= 0, default 0); envelope `Page[T]` with `items`, `total`, `limit`, `offset`. | Matches the existing `JobPage`; supports "jump to page" in admin UIs. |
| **H - Query models** | At most one query-parameter model per endpoint; filters are added by subclassing `PageParams`. `extra="forbid"`. | Mixing a query model with standalone query params breaks silently (see verified behavior). |
| **I - Field docs** | New schemas derive from `ApiModel` (`use_attribute_docstrings=True`); a docstring under a field becomes its description. | Docs feed from code; bot schemas (class docstrings only) are unaffected. |
| **J - Scope descriptions** | Stored on the `Scope` enum members; `DEFAULT_SCOPES` is removed. | A scope without a description becomes unrepresentable. |
| **K - Location** | API vocabulary in `app/api/v1/common/`; the HTTP-agnostic error taxonomy in `app/core/errors.py`. | The auth services in `app/services/auth` raise domain errors too; `app/core/auth` must not import from `app/services`. |
| **L - Pre-launch window** | Contract *renames* are free as of 2026-09 (nothing is live) and are done in this arc; *semantics* (paths, status codes, parameter names) do not change. | Renames only get more expensive; semantic changes have no upside here. |

## Verified framework behavior

Checked against the locked versions (FastAPI 0.141.1, Pydantic 2.13.4, Python 3.14) before writing this spec.
Agents can rely on these and should not re-derive them:

- PEP 695 aliases work as FastAPI parameters: `type DBSession = Annotated[AsyncSession, Depends(...)]`.
- A `Security(dep, scopes=[...])` marker works both in `dependencies=[...]` on the decorator and inside
  `Annotated[Principal, ...]`; in both positions the scopes land in the operation's `security` in OpenAPI.
- Query-parameter models (`Annotated[PageParams, Query()]`) are flattened into individual documented
  parameters, including `description`, defaults and bounds. Subclasses inherit the fields.
  With `extra="forbid"`, an unknown parameter (`?limt=7`) is a 422 instead of being ignored.
- **Trap:** a query model *plus* a standalone `Query()` parameter on the same endpoint registers without error
  but fails every request with 422 `Field required: <model param name>`. Hence decision H.
- `generate_unique_id_function` set on the `FastAPI` app is applied with the *final* merged tags, also for
  routers nested three levels deep. An untagged route registered on the app raises at decoration time.
- **Trap (found during implementation):** routes of an *included router* are resolved lazily - on the first
  request or the first `app.openapi()` call, not at `include_router`. An untagged router would therefore import
  fine and then answer 500 on its own routes, on every router included after it and on unknown paths.
  `customize_openapi` builds the schema right away, which resolves all routes and keeps decision B's
  import-time failure.
- FastAPI's `app.openapi()` regenerates its cached schema when routes change. A wrapper must not short-circuit
  on `app.openapi_schema` itself; `customize_openapi` post-processes whenever FastAPI hands out a new schema
  object.
- `class JobPage(Page[JobListItem])` keeps the OpenAPI schema name `JobPage`; using `Page[JobListItem]` directly
  yields `Page_JobListItem_`.
- A `StrEnum` whose `__new__` takes `(value, description)` keeps full `StrEnum` behavior
  (`Scope("crm:read") is Scope.CRM_READ`, `f"{Scope.CRM_READ}" == "crm:read"`).
- Overriding `app.openapi` is compatible with `scripts/dump_openapi.py`, which calls `app.openapi()`.
  Replacing FastAPI's auto-422 with `ErrorResponse` and dropping `HTTPValidationError` / `ValidationError` from
  `components.schemas` leaves no dangling `$ref`.
- Exception handlers for a base class catch subclasses; a `StarletteHTTPException` handler also covers unknown
  routes (404) and preserves `exc.headers` when passed through.

## Target shape

What a CRM endpoint looks like once P0 is done - everything else is derived:

```python
@router.get("", dependencies=[require_scopes(Scope.CRM_READ)])
async def list_parties(params: PartyListQuery, session: DBSession) -> Page[PartyResponse]:
    """List parties."""
    parties, total = await parties_service.list_parties(session, **params.model_dump())
    return Page.of([PartyResponse.from_model(p) for p in parties], total=total, params=params)


@router.get(
    "/{party_id}",
    dependencies=[require_scopes(Scope.CRM_READ)],
    responses=error_responses(PartyNotFoundError),
)
async def get_party(party_id: PartyId, session: DBSession) -> PartyResponse:
    """Read a single party."""
    return PartyResponse.from_model(await parties_service.get_party(session, party_id))
```

Derived without being written: 401/403 responses naming `crm:read`, the 404 response with its `code` example, the
422 envelope, documented `limit`/`offset`, operation IDs `crm_list_parties` / `crm_get_party`. (Since P0-7 of
[`user-authentication.md`](user-authentication.md) these two routes are reach-aware and take an `Access` from
`require_access` instead; the guard shown is that of every other read.)

## Module layout

```
app/core/errors.py               DomainError, NotFoundError, ConflictError, DomainValidationError   (new)
app/core/auth/scopes.py          Scope carries .description; DEFAULT_SCOPES removed
app/core/auth/dependencies.py    require_scopes returns the Security marker; require_access its reach-aware sibling
app/core/db/dependencies.py      DBSession                                           (moved from common/ in user-auth P0-7)
app/api/v1/common/
  __init__.py                    re-exports the public vocabulary, DBSession included
  schemas.py                     ApiModel, ErrorResponse, FieldError                 (ErrorResponse exists)
  errors.py                      STATUS_BY_ERROR, status_for, register_exception_handlers            (new)
  responses.py                   error_responses(*error_types); legacy error_response stays until P1-1
  pagination.py                  PageParams, PageQuery, Page[T]                                      (new)
  openapi.py                     operation_id, OPENAPI_TAGS, customize_openapi                       (new)
app/services/crm/errors.py       PartyNotFoundError and the CRM error catalog (crm-api.md)            (new)
```

Domain-specific vocabulary (`PartyId`, `PartyListParams`) lives in the domain package, not in `common/`.

## User stories

**Endpoint author (human or agent)**
- As an author I want to add an endpoint by writing its decorator, signature and one service call, so new
  domains do not copy boilerplate that then drifts.

**API consumer (generated client)**
- As SkillBot I want every status code I can receive to be documented, so the generated client returns typed
  responses instead of raising `UnexpectedStatus`.
- As a consumer I want a stable `code` on every error, so I never match on prose.

**Reviewer**
- As a reviewer I want contract changes to show up in the `openapi.json` diff, so an accidental rename of an
  error code or operation is caught in the PR.

## Requirements

### Must-have (P0)

**P0-1 - Stable operation IDs and OpenAPI metadata.** *Rewrites every operation ID; own PR so later diffs stay readable.*
- *Technique:* `operation_id(route)` in `app/api/v1/common/openapi.py` implements decision A and raises
  `RuntimeError` for a route without tags. Pass it as `generate_unique_id_function=` to `FastAPI(...)` in
  [`main.py`](../../app/main.py), together with `openapi_tags=OPENAPI_TAGS` (one entry with a description per
  domain tag). The `root` route in `main.py` gets `tags=["system"]`.
- *Technique:* `Scope` in [`scopes.py`](../../app/core/auth/scopes.py) gets a `description` attribute via
  `__new__(cls, value, description)`. Remove `DEFAULT_SCOPES` (and its re-export in `app/core/auth/__init__.py`);
  `seed_default_scopes` iterates `Scope`; `oauth2_scheme` in [`security.py`](../../app/core/auth/security.py)
  maps `scope.value -> scope.description`.
- *Acceptance criteria:*
  - [x] A test over `app.openapi()` asserts every `operationId` matches `^(auth|bot|crm|system)_[a-z0-9_]+$` and
        is unique.
  - [x] Spot checks: `POST /api/v1/auth/token` is `auth_create_token`; `GET /api/v1/bot/jobs` is `bot_list_jobs`;
        `GET /health` is `system_health_check` (no `system_system_` stutter); `GET /` is `system_root`.
  - [x] Renaming an endpoint function by only dropping `_endpoint` leaves all operation IDs in `openapi.json`
        unchanged.
  - [x] Every tag used by an operation has an `openapi_tags` entry with a non-empty description.
  - [x] `components.securitySchemes` lists every scope with its description text; no reference to
        `DEFAULT_SCOPES` remains; existing scope-seeding tests pass unchanged in behavior.
  - [x] `openapi.json` is regenerated via `just openapi`; the PR description lists the consumer impact (below).

**P0-2 - Scope guards as declarations, 401/403 derived.**
- *Technique:* `require_scopes(*scopes)` in [`dependencies.py`](../../app/core/auth/dependencies.py) returns
  `Security(get_current_principal, scopes=[...])` instead of a closure. All call sites change in the same PR:
  the aliases `BotRead`, `BotWrite`, `CrmRead`, `CrmWrite`, `ManageAuthClients` become
  `Annotated[Principal, require_scopes(...)]` (no `Depends(...)` wrapper, no `object`), as do the local aliases
  in [`test_dependencies.py`](../../tests/auth/test_dependencies.py) and
  [`test_logging.py`](../../tests/test_logging.py). Existing `_: BotRead` parameters keep working.
- *Convention for new endpoints:* guard only -> `dependencies=[require_scopes(Scope.X)]` on the decorator;
  principal needed (audit, `/users/me`) -> a parameter named `principal` typed with the alias. Never `_`.
- *Technique:* `customize_openapi(app)` in `app/api/v1/common/openapi.py` wraps `app.openapi` (respecting the
  `app.openapi_schema` cache). For every operation with a non-empty `security` list it sets responses `401` and
  `403` with the `ErrorResponse` schema (making sure the schema exists in `components.schemas` even if no route
  references it); the 403 description names the required scopes
  (`Missing required scope: crm:read`). Reuse the example payloads of `_authentication_error_response` in
  [`responses.py`](../../app/api/v1/common/responses.py). Called once in `main.py` after the routers are included.
- *Technique:* remove `responses=auth_error_responses()` from the bot router and delete `auth_error_responses`.
- *Acceptance criteria:*
  - [x] A test over `app.openapi()` asserts: every operation with `security` documents 401 and 403; no
        operation without `security` documents either. *Refined by P1-3: "either" means the derived bearer-token
        responses. The token endpoint has no `security` but owns a 401 of its own (`invalid_client`), which it
        declares itself.*
  - [x] The 403 description of `GET /api/v1/bot/jobs` contains `bot:read`; the `auth/clients` operations now
        document 401/403 (they do not today).
  - [x] Runtime behavior is unchanged: the existing auth dependency tests pass (missing token 401 with
        `WWW-Authenticate`, invalid token 401, missing scope 403).
  - [x] No occurrence of `Depends(require_scopes(` or `Annotated[object,` remains in `app/` or `tests/`.

**P0-3 - Error envelope and global handlers** (implements ADR 0006).
- *Technique - taxonomy:* `app/core/errors.py` defines `DomainError(Exception)` with class attributes
  `code: str`, `message: str` (safe public default) and `expose_message: bool = False`, plus the categories
  `NotFoundError`, `ConflictError`, `DomainValidationError`. `__init_subclass__` derives `code` from the class
  name (`PartyNotFoundError` -> `party_not_found`) unless the class sets it explicitly.
- *Technique - mapping:* `app/api/v1/common/errors.py` holds
  `STATUS_BY_ERROR = {NotFoundError: 404, ConflictError: 409, DomainValidationError: 422}` and
  `status_for(error_type)` (first match along the MRO). `register_exception_handlers(app)` registers handlers for
  - `DomainError` -> `status_for(type(exc))`; `detail` is `str(exc)` only if `expose_message` is set and the
    message is non-empty, otherwise the class `message` (instance messages may contain internals such as IDs);
  - `RequestValidationError` -> 422, `detail="Request validation failed"`, `code="validation_error"`, `errors`
    built from `exc.errors()` (`loc`, `msg` -> `message`, `type`);
  - `StarletteHTTPException` -> same status and **headers**, `code` from the status phrase
    (`unauthorized`, `forbidden`, `not_found`, `conflict`).
- *Technique - schema:* `ErrorResponse` in [`schemas.py`](../../app/api/v1/common/schemas.py) gains `code: str`
  (required) and `errors: list[FieldError] | None = None`; bodies are serialized with `exclude_none`.
- *Technique - docs:* `error_responses(*error_types)` in `responses.py` groups the given error classes by
  `status_for` and returns a `responses=` dict whose examples are keyed by `code` and contain
  `{"detail": message, "code": code}`. `customize_openapi` additionally replaces FastAPI's **auto-generated** 422
  (the one referencing `HTTPValidationError`) with `ErrorResponse` and removes the two unused validation schemas.
  A 422 declared by a route itself is left untouched - except that, if it lists `examples` (as
  `error_responses` does) and the route takes input, the `validation_error` example joins them: FastAPI omits
  its own 422 once a route declares one, although request validation can still fail.
- *Acceptance criteria:*
  - [x] A parametrized API test asserts the body validates against `ErrorResponse` for: a mapped domain error,
        an unknown route (404), a missing token (401, `WWW-Authenticate` header still present), a missing
        scope (403), a malformed path/query parameter (422 with a non-empty `errors` list).
  - [x] A test imports all service error modules and asserts every concrete `DomainError` subclass resolves via
        `status_for` and that all `code` values are unique.
  - [x] An instance message is **not** leaked for a class with `expose_message = False`.
  - [x] `openapi.json` contains neither `HTTPValidationError` nor `ValidationError`, and no dangling `$ref`.
  - [x] No existing status code changes (`git diff openapi.json` shows no added/removed status keys except the
        401/403 additions from P0-2).
  - ~~Known and accepted until P1-1: examples produced by the legacy `error_response()` helper lack `code`.~~
    Closed by P1-1: the helper is gone and `test_every_documented_error_example_is_a_valid_envelope` guards it.

**P0-4 - Pagination vocabulary.**
- *Technique:* introduce `ApiModel` (decision I) in [`schemas.py`](../../app/api/v1/common/schemas.py).
  `app/api/v1/common/pagination.py` defines `PageParams(BaseModel)` (decision G, `extra="forbid"`, each field
  with a `description`), `type PageQuery = Annotated[PageParams, Query()]`, and the generic `Page[T]` (an
  `ApiModel` subclass using PEP 695 syntax) with `items`, `total`, `limit`, `offset` plus a constructor helper
  `Page.of(items, *, total, params)`.
- *Rules:* a list endpoint never returns a bare array. Filters go into a `PageParams` subclass declared next to
  the endpoint (decision H). The service signature is `list_x(session, *, limit, offset, <filters>) ->
  tuple[list[X], int]` (as `list_jobs` already does) and orders by a deterministic key ending in a unique column.
- *Acceptance criteria:*
  - [x] In `openapi.json`, `limit` and `offset` of a paged endpoint carry description, default, `minimum` and
        `maximum`. *Fulfilled by the bot lists (P1-2), the first real paged endpoints; asserted by
        `test_paged_endpoint_documents_limit_and_offset` in
        [`test_openapi_contract.py`](../../tests/api/test_openapi_contract.py).*
  - [x] `?limit=0`, `?limit=101`, `?offset=-1` and an unknown query parameter each return 422 in the envelope.
  - [x] A subclass with one filter field documents and parses all three parameters.

**P0-5 - CRM `parties` as the reference implementation.** *Superseded by [`crm-api.md`](crm-api.md): the CRM
surface is specified there as a whole, and its standing criteria carry over the acceptance criteria below. The
technique stays as the record of what was planned and was not built in this form (there is no `PartyResponse`; the
read side is `PartyListItem` and the `PartyDetail` union). The criteria are ticked because the CRM API meets them -
for all 21 CRM operations, not only for two.*
- *Technique:* bring [`parties.py`](../../app/api/v1/crm/parties.py) to the target shape. `get_party` in the
  service raises `PartyNotFoundError(NotFoundError)` from `app/services/crm/errors.py` instead of returning
  `None`; the list endpoint returns `Page[PartyResponse]` and takes `PartyListQuery`, the alias of a
  `PartyListParams(PageParams)` subclass carrying the `type` filter; guards move to the decorator; `PartyId` uses
  `examples=[...]` (the singular `example=` is deprecated); `PartyResponse` derives from `ApiModel` with field
  docstrings instead of `Field(..., description=...)`; endpoint functions drop the `_endpoint` suffix and
  import the service as a namespace (`from app.services.crm import parties as parties_service`).
- *Acceptance criteria:*
  - [x] `parties.py` contains no `HTTPException`, no `try/except`, no inline `Annotated[...]` in signatures and
        no parameter named `_`.
  - [x] API tests cover: list paging, read hit, read miss (404 with `code == "party_not_found"`), 401, 403.
  - [x] Swagger shows for both GET operations: scope, 401/403, parameter docs, field descriptions.
- *Proven by:* `test_the_crm_endpoints_carry_no_boilerplate` in
  [`test_crm_architecture.py`](../../tests/api/test_crm_architecture.py) (an AST check over every CRM endpoint);
  [`test_crm_party_list_api.py`](../../tests/db/crm/test_crm_party_list_api.py) and
  [`test_crm_parties_api.py`](../../tests/db/crm/test_crm_parties_api.py) for paging, hit and miss;
  [`test_crm_auth.py`](../../tests/api/test_crm_auth.py) for 401 / 403 on every CRM operation; and
  [`test_crm_openapi.py`](../../tests/api/test_crm_openapi.py) for scopes and descriptions in the contract.

### Nice-to-have (P1)

**P1-1 - Migrate the `bot` domain to the taxonomy (status-preserving).** Re-parent the classes in
`app/services/bot/errors.py` (`JobNotFoundError(BotServiceError, NotFoundError)`, ...), set `message` to today's
public strings (`JOB_NOT_FOUND`, ...) and `expose_message = True` exactly where `transition_http_exception`
surfaces `str(exc)` today. Then delete the per-endpoint `try/except`, `transition_http_exception`, the
`*_RESPONSES` dicts and the legacy `error_response()` helper. The bot's own `PartyNotFoundError` is replaced by
the CRM one (CRM owns parties).
- [x] `git diff openapi.json` shows no status-code change; every `detail` string observable today is unchanged
      (pinned per endpoint and error by
      [`test_bot_error_contract.py`](https://github.com/Nachhilfe-Leon-Weimann/skillforge/blob/v0.5.0/tests/api/test_bot_error_contract.py)). *One deliberate exception,
      approved 2026-09-18:* `PrincipalNotFoundError` surfaced as three endpoint-specific strings ("Discord
      principal not found", "Discord user not found", "Actor principal not found"); one class has one `code` and
      now one `detail`, "Discord principal not found".
- [x] Closed by P0-2 of [`crm-api.md`](crm-api.md): `PartyNotFoundError` is defined once, in
      `app/services/crm/errors.py`; the bot's class is deleted and `app/services/bot/errors.py` imports the CRM one.
      Status, `code` and `detail` are unchanged, and
      [`test_error_taxonomy.py`](../../tests/api/test_error_taxonomy.py) passes with a single `party_not_found`.

**P1-2 - Bot lists adopt the vocabulary.** `JobPage` and `OperationPage` are deleted in favor of
`Page[JobListItem]` / `Page[...]` (the schema rename is free pre-launch, decision L); the list endpoints move to
`PageParams` subclasses with unchanged parameter names (`status` stays an alias). Behavior change to flag in the
PR: unknown query parameters become 422.
- [x] Done. `status` needs no alias any more: inside a params model the field can simply be named `status` (the
      alias only existed because a function parameter of that name shadowed `fastapi.status`), so the endpoint
      calls the service with `**params.model_dump()`. Parameter names, bounds and defaults are unchanged in
      `openapi.json`.

**P1-3 - Token endpoint emits the envelope.** The hand-built `JSONResponse`s in `create_token` get `code`
(`invalid_client`, `invalid_scope`, `unsupported_grant_type`); status codes and `WWW-Authenticate` unchanged.
- [x] Done via `ApiError` declarations in `app/api/v1/auth/errors.py`. Beyond the three codes: the missing
      credentials 422 gets the RFC 6749 code `invalid_request`, and the endpoint now *documents* its 400/401/422
      (they were undocumented, so the generated client raised `UnexpectedStatus`; SkillBot reads
      `response.status_code` and is unaffected). The two denials stay *returned* rather than raised - the
      `TOKEN_DENIED` audit entry must commit - which
      `test_auth_token_denial_commits_the_session_so_the_audit_entry_survives` guards.

**P1-4 - Catch-all 500.** An `Exception` handler returns the envelope with `code="internal_error"` and a generic
`detail`; the logging middleware (`register_request_logging`) must still log the traceback.
- [x] Done. The handler runs in Starlette's outermost `ServerErrorMiddleware`, which re-raises after responding;
      the request-logging middleware sits inside it and logs `http_request_failed` with the traceback exactly once
      (`test_request_logging_keeps_the_traceback_when_the_500_envelope_handles_the_exception`). A `DomainError`
      outside every category also ends here, as a 500 rather than a guessed status.
      Because the response is built outside the logging middleware, the handler adds the `x-request-id` header
      itself (`get_request_id` in `app/core/logging/middleware.py`), so a 500 can be matched to its log entry.

**P1-5 - Migrate the `auth` client routes.** Re-parent the `ApplicationClient*` errors; `InvalidClientScopeError`
keeps a local 400 mapping (non-goal: changing status codes).
- [x] Done; status and `detail` of every route are pinned by
      [`test_auth_clients_error_contract.py`](../../tests/api/test_auth_clients_error_contract.py) (these routes had
      no API tests before). The routes now *document* their 404/409/400, which were undocumented. The local 400 is
      the shared `INVALID_SCOPE` declaration (`code="invalid_scope"`); it is raised, not returned, so scopes granted
      before the invalid one roll back.
- [x] `GET /auth/clients` returns `Page[ApplicationClientResponse]` instead of a bare JSON array (decided
      2026-09-18; a response-shape change, free pre-launch and unused by SkillBot). `list_application_clients`
      follows the list-service rule: `limit`/`offset`, `(page, total)`, ordered by the unique `client_id`.
      `test_no_operation_returns_a_bare_array` guards goal 5 from now on.

**P1-6 - Record the conventions.** Add a bullet to `CLAUDE.md` (Conventions) and a short section to
`docs/ARCHITECTURE.md` pointing here, once P0 is merged.
- [x] Done ahead of the CRM: the conventions are in force for `auth` and `bot`, and new CRM endpoints are
      written against them. The reference implementation to point at is the CRM API
      ([`crm-api.md`](crm-api.md)), starting with [`subjects.py`](../../app/api/v1/crm/subjects.py).

### Future considerations (P2)

- RFC 9457 media type plus `type` / `title` / `status` members (additive on top of the envelope).
- Precise auth codes (`invalid_token`, `insufficient_scope`) instead of status-derived ones.
- Cursor pagination and a `sort` convention once a table outgrows offset scans.
- Optimistic concurrency and a PATCH convention (`model_fields_set`, as `update_application_client_endpoint`
  already does) for CRM writes; `201` + `Location` on create.

## Consumer impact

**Nothing is live (2026-09), so no change in this spec breaks a deployed consumer.** SkillBot's source builds on
the generated `skillforge-client` (`just generate-python-client`, `openapi-python-client`). It branches on status
codes only and never parses error bodies, so P0-2 to P0-5 need no SkillBot change. P0-1 renames the generated
names it imports; SkillBot updates them when it picks up the regenerated client:

| Today | After P0-1 |
|---|---|
| `create_token_api_v1_auth_token_post` | `auth_create_token` |
| `upsert_discord_user_endpoint_api_v1_bot_users_discord_id_put` | `bot_upsert_discord_user` |
| `link_discord_account_endpoint_api_v1_bot_users_discord_id_account_put` | `bot_link_discord_account` |
| `liveness_check_health_live_get` | `system_liveness_check` |
| model `BodyCreateTokenApiV1AuthTokenPost` | derived from `Body_auth_create_token` |

These five are all the generated names SkillBot imports today. The cost grows with every feature it adds and
jumps once either side is live, which is why the rename happens first.

## Success metrics

- *Undocumented auth responses:* operations with `security` but without 401/403 in `openapi.json`.
  **Target: 0** (today: 8).
- *Error shapes in the contract:* distinct error body schemas. **Target: 1** (today: 2).
- *Boilerplate:* `HTTPException` occurrences under `app/api/v1/`. **Target: 0 in `crm/`** (a standing criterion
  of [`crm-api.md`](crm-api.md)); trending to 0 in `bot/` with P1-1.
- *Endpoint cost:* a new CRUD read endpoint is decorator + signature + at most three body lines.

## Decided (formerly open questions)

- **422:** keep the status for domain validation, unify the body (ADR 0006) - *not* a move to 400. SkillBot reads
  status codes, not bodies, so this is the variant without consumer impact.
- **Operation IDs now, not later** - nothing is live, SkillBot imports only five generated names; both facts
  have an expiry date.
- **`code` is auto-derived** from the class name, overridable explicitly; examples in `openapi.json` make renames
  visible in review.
- **`Page[T]` directly everywhere**, including the bot lists (P1-2). Named subclasses such as
  `class JobPage(Page[JobListItem])` are the fallback for keeping a schema name stable once the API is live.
- **`PartyNotFoundError` is defined once**, in `app/services/crm/errors.py`.

## Timeline / phasing

One PR per requirement, in this order; each keeps `just check` green and regenerates `openapi.json`:

1. **P0-1** alone - it rewrites every operation ID in `openapi.json`; SkillBot updates its five imports when it
   regenerates the client.
2. **P0-2** - security declaration + derived 401/403.
3. **P0-3** - envelope, handlers, docs helper (ADR 0006).
4. **P0-4** - pagination; P0-5 is superseded by [`crm-api.md`](crm-api.md), whose list endpoints use the
   vocabulary (the bot lists of P1-2 were its first real users).
5. **P1-1 .. P1-6** as independent follow-ups, bot migration first.

**Dependency:** none open - ADR 0006 is accepted. **Blocks:** every further CRM endpoint should be
written against the target shape, so steps 1-4 come before the CRM surface grows.

## Rules for implementing agents

- Follow the repo conventions in [`CLAUDE.md`](../../CLAUDE.md): English only, symbol references instead of line
  numbers in docs, never hand-edit `openapi.json` (run `just openapi`), `just check` green before every commit,
  conventional commits with PR number.
- Do not change paths, status codes, query parameter names or response field names, and rename schemas or
  operations only where a requirement above says so (decision L). If a criterion cannot be met without such a change, stop and report.
- Stay inside the requirement's PR scope; opportunistic bot restyling belongs to P1.
- Tick the acceptance checkboxes in this file in the PR that fulfils them and flip the status line when P0 is done.
