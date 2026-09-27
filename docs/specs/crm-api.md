# Spec: CRM API (parties, roles, contact infos, relations, subjects)

> Status: Implemented - P0 and P1 on `main` (2026-09) | Domain arc `crm`
> Builds on [`api-conventions.md`](api-conventions.md) (P0-1 to P0-4 and P1 merged) and on
> [ADR 0007](../decisions/0007-crm-system-of-record.md) (Accepted). **Supersedes P0-5 of `api-conventions.md`.**
> Written to be executed by coding agents: every requirement names its symbols, files and checkable criteria.

## Problem statement

The `core` schema models the business (parties, persons, companies, the `student` / `tutor` roles, contact infos,
party relations, subjects), but **nothing in the codebase creates any of it**. Every real student, parent or tutor
is an out-of-band SQL operation today. The bot can only link a Discord account to a party that already exists
(`link_discord_account` in [`provisioning.py`](../../app/services/bot/provisioning.py)), so without a CRM API the
platform cannot be filled with real data.

The first consumer is **the operator working in Swagger UI**: entering people by hand, copying UUIDs between calls,
correcting mistakes. That shapes the API more than any future UI does.

## Goals

1. **Data gets in through the API.** A complete real-world case - a new student whose mother pays - takes four
   calls and no database access.
2. **Flat, typed request bodies.** Polymorphism exists on the read side only; Swagger UI renders `oneOf` request
   bodies poorly and PATCH on a union is awkward.
3. **One read model, many small write commands.** Every party write answers with the same detail representation
   `GET /parties/{party_id}` returns.
4. **Exactly one target party per route.** Later object-level authorization (delegation via `PARENT_OF` /
   `PAYS_FOR`) becomes a single dependency instead of a redesign.
5. **The CRM leads** (ADR 0007): CRM writes are validated against CRM rules only, never against Discord state.
6. **No new cross-cutting mechanism.** Everything is built from the conventions vocabulary (`Page`, `PageParams`,
   `ApiModel`, `error_responses`, the error taxonomy, `require_scopes`).

## Non-goals

- **A composite onboarding endpoint.** Nested create (decision C) covers the need; revisit with a real UI.
- **User principals, delegation, object-level authorization, `/users/me`.** Only application clients exist today.
- **Addresses.** `geo.plz_ort` is reference data; there is no address model yet.
- **Archiving, soft delete, GDPR erasure or anonymization.** Needs its own spec (minors' data, sevDesk retention).
- **Optimistic concurrency** (`version` / `If-Match`).
- **Managing `ext` links** (sevDesk, Clockodo, Microsoft, Discord) through the CRM, including showing them in the
  party detail. They belong to the integrations (ADR 0007).
- **Change feed / eventing.** `updated_at` on the aggregate root is the only signal; P1-3 adds a filter on it.
- **A `sort` parameter, fuzzy search, `pg_trgm`.** Fixed order and `ILIKE` are enough at this size.
- **Global duplicate prevention** (a parent's e-mail may legitimately appear on the child too).
- **Any change to the bot domain in P0.** The follow-ups are P1-2.

## Decided defaults

| Topic                             | Decision                                                                                                                                                                                                                                                                                                      | Rationale                                                                                                                                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A - Route form**                | Polymorphic read side under `/parties`; typed write side under `/persons` and `/companies`. `party.type` is immutable.                                                                                                                                                                                        | Typed bodies for Swagger and generated clients; one ID space makes both address the same entity.                                                                                              |
| **B - Roles**                     | Idempotent singleton sub-resource: `PUT` / `DELETE /persons/{party_id}/student` and `/tutor`. No `/students` or `/tutors` collections; `GET /parties?role=` covers lists. A person may hold both roles.                                                                                                       | A role is a fact about a person, keyed by the person's ID.                                                                                                                                    |
| **C - Nested create**             | `POST /persons` accepts optional `contact_infos`, `student`, `tutor`; `POST /companies` accepts `contact_infos`. Updates stay granular.                                                                                                                                                                       | Owned children may be born with their parent; one transaction per real-world step.                                                                                                            |
| **D - Children vs. associations** | Owned children with their own ID are nested CRUD (`/parties/{id}/contact-infos`). Associations are addressed by natural key with idempotent `PUT` / `DELETE` (roles, relations).                                                                                                                              | Mirrors the keys in the schema: `contact_info.id` vs. the composite key of `party_relation`.                                                                                                  |
| **E - Responses**                 | Party writes return `PersonDetail` / `CompanyDetail`; child routes return the child; relation `PUT` returns `RelationResponse`; every `DELETE` is 204; `POST` is 201.                                                                                                                                         | The relation answer shows the other party's name: immediate feedback when pasting UUIDs.                                                                                                      |
| **F - Detail shape**              | Discriminated union `PartyDetail = PersonDetail \| CompanyDetail` on `type`. It appears in exactly one operation, `GET /parties/{party_id}`.                                                                                                                                                                  | Illegal states (both, neither, a company with a student role) are unrepresentable; fields stay flat.                                                                                          |
| **G - PATCH**                     | Update models use Pydantic's experimental `MISSING` sentinel.                                                                                                                                                                                                                                                 | Optional but not nullable in the schema, explicit `null` is rejected, unset fields vanish from `model_dump()`. Confined to a handful of classes; trivially replaceable by `model_fields_set`. |
| **H - Aggregate root**            | `Party` is the aggregate root: every write inside the aggregate sets `party.updated_at`; the detail exposes only that timestamp. Relations touch both parties.                                                                                                                                                | One answer to "what changed since X" for later sync consumers.                                                                                                                                |
| **I - Error rules**               | (1) Missing thing named in the **path** -> 404; missing thing referenced in the **body** -> 422. (2) What can be checked without the database lives in the Pydantic model (framework 422 with a field path). (3) A new `code` only if a client would branch on it; otherwise one class with `expose_message`. | Keeps the catalog at 15 classes; matches `CommandEnvValidationError` in the bot domain.                                                                                                       |
| **J - Relation rules**            | Enforced on write only, see the table below. Inverse or cyclic `parent_of` is not checked; removing a role leaves relations in place. _Amended 2026-09 by [`bot-decoupling.md`](bot-decoupling.md), decision O (built in its P0-6): removing the tutor role removes the party's outgoing `tutor_of`, removing the student role its incoming `tutor_of`, and both sides move._ | Wrong pairs would poison delegation later; more strictness has no consumer.                                                                                                                   |
| **K - Deleting a party**          | Allowed, guarded: 409 while any `ext` link or Discord account exists. Roles, contact infos and relations cascade. _Amended 2026-09 by [`bot-decoupling.md`](bot-decoupling.md), decision H (built in its P0-2): only an active Discord link guards the party; a deactivated one goes with it._ | The operator must be able to undo a mistaken create; external systems must not be orphaned.                                                                                                   |
| **L - Search**                    | One query model per list; filters never produce errors (an impossible combination is an empty page); fixed order; `q` also matches contact values.                                                                                                                                                            | `q` on e-mail doubles as the duplicate check before creating.                                                                                                                                 |
| **M - Service layer**             | One loading path (`PARTY_GRAPH` / `load_party`); every write function ends by reloading through it.                                                                                                                                                                                                           | Async SQLAlchemy cannot lazy-load, see verified behavior.                                                                                                                                     |
| **N - Session scope**             | `DBSession = Annotated[AsyncSession, Depends(get_db_session, scope="function")]`.                                                                                                                                                                                                                             | With the default scope the commit runs after the response is sent; a failed commit would still answer 201.                                                                                    |
| **O - Shared vocabulary**         | Enums and input dataclasses used by both layers live in `app/services/crm/inputs.py`; the API imports them, never the reverse.                                                                                                                                                                                | Same direction as `views.py` in the bot domain.                                                                                                                                               |

## Verified framework behavior

Checked before writing this spec against the locked versions (FastAPI 0.141.1, Pydantic 2.13.4, SQLAlchemy
2.0.52, Python 3.14), the last block against the real models in a throwaway Postgres with the production session
settings (`autoflush=False`, `expire_on_commit=False`). Agents can rely on these:

- **Loading traps.** After create + `flush()`: `party.created_at` is available (`RETURNING`), but a relationship
  that was never set (`party.company`, `person.student`) raises `MissingGreenlet`. After an update + `flush()`:
  `person.updated_at` (`onupdate`) and a `party.updated_at` assigned as `func.now()` raise `MissingGreenlet`.
- **The reload works.** `select(Party).options(*PARTY_GRAPH).execution_options(populate_existing=True)` after a
  `flush()` returns the same identity with everything accessible. The `flush()` is mandatory (`autoflush=False`).
- **Polymorphic order and count.** Outer joins to `Person` and `Company`, ordered by
  `lower(coalesce(company.name, person.lastname || ' ' || person.firstname)), party.id`, sort case-insensitively
  across both types; `select(count()).select_from(<same statement>.subquery())` yields `total`.
- **The union in OpenAPI.** `type PartyDetail = Annotated[PersonDetail | CompanyDetail, Field(discriminator="type")]`
  with `type: Literal[PartyType.PERSON]` works as a return annotation and becomes a named `PartyDetail` schema
  (`oneOf` + `discriminator` mapping). A typed write route returning `PersonDetail` references that member directly.
- **The pinned client generator copes.** `openapi-python-client==0.29.0` with `--fail-on-warning` generates
  cleanly. It ignores the discriminator and tries the variants in order, which works because `PersonDetail` and
  `CompanyDetail` have different required fields. Keep it that way.
- **Alias style matters.** A PEP 695 alias (`type Name = Annotated[str, ...]`) becomes its own `$ref` schema; a
  plain assignment (`Name = Annotated[str, ...]`) inlines the constraints at the field. Use the plain form for
  scalars, the PEP 695 form for the union.
- **`MISSING`.** `from pydantic.experimental.missing_sentinel import MISSING` imports without a warning.
  `field: str | MISSING = MISSING` is optional and non-nullable in the JSON schema, rejects an explicit `null`
  (422), and is omitted by `model_dump()`; `str | None | MISSING` keeps `null` meaningful. Works end to end through
  a FastAPI PATCH body; `ty` accepts the `is not MISSING` narrowing.
- **Small ones.** `set[int]` becomes `uniqueItems` and collapses duplicates silently. `EmailStr` is available
  (`email-validator` ships with `fastapi[standard]`). `Field(examples=[...])` lands in the schema, which is what
  Swagger UI builds its sample body from. `StringConstraints(strip_whitespace=True, min_length=1)` turns `"  Max "`
  into `"Max"` and rejects a blank string.
- **Commit timing.** A dependency with `yield` runs its exit code after the response is sent. A failing commit
  behind a 201 route still delivered 201; with `scope="function"` the same failure is a 500, and with the merged
  handlers of `register_exception_handlers` that 500 is the `internal_error` envelope.

## Route map

All routes live under `/api/v1/crm`, tag `crm`. Reads need `crm:read`, everything else `crm:write`, declared with
`dependencies=[require_scopes(...)]` on the decorator. Exception: `GET /parties` and `GET /parties/{party_id}` take
`CrmReadAccess` and accept `crm:read` or `crm:read:own` (P0-7 of [`user-authentication.md`](user-authentication.md)).

```
# Parties - polymorphic read side
GET    /parties                                         list and search
GET    /parties/{party_id}                              detail (the only union response)
DELETE /parties/{party_id}                              guarded (decision K)

# Persons and companies - typed write side
POST   /persons                                         party + person (+ contact_infos, student, tutor)
PATCH  /persons/{party_id}
POST   /companies                                       party + company (+ contact_infos)
PATCH  /companies/{party_id}

# Roles - idempotent singletons
PUT    /persons/{party_id}/student                      {preferred_meeting_tool, subject_ids}
DELETE /persons/{party_id}/student
PUT    /persons/{party_id}/tutor                        {subject_ids}
DELETE /persons/{party_id}/tutor

# Contact infos - owned children
POST   /parties/{party_id}/contact-infos
PATCH  /parties/{party_id}/contact-infos/{contact_info_id}
DELETE /parties/{party_id}/contact-infos/{contact_info_id}

# Relations - natural key in the URL: {party_id} --type--> {to_party_id}
GET    /parties/{party_id}/relations
PUT    /parties/{party_id}/relations/{type}/{to_party_id}
DELETE /parties/{party_id}/relations/{type}/{to_party_id}

# Subjects - reference data
GET    /subjects
POST   /subjects
PATCH  /subjects/{subject_id}
DELETE /subjects/{subject_id}
```

Endpoint function names, and therefore the operation IDs after the `crm_` prefix, are part of the contract and must
be unique across the CRM routers: `list_parties`, `get_party`, `delete_party`, `create_person`, `update_person`,
`create_company`, `update_company`, `put_student_role`, `remove_student_role`, `put_tutor_role`, `remove_tutor_role`,
`add_contact_info`, `update_contact_info`, `remove_contact_info`, `list_relations`, `put_relation`,
`remove_relation`, `list_subjects`, `create_subject`, `update_subject`, `delete_subject`. Services are imported as
namespaces (`from app.services.crm import persons as persons_service`) so the names do not clash.

**The reference flow** (goal 1): `POST /persons` (student with contact infos and the `student` role),
`POST /persons` (mother), `PUT /parties/{mother}/relations/parent_of/{student}`,
`PUT /parties/{mother}/relations/pays_for/{student}`.

## Representations

Read and write models are different classes. Reads resolve (`subjects` with titles, `display_name`); writes take
IDs and raw values. Names follow the existing convention (`*ListItem`, `*Detail`, `*CreateRequest`,
`*UpdateRequest`, `*Response`). All derive from `ApiModel`; field docstrings are the descriptions; create models
carry `Field(examples=[...])`.

```python
class PartyListItem:        id, type: PartyType, display_name, roles: list[PartyRole]
class PersonDetail:         type: Literal[PartyType.PERSON], id, display_name, firstname, lastname,
                            student: StudentRole | None, tutor: TutorRole | None,
                            contact_infos: list[ContactInfoResponse], created_at, updated_at
class CompanyDetail:        type: Literal[PartyType.COMPANY], id, display_name, name,
                            contact_infos: list[ContactInfoResponse], created_at, updated_at
type PartyDetail = Annotated[PersonDetail | CompanyDetail, Field(discriminator="type")]

class StudentRole:          preferred_meeting_tool, subjects: list[SubjectResponse]
class TutorRole:            subjects: list[SubjectResponse]
class ContactInfoResponse:  id, type: ContactInfoType, value, label
class SubjectResponse:      id, title
class RelationResponse:     type: PartyRelationType, direction: RelationDirection, party: PartyListItem, created_at
```

- `display_name` is `"{firstname} {lastname}"` for a person and `name` for a company. `roles` lists the roles a
  person holds (`PartyRole`: `student`, `tutor`); it is empty for companies. `updated_at` is `party.updated_at`.
- `RelationResponse` is relative to the party in the URL: `direction` is `outgoing` when that party is the `from`
  side, and `party` is always the _other_ side.
- `party_detail(party: Party) -> PersonDetail | CompanyDetail` dispatches on `party.type` with `match` and
  `assert_never`, so `ty` proves exhaustiveness. Mapping stays in explicit `from_model` classmethods.

```python
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

class PersonCreateRequest:      firstname: Name, lastname: Name, contact_infos: list[ContactInfoCreateRequest] = [],
                                student: StudentRoleRequest | None = None, tutor: TutorRoleRequest | None = None
class PersonUpdateRequest:      firstname: Name | MISSING, lastname: Name | MISSING
class CompanyCreateRequest:     name: Name, contact_infos: list[ContactInfoCreateRequest] = []
class CompanyUpdateRequest:     name: Name | MISSING
class StudentRoleRequest:       preferred_meeting_tool: PreferredMeetingTool, subject_ids: set[int] = set()
class TutorRoleRequest:         subject_ids: set[int] = set()
class ContactInfoCreateRequest: type: ContactInfoType, value: str, label: Name | None = None
class ContactInfoUpdateRequest: value: str | MISSING, label: Name | None | MISSING     # type is immutable
class SubjectCreateRequest:     title: Name
class SubjectUpdateRequest:     title: Name | MISSING
```

- Contact values are normalized by one function, `normalize_contact_value(type, value)` in `inputs.py`: an
  `email` is validated as `EmailStr` and lowercased; a `phone` becomes its E.164 form (`+491711234567`, a national
  form is read as a German number - P1-4; until then it only lost its whitespace).
  It raises `ValueError`.
  - **On create** the type is in the body, so a `model_validator(mode="after")` calls it and Pydantic turns the
    `ValueError` into the validation 422 with a field path (rule I-2).
  - **On update** the type is immutable and only known from the stored row, so the check needs the database and
    belongs to the service: `update_contact_info` calls the same function and raises `InvalidContactValueError`.
  - The services normalize on every write anyway, so in-process callers (ADR 0007) cannot store a raw value.
- A create body containing the same `(type, value)` twice is rejected by a model validator (framework 422 with
  the field path), never by the database.

## Error catalog

All classes live in `app/services/crm/errors.py`, derive from a category of
[`errors.py`](../../app/core/errors.py), and get their `code` from the class name. There is no `CrmServiceError`
marker base: the handlers are global, nobody catches by domain.

| Class                           | Category                | Public `message`                                | `expose_message`              |
| ------------------------------- | ----------------------- | ----------------------------------------------- | ----------------------------- |
| `PartyNotFoundError`            | `NotFoundError`         | Party not found                                 | no                            |
| `PersonNotFoundError`           | `NotFoundError`         | Person not found                                | no                            |
| `CompanyNotFoundError`          | `NotFoundError`         | Company not found                               | no                            |
| `RoleNotFoundError`             | `NotFoundError`         | Role not assigned                               | no                            |
| `ContactInfoNotFoundError`      | `NotFoundError`         | Contact info not found                          | no                            |
| `PartyRelationNotFoundError`    | `NotFoundError`         | Party relation not found                        | no                            |
| `RelatedPartyNotFoundError`     | `NotFoundError`         | Related party not found                         | no                            |
| `SubjectNotFoundError`          | `NotFoundError`         | Subject not found                               | no                            |
| `ContactInfoAlreadyExistsError` | `ConflictError`         | Contact info already exists for this party      | no                            |
| `SubjectAlreadyExistsError`     | `ConflictError`         | Subject already exists                          | no                            |
| `SubjectInUseError`             | `ConflictError`         | Subject is still assigned to students or tutors | no                            |
| `PartyInUseError`               | `ConflictError`         | Party is linked to external systems             | yes - names the link kinds    |
| `UnknownSubjectError`           | `DomainValidationError` | Unknown subject                                 | yes - lists the unknown IDs   |
| `InvalidPartyRelationError`     | `DomainValidationError` | Invalid party relation                          | yes - names the violated rule |
| `InvalidContactValueError`      | `DomainValidationError` | Value is not valid for this contact info type   | no                            |

Per route (401 / 403 / the validation 422 are derived and never declared):

| Route                                    | 404                                          | 409                           | 422 (domain)             |
| ---------------------------------------- | -------------------------------------------- | ----------------------------- | ------------------------ |
| `GET /parties/{id}`, `GET .../relations` | `party_not_found`                            |                               |                          |
| `DELETE /parties/{id}`                   | `party_not_found`                            | `party_in_use`                |                          |
| `POST /persons`                          |                                              |                               | `unknown_subject`        |
| `PATCH /persons/{id}`                    | `person_not_found`                           |                               |                          |
| `PATCH /companies/{id}`                  | `company_not_found`                          |                               |                          |
| `PUT .../student`, `PUT .../tutor`       | `person_not_found`                           |                               | `unknown_subject`        |
| `DELETE .../student`, `DELETE .../tutor` | `person_not_found`, `role_not_found`         |                               |                          |
| `POST .../contact-infos`                 | `party_not_found`                            | `contact_info_already_exists` |                          |
| `PATCH .../contact-infos/{cid}`          | `contact_info_not_found`                     | `contact_info_already_exists` | `invalid_contact_value`  |
| `DELETE .../contact-infos/{cid}`         | `contact_info_not_found`                     |                               |                          |
| `PUT .../relations/{type}/{to}`          | `party_not_found`, `related_party_not_found` |                               | `invalid_party_relation` |
| `DELETE .../relations/{type}/{to}`       | `party_relation_not_found`                   |                               |                          |
| `POST /subjects`                         |                                              | `subject_already_exists`      |                          |
| `PATCH /subjects/{id}`                   | `subject_not_found`                          | `subject_already_exists`      |                          |
| `DELETE /subjects/{id}`                  | `subject_not_found`                          | `subject_in_use`              |                          |

- A company's ID under `/persons/{party_id}` is `person_not_found` (and vice versa): from that collection's point
  of view it does not exist. This also answers "a role on a company".
- A contact info is looked up by `(party_id, contact_info_id)` in one query, so an ID belonging to another party is
  a plain 404 and leaks nothing.
- Uniqueness is decided by the database constraint, not by check-then-insert: `begin_nested()` + `flush()` +
  translating `IntegrityError`, exactly as `upsert_command_env` and `link_discord_account` do. The explicit `flush()`
  forces the violation inside the service instead of at commit.

**Relation rules** (`invalid_party_relation`; additionally `from != to` for every type):

| `type`      | `from` (party in the path)    | `to`                            |
| ----------- | ----------------------------- | ------------------------------- |
| `parent_of` | person                        | person                          |
| `tutor_of`  | person holding the tutor role | person holding the student role |
| `pays_for`  | person or company             | person                          |

## List and search

```python
class PartyListParams(PageParams):
    type: PartyType | None = None
    role: PartyRole | None = None
    subject_id: int | None = None
    q: str | None = Field(None, min_length=2)
    updated_since: AwareDatetime | None = None  # P1-3


class RelationListParams(PageParams):
    direction: RelationDirection | None = None
    type: PartyRelationType | None = None
```

- `q` is split on whitespace; **every** token must match, case-insensitively as a substring, **any** of
  `firstname`, `lastname`, the company `name`, or a contact info `value`.
- `subject_id` without `role` means "has the subject in any role"; with `role` it is restricted to that role.
- Filters never error: `type=company&role=student` or an unknown `subject_id` is an empty page.
- `updated_since` (P1-3) keeps the parties with `updated_at >= updated_since`, the boundary included. It is the
  pull side of decision H and has two limits a consumer must know: a **deleted** party is invisible to it (there is
  no soft delete), and `updated_at` is the **start** of the writing transaction (`now()`), so a change can become
  visible after a later timestamp has already been seen - poll with an overlap, not from the newest `updated_at`.
- Fixed order as verified above. `GET /subjects` takes the bare `PageQuery` and orders by `lower(title), id`.
  Relations order by `created_at, type, other party id`.
- All query parameters of an endpoint live in its one model (the query-model trap of `api-conventions.md`).

## Service layer

```
app/core/db/dependencies.py          DBSession with scope="function"; re-exported from app.api.v1.common
app/api/v1/crm/
  __init__.py                        router: prefix /crm, tag crm
  params.py                          PartyId, SubjectId, ContactInfoId, PartyListQuery, RelationListQuery,
                                     CrmReadAccess, VisibleParty
  schemas.py                         read and write models, from_model mappers, party_detail()
  subjects.py  parties.py  persons.py  companies.py  roles.py  contact_infos.py  relations.py
app/services/crm/
  inputs.py                          PartyRole, RelationDirection, NewContactInfo, StudentRoleData, TutorRoleData,
                                     normalize_contact_value
  errors.py
  parties.py                         PARTY_GRAPH, load_party, list_parties, delete_party, saved
  persons.py  companies.py  roles.py  contact_infos.py  relations.py  subjects.py
```

- **Shape.** Function modules as in the bot domain: `session` first, the rest keyword-only, no commits, no imports
  from `app.api`. Party writes return the reloaded `Party`; child writes return the child.
- **One loading path.** `PARTY_GRAPH` is the tuple of `selectinload` options covering person -> student/tutor ->
  subjects, company and contact infos. `load_party(session, party_id)` applies it with `populate_existing=True` and
  raises `PartyNotFoundError`. `list_parties` applies the same options. **`from_model` may touch only what
  `PARTY_GRAPH` loads.**
- **Every write ends the same way:** mutate, `await saved(session, party_id)` (one `UPDATE core.party SET
updated_at = now()` for the given IDs, then `flush()`), `return await load_party(session, party_id)`. Relations
  call `saved(session, from_id, to_id)`.
- **The PATCH bridge.** The endpoint calls `update_person(session, party_id, **request.model_dump())`. `MISSING`
  fields are absent from the dump, so only sent fields arrive; the service signature uses `None` for "unchanged",
  which is unambiguous because those columns are `NOT NULL`. The single nullable field, `contact_info.label`, uses
  the service-level `UNSET` sentinel (an `Enum` member in [`unset.py`](../../app/core/unset.py), shared with the auth
  services) instead of a boolean flag like `update_description`.
- **Subject sets.** Compute the difference (add the missing rows, delete the surplus ones) instead of reassigning the
  collection: the difference is what tells a write from a `PUT` that changes nothing. (The original rationale - a
  reassigned collection would delete and re-insert unchanged rows - does not hold for a loaded collection:
  SQLAlchemy turns a deleted and a pending object with the same key into no statement.) `unknown_subject` is one
  `select(Subject.id).where(Subject.id.in_(ids))` plus a set difference; the `detail` lists the unknown IDs sorted.
- **Transactions.** The request-scoped transaction of `get_db_session` makes every route atomic, including the
  nested `POST /persons`.

## Schema change

`core.subject.title` gets a unique functional index `uq_subject_title_lower` on `lower(title)` (model
`__table_args__`, an explicit Alembic revision, and the entry in
[`DATABASE_SCHEMA.md`](../DATABASE_SCHEMA.md)). No other schema change.

## Requirements

### Must-have (P0)

**Standing criteria - they hold for every slice and are ticked with P0-6.** They carry over the acceptance
criteria of P0-5 in `api-conventions.md`, which this spec supersedes.

- [x] Under `app/api/v1/crm/` there is no `HTTPException`, no `try/except` around a service call, no inline
      `Annotated[...]` in an endpoint signature and no parameter named `_`. Path and query vocabulary comes from
      `params.py`; its path parameters carry a description and `examples=[...]`.
- [x] Every property of every CRM schema and every CRM path and query parameter has a description in
      `openapi.json` (a test over `app.openapi()`).
- _Proven by:_ `test_the_crm_endpoints_carry_no_boilerplate` in
  [`test_crm_architecture.py`](../../tests/api/test_crm_architecture.py) (an AST check over all 21 endpoints, with a
  probe that it catches each kind) and the description and `examples` tests in
  [`test_crm_openapi.py`](../../tests/api/test_crm_openapi.py), which walk every schema reachable from a CRM
  operation. "No inline `Annotated[...]`" is checked positively: every endpoint parameter is a bare name imported
  from `params.py`, `schemas.py` or `app.api.v1.common` (plus FastAPI's `Response`) and has no default - so an
  inline `= Path(...)` or a `Depends(get_db_session)` that bypasses `DBSession` (decision N) fails the check too.

**P0-1 - Foundation and subjects.** _The whole stack once, on the simplest resource._

- _Technique:_ `Scope.CRM_READ` / `Scope.CRM_WRITE` as `(value, description)`; the `crm` entry in `OPENAPI_TAGS`
  (it already exists); the
  CRM router included in [`router.py`](../../app/api/v1/router.py); the `DBSession` alias (decision N);
  `app/services/crm/errors.py`; the four subject routes with `SubjectResponse`, `Page[SubjectResponse]`, the
  `MISSING`-based update model, and the schema change above.
- _Acceptance criteria:_
  - [x] Both scopes appear with their description in `components.securitySchemes`; every CRM operation ID matches
        `^crm_[a-z_]+$`; reads require `crm:read`, writes `crm:write` (asserted over `app.openapi()`).
  - [x] Given a session dependency whose exit raises, when a CRM write route is called, then the client receives
        the 500 envelope, not a 2xx.
  - [x] `POST /subjects` with a title differing only in case or surrounding whitespace from an existing one is 409
        `subject_already_exists`; the same holds for `PATCH`.
  - [x] `PATCH /subjects/{id}` with `{}` is 200 and changes nothing; with `{"title": null}` it is the validation 422.
  - [x] `DELETE /subjects/{id}` is 409 `subject_in_use` while a `student_subject` or `tutor_subject` row references
        it, and 204 otherwise.
  - [x] `GET /subjects` returns `Page[SubjectResponse]` ordered by `lower(title), id`.
  - [x] An architecture test asserts that nothing under `app/services/crm` or `app/api/v1/crm` imports
        `app.services.bot` or `app.api.v1.bot` (ADR 0007).
  - [x] An error-contract test pins status, `code` and `detail` of every catalog row for the subject routes,
        following [`test_bot_error_contract.py`](../../tests/api/test_bot_error_contract.py). Every later slice
        extends it with its own rows.
  - [x] At runtime a CRM route answers 401 without a token and 403 with a token that lacks the scope, both in the
        error envelope.
  - [x] `just check-all` is green, `openapi.json` is regenerated, and `just test-clients` passes.
- _Deviations:_
  - `SubjectId` is bounded to the Postgres `INTEGER` range (`ge=1`, `le=2**31 - 1`): a larger ID could not be
    bound to the query and would surface as a 500 instead of the validation 422.
  - `errors.py` starts with the three subject classes; every later slice adds the classes of its own routes, so
    `party_not_found` stays unique until P0-2 replaces the bot's class.
  - Tests that run the real app against the database live in `tests/db/crm/` (the `session` fixture is only
    visible under `tests/db/`); its `client` fixture wraps every request in a SAVEPOINT that rolls back on failure,
    mirroring the request-scoped transaction. Stubbed API tests stay in `tests/api/test_crm_*.py`.
  - The 401 / 403 criterion is asserted for **every** CRM operation of the OpenAPI document, not for one route.
  - The savepoint pattern differs from `upsert_command_env` in one point: the change is made **inside**
    `begin_nested()` (`_unique_title` in `subjects.py`). `begin_nested()` first flushes whatever is pending into the
    enclosing transaction, so a change made before it fails out there, no SAVEPOINT is involved and the session
    ends in `PendingRollbackError`. Status, `code` and `detail` are the same either way; found by the first review
    gate. The bot's call sites are left alone (guardrail).

**P0-2 - Party aggregate: create, read, update.**

- _Technique:_ `inputs.py`; `PARTY_GRAPH`, `load_party`, `saved`; `create_person` / `create_company` with nested
  `contact_infos`; `update_person` / `update_company`; the read and write models including the union;
  `POST /persons`, `POST /companies`, `GET /parties/{party_id}`, `PATCH /persons/{party_id}`,
  `PATCH /companies/{party_id}`. `POST` answers 201 with `Location: /api/v1/crm/parties/{id}`. The `student` and
  `tutor` fields of `PersonCreateRequest` arrive with P0-4; until then the detail shows both roles as `null`.
- _Technique:_ `PartyNotFoundError` is defined in `app/services/crm/errors.py`; the bot's own class is deleted and
  [`errors.py`](../../app/services/bot/errors.py) imports the CRM one. The class thereby loses `BotServiceError` as a
  base, which nothing relies on: the only `except BotServiceError` is `requeue` in the dead-letter CLI, which
  never sees it. Status, `code` and `detail` ("Party not found") stay identical, so the bot's contract test keeps
  passing unchanged. This closes the open checkbox of P1-1 in
  `api-conventions.md`. In the same PR, mark P0-5 there as superseded by this spec and repoint its other mentions
  (the "reference implementation" note under P1-6, the boilerplate metric, the phasing list) to `crm-api.md`.
- _Acceptance criteria:_
  - [x] In `openapi.json`, `PartyDetail` is `oneOf` `PersonDetail` / `CompanyDetail` with a `type` discriminator and
        is referenced by exactly one operation, `crm_get_party`.
  - [x] **Reload rule:** for every write service function, a DB test calls it and maps the result with
        `party_detail()` without `MissingGreenlet` - including a freshly created person without roles and a
        person right after an update.
  - [x] Given a party whose `updated_at` lies in the past, when any write service touches its aggregate, then
        `party.updated_at` moves forward, and the detail's `updated_at` equals it.
  - [x] `PATCH /persons/{company_id}` is 404 `person_not_found`; `PATCH /companies/{person_id}` is 404
        `company_not_found`; `GET /parties/{unknown}` is 404 `party_not_found`.
  - [x] Names are stripped; a blank name and an explicit `null` on update are the validation 422.
  - [x] A create body with the same `(type, value)` contact info twice is the validation 422 with a field path.
        An e-mail is stored lowercased, an invalid one is 422; a phone value is stored without whitespace
        (in E.164 since P1-4).
  - [x] The create request schemas carry `examples`; `just test-clients` still passes with the union in place.
  - [x] [`test_error_taxonomy.py`](../../tests/api/test_error_taxonomy.py) passes with a single `party_not_found`.
- _Deviations:_
  - The contact value is normalized by a `field_validator("value")` reading the already validated `type`, not by a
    `model_validator(mode="after")`: the error then points at `["body", "contact_infos", 0, "value"]` instead of
    at the whole item. The duplicate check is an `AfterValidator` on the list (`["body", "contact_infos"]`).
  - **An update with nothing to change is not a write:** `PATCH` with `{}` leaves `party.updated_at` alone, so
    "`{}` changes nothing" holds literally. Every real write still ends with `saved(...)` and `load_party(...)`.
  - The row for `PartyNotFoundError` moved from the class table in `tests/test_bot_errors.py` (which asserts
    `BotServiceError` as a base) to the CRM catalog in `tests/api/test_crm_error_contract.py`; the bot's route
    contract test is unchanged.
  - `contact_infos` in the detail are ordered by `(type, value)`, role `subjects` by `lower(title), id`: the
    relationships have no order of their own.
  - **Text Postgres cannot store is a validation 422** (error rule I-2): `Name` and `normalize_contact_value`
    reject U+0000 and a lone UTF-16 surrogate through `require_storable_text` in `inputs.py`. Left to the database
    both were a 500, and the driver's message for the surrogate repeated the phone number into the request log.
    Found by the first review gate. `openapi.json` is unchanged by it.
  - Outside the CRM packages: the engine runs with `hide_parameters=True`, so a logged database error does not
    repeat names or contact values. Postgres' own `DETAIL` of a unique violation still names the key; the services
    translate those inside a SAVEPOINT, so only an untranslated `IntegrityError` would show it.

**P0-3 - List, search and guarded delete.**

- _Technique:_ `list_parties` and `PartyListParams` as specified under "List and search"; `delete_party` with the
  guard of decision K, checking `ext.discord_account`, `ext.sevdesk_contact`, `ext.clockodo_customer`,
  `ext.clockodo_project`, `ext.microsoft_account` and `ext.microsoft_contact`.
- _Acceptance criteria:_
  - [x] `q=max muster` finds "Max Mustermann" and not "Max Meier"; `q=<part of an e-mail>` finds the owner.
  - [x] `type=company&role=student` and an unknown `subject_id` return an empty page with `total == 0`, not an error;
        an unknown query parameter is the validation 422.
  - [x] Items are ordered case-insensitively across persons and companies; `total` counts the filtered set.
  - [x] `limit` and `offset` page through that order without gaps or repeats, and the page echoes both values.
  - [x] `roles` is correct for a person with none, one and both roles; the number of SQL statements for a page does
        not grow with the page size (asserted with a statement counter).
  - [x] `DELETE` is 409 `party_in_use` with a `detail` naming the link kinds while any of the six links exists, and
        204 otherwise, after which roles, contact infos and relations of the party are gone.
- _Deviations:_
  - `subject_id` is bounded to the Postgres `INTEGER` range like `SubjectId`, and `q` rejects unstorable text
    (`require_storable_text`): both are the validation 422 instead of a driver error behind a 500. An *unknown* but
    well-formed `subject_id` is still an empty page. `LIKE` wildcards in `q` are matched literally (`autoescape`).
  - `delete_party` locks the party row (`FOR UPDATE`) before the guard looks for links. All six foreign keys into
    `core.party` are `ON DELETE CASCADE`, so without the lock a link created between check and delete would be
    deleted silently. The delete itself is one Core `DELETE`; the ORM cascade would have loaded - and thereby
    touched - the `ext` relationships.
  - Deleting a party calls `saved(...)` for the parties on the other side of its relations: their aggregate
    changed (decision H, "relations touch both parties").
  - The order and the `lower(...)` of the list are asserted on the emitted SQL as well as on the data: the test
    database collates case-insensitively, so the data alone cannot prove it.
  - The role rows of these tests are built through the ORM; the role routes arrive with P0-4.
  - `offset` got an upper bound (`MAX_PAGE_OFFSET`, the `INTEGER` range) in the shared `PageParams`: a value beyond
    int64 could not be bound and was a 500 on every list, including the bot's and the auth clients' (the class came
    from `main` unbounded). `openapi.json` gains a `maximum` on `offset` for all six paged operations. Found by the
    second review gate.
  - `q` is at most 200 characters: every word becomes four bind parameters and a statement takes 32767 of them, so
    an unbounded `q` was a 500 (and a megabyte-sized log line). Found by the second review gate.

**P0-4 - Roles.**

- _Technique:_ `put_student_role`, `remove_student_role`, `put_tutor_role`, `remove_tutor_role` in `roles.py`; the
  nested `student` / `tutor` of `POST /persons` reuse the same service functions.
- _Acceptance criteria:_
  - [x] `PUT` creates the role or replaces its data; sending the same body twice answers 200 twice with the same
        representation. A person can hold both roles at once.
  - [x] `subject_ids` replaces the set; an unknown ID is 422 `unknown_subject` whose `detail` lists the unknown IDs,
        and nothing is written (also for the nested create, which then creates no party at all).
  - [x] `DELETE` of a role that is not assigned is 404 `role_not_found`; a company's ID is 404 `person_not_found`.
  - [x] Removing a role never inspects bot state (ADR 0007) and leaves relations untouched.
- _Deviations:_
  - **A `PUT` that changes nothing is not a write** (same rule as the empty `PATCH` of P0-2): `updated_at` stays,
    so "200 twice with the same representation" also holds when the two requests are two transactions.
  - The nested create reuses `apply_student_role` / `apply_tutor_role` and `require_subjects` - the functions the
    `PUT` services are built from - rather than calling `put_student_role` itself, which needs a stored person.
    The subjects of both roles are checked together before the party is added, so the `detail` lists the unknown
    IDs of both.
  - The roles service finds its person through `load_party` (the one loading path) and turns a missing party or a
    company into `person_not_found`; it needs the roles and their subject rows loaded to compute the difference.
  - `subject_ids` are bounded to the Postgres `INTEGER` range (validation 422), like `SubjectId`.
  - **Role writes are serialized per person:** `_load_person` locks the party row (`FOR NO KEY UPDATE`, the lock
    `saved` takes anyway) before it loads. Without it two overlapping `PUT`s of the same role - a client retrying -
    both found no role, both inserted it, and the loser's primary-key violation was a 500. Now the second one
    waits, sees the role and changes nothing. Proven with two real transactions in
    `test_crm_roles_concurrency.py`. Found by the second review gate.
  - `subject_ids` holds at most 100 IDs (validation 422): each becomes a bind parameter.
  - `require_subjects` lives in `subjects.py`; "never inspects bot state" is asserted on the emitted SQL (no
    statement names the `bot` or `ext` schema or `party_relation`).

**P0-5 - Contact infos.**

- _Technique:_ `add_contact_info`, `update_contact_info`, `remove_contact_info`; the savepoint pattern for
  `uq_contact_info`; `normalize_contact_value` shared by the create validator and the services (see
  "Representations"). The update model only requires a non-empty `value`; `update_contact_info` normalizes it
  against the stored `type` and raises `InvalidContactValueError` when that fails.
- _Acceptance criteria:_
  - [x] `POST` answers 201 with `ContactInfoResponse`; a duplicate `(type, value)` for the party is 409
        `contact_info_already_exists`, also when an update changes the value into an existing one.
  - [x] `PATCH` cannot change `type`; `{"label": null}` clears the label; `{}` changes nothing.
  - [x] `PATCH` of an e-mail contact info with a non-e-mail `value` is 422 `invalid_contact_value`; with a valid,
        differently cased one it is 200 and the stored value is lowercased. On `POST`, the same invalid value is
        the validation 422 with a field path instead.
  - [x] A `contact_info_id` belonging to another party is 404 `contact_info_not_found` for `PATCH` and `DELETE`.
  - [x] Every contact info write moves the party's `updated_at`.
- _Deviations:_
  - The savepoint makes its change **inside** `begin_nested()` (`_unique_per_party`), as `_unique_title` does since
    the first review gate; see the deviation under P0-1.
  - "`PATCH` cannot change `type`": the field is not part of `ContactInfoUpdateRequest`, and FastAPI ignores unknown
    body fields, so a `type` that is sent has no effect (asserted); it is not a 422.
  - "`{}` changes nothing" includes `updated_at` (the rule of P0-2); a refused write leaves it alone as well.
  - The services translate a `ValueError` of `normalize_contact_value` into `InvalidContactValueError` on every
    path, and `new_contact_infos` rejects duplicates of a nested create with `ContactInfoAlreadyExistsError`. Both
    are unreachable over HTTP on the create routes (the request models catch them first, as the validation 422) and
    therefore not declared there; they are what in-process callers get instead of a driver error.
  - The update model's `value` also rejects unstorable text as the validation 422 (see P0-2).
  - **`normalize_contact_value` is a fixed point:** it lowercases *before* validating, so what it returns is the
    validated, NFC-normalized form. The first version lowercased afterwards, and because the create routes
    normalize twice (request model, then service) the second pass could differ: `J` + caron became U+01F0 (two
    spellings the model saw as distinct collided in the service - an undeclared 409), and U+0130 grows when
    lowercased (an undeclared 422). That made the "unreachable over HTTP" sentence above false; with the fixed
    point it holds, and `POST` and `PATCH` store the same form. Found by the second review gate.
  - A contact value is at most 254 characters (`MAX_CONTACT_VALUE_LENGTH`, the longest e-mail address): it sits in
    the index of `uq_contact_info`, whose rows Postgres limits, so an unbounded value was a 500 on all three
    write routes.

**P0-6 - Relations and the reference flow.**

- _Technique:_ `list_relations`, `put_relation`, `remove_relation`; the rules table; `RelationResponse` built
  relative to the party in the path; `saved(session, from_id, to_id)`.
- _Acceptance criteria:_
  - [x] Each rule of the table has a passing and a failing case; a failing one is 422 `invalid_party_relation` with a
        `detail` naming the rule. `from == to` is rejected for every type.
  - [x] An unknown `party_id` is 404 `party_not_found`, an unknown `to_party_id` is 404 `related_party_not_found`.
  - [x] `PUT` is idempotent (200 twice); `DELETE` of a missing relation is 404 `party_relation_not_found`.
  - [x] `GET .../relations` returns both directions by default; from the child's side a `parent_of` relation shows
        `direction == "incoming"` and the parent as `party`.
  - [x] **End to end:** an API test performs the four-call reference flow and then reads the student's detail and
        relations.
  - [x] Docs: a CRM section in [`ARCHITECTURE.md`](../ARCHITECTURE.md), the layout in
        [`CLAUDE.md`](../../CLAUDE.md), and this spec's checkboxes ticked.
- _Deviations:_
  - `put_relation` is one `INSERT ... ON CONFLICT DO NOTHING`: idempotent also for two racing requests, without a
    savepoint. **An existing relation is not a write** (the rule of P0-2 and P0-4), so the repeated `PUT` leaves
    both `updated_at` alone and answers with the same representation.
  - The services return `PartyRelationView` (defined in `relations.py`, like the bot's `views.py`): the relation
    relative to one party, with the other side loaded through `PARTY_GRAPH`. `RelationResponse.from_view` maps it;
    a page loads all other sides in one statement.
  - The rule messages: "a party cannot be related to itself", "parent_of must start at / point to a person",
    "tutor_of must start at a person holding the tutor role", "tutor_of must point to a person holding the
    student role", "pays_for must point to a person" - each prefixed with "Invalid party relation: ".
  - `DELETE` looks only for the relation: unknown parties are `party_relation_not_found` too, as the route table
    says.
  - The order `created_at, type, other party id` is asserted on the emitted SQL: inside one test transaction all
    `created_at` are equal.
  - Now that the surface is complete, the closed items are pinned by tests: the route map with its operation IDs
    and success statuses (`test_crm_openapi.py`) and the 15 catalog classes (`test_crm_error_contract.py`).


### Nice-to-have (P1)

Four independent requirements in three domains. The rules for implementing agents apply to each; every slice ends
with `just check-all` green, `openapi.json` regenerated where the API changed, `just test-clients` passing, and its
own `docs(specs): tick P1-x` commit. Only P1-3 and P1-4 touch the CRM packages.

**P1-1 - `GET /api/v1/auth/me`.** _What does this token say about me? Useful to verify a token in Swagger UI. Belongs
to the `auth` domain, not to the CRM._

- _Technique:_ `get_me` in a new `app/api/v1/auth/me.py`, included in the `auth` router. The principal comes from a
  parameter typed `Annotated[Principal, require_scopes()]`: no scope, so any valid token passes, and the 401 is
  still derived by the OpenAPI hook. `MeResponse(ApiModel)` in the auth [`schemas.py`](../../app/api/v1/auth/schemas.py)
  carries `principal_type`, `client_id: str | None` and `scopes: list[str]` (sorted). The route reads no database: it
  reports what the token carries, also for a client that was suspended after the token was issued.
- _Acceptance criteria:_
  - [x] With a valid token the route answers 200 with the token's principal type, client ID and its scopes sorted.
  - [x] Without a token and with an invalid one it is 401 in the error envelope.
  - [x] In `openapi.json` the operation ID is `auth_get_me`, the security requirement lists no scopes, the 401 is
        derived, and every property of `MeResponse` has a description.
  - [x] The handler depends on the principal only - no session, no settings.
- _Deviations:_
  - **A route that needs a token but no scope documents no 403.** Any valid token passes it, so the response cannot
    occur; `_document_auth_errors` in [`openapi.py`](../../app/api/v1/common/openapi.py) now derives the 403 only
    where the security requirement names a scope. Before, the hook had a fallback description for this case
    ("Not enough permissions") while the contract test demanded a scope-naming 403 from every guarded operation -
    the two disagreed, and this route is the first to hit it. `test_openapi_contract.py` asserts both directions.
    `openapi.json` is unchanged for every existing operation.

**P1-2 - Bot follow-ups of ADR 0007.** _Bot-domain and model work in three commits (a, b, c). The CRM packages stay
untouched, and the dependency keeps its direction: the bot reads the CRM, never the reverse._

- _Technique (a) - `Party.discord_accounts`:_ the relationship in [`party.py`](../../app/core/db/models/core/party.py)
  becomes `discord_accounts: Mapped[list[DiscordAccount]]`, ordered primary first, then by `discord_id`;
  `DiscordAccount.party` back-populates it. `ExternalAccountsProfile.discord` becomes `list[DiscordAccountProfile]`
  and `load_parties_for_discord_ids` loads the collection. No migration: `ext.discord_account` always allowed several
  rows per party (its key is `discord_id`; only "one primary active account per party" is unique), which the scalar
  relationship answered with a warning and an arbitrary row. The link kind `"discord_account"` in `EXTERNAL_LINKS`
  names the table, is part of the `party_in_use` contract, and stays.
- _Technique (b) - one party graph:_ `load_parties_for_discord_ids` in
  [`profile.py`](../../app/services/bot/profile.py) applies `*PARTY_GRAPH` plus what only the bot's profile touches
  (both relation collections, `discord_accounts`, `microsoft_account`). `PARTY_GRAPH` itself is unchanged: no CRM
  mapper touches the extras.
- _Technique (c) - `TUTOR_OF`:_ a helper `_require_tutor_of` in
  [`transitions.py`](../../app/services/bot/transitions.py), called by `prepare_student_activation` with its other
  validations and therefore before the replay lookup. It resolves both Discord IDs to parties through **active**
  `ext.discord_account` rows (a deactivated link counts as unlinked; any active account of a party qualifies, primary
  or not) and requires a `tutor_of` relation from the tutor's party to the student's. Each failure is the existing
  `TransitionValidationError` (422 `transition_validation`) with its own message: "Student is not linked to a party",
  "Tutor is not linked to a party", "Tutor is not assigned to this student". It is a plain read: no lock, no write.
  `commit_student_activation` does not check again - a relation removed between the two phases is divergence, and
  reconciling that is later bot work (ADR 0007).
- _Technique:_ [ADR 0007](../decisions/0007-crm-system-of-record.md) marks its three follow-ups as done and says what
  "no synchronous cross-checks" rules out: the CRM asking the bot. The bot validating its own transition against the
  intended state is a consumer reading the CRM.
- _Acceptance criteria:_
  - [x] (a) Given a party with two linked accounts, its profile lists both, the primary one first, and loading it
        raises no SQLAlchemy warning; a party without an account has `external_accounts.discord == []`.
  - [x] (a) `party_in_use` still names `discord_account`; the CRM's contract tests pass unchanged.
  - [x] (b) The profile of a person holding both roles, and of a company, maps without `MissingGreenlet`; that the
        loader is built on `PARTY_GRAPH` is asserted on the emitted SQL (it loads `core.company`, which only
        `PARTY_GRAPH` contributes).
  - [x] (c) With both accounts linked and `tutor_of` from tutor to student in place, prepare and commit work as
        before; a non-primary active account is enough.
  - [x] (c) Without the relation - or with only the inverse one - prepare is 422 "Tutor is not assigned to this
        student"; a student or tutor without a link, or with only a deactivated one, is 422 with the message of that
        side. Nothing is written in any of these cases.
  - [x] (c) A retried prepare after the relation was removed is the 422, not a replay; a commit after the relation
        was removed still succeeds.
  - [x] (c) The three messages are pinned over HTTP next to the existing rows of
        [`test_bot_error_contract.py`](../../tests/api/test_bot_error_contract.py); the bot's error catalog does not
        grow.
  - [x] The architecture test of P0-1 stays green: nothing under the CRM packages imports the bot.
- _Deviations:_
  - (a) The order of the collection is part of the relationship (`order_by` on `Party.discord_accounts`), so every
    loader gets it, not only the bot's.
  - (b) The `statements` fixture moved from `tests/db/crm/conftest.py` up to `tests/db/conftest.py`: the bot's
    loader test needs it too.
  - (c) The check sits between `_require_active_user` and `_lock_tutor_workspace`: a pair the CRM does not know
    fails before the tutor's workspace row is locked. That it only reads is asserted on the emitted SQL (every
    statement against `core` or `ext` is a `SELECT` without `FOR ...`).
  - (c) A relation of another type between the two parties (`pays_for`) does not count, and neither does
    `tutor_of` in the inverse direction - both have their own test.
  - (c) Every Discord user of `test_bot_transitions_service.py` is linked to a party now (`_add_user`), and a
    student that gets activated is assigned to the tutor(s) the test names (`_add_student`). The tests that commit
    for real delete their parties again; the Discord accounts and relations cascade.
  - (c) The three messages over HTTP are rows of the stubbed contract test, which proves that the reason reaches
    the client unchanged (`expose_message`); that the service raises exactly these reasons is pinned by the DB
    tests (`match=`).

**P1-3 - `updated_since` filter on `GET /parties`.** _The pull-based change signal of decision H. The spec tied it to
a polling consumer; it is built ahead of one because it is one filter._

- _Technique:_ `updated_since: AwareDatetime | None` on `PartyListParams` with a description and an example in `Z`
  notation (a literal `+` in a query string decodes as a space); `list_parties(..., updated_since=None)` adds
  `Party.updated_at >= updated_since`. The boundary is included so that a poller sees a change twice rather than
  never. Order, paging and the other filters are unchanged. Two limits go into "List and search": a deleted party
  is invisible to the filter (no soft delete), and `updated_at` is the **start** of the writing transaction
  (`now()`), so a consumer must poll with an overlap instead of a moving cursor.
- _Acceptance criteria:_
  - [x] `updated_since=<t>` returns exactly the parties with `updated_at >= t`, the boundary included; it combines
        with the other filters by AND, and `total` counts the filtered set.
  - [x] A timestamp in the future is an empty page, not an error; a timestamp without an offset and a malformed one
        are the validation 422.
  - [x] A write anywhere in the aggregate makes its party appear: shown for a contact info write and for the party
        on the other side of a new relation.
  - [x] The parameter has its description in `openapi.json` (standing criterion).
- _Deviations:_
  - A timestamp without an offset is told apart from an unknown parameter by the `type` of the validation error
    (`timezone_aware`): both are a 422 at `["query", "updated_since"]`, so the location alone would have passed
    before the parameter existed.
  - The boundary is tested one microsecond to either side, on `updated_at` values written directly: inside one test
    transaction every `now()` is the same instant.
  - The description of the parameter carries both limits, so the generated client and Swagger UI show them too.

**P1-4 - Phone numbers in E.164.**

- _Technique:_ the `phone` branch of `normalize_contact_value` in [`inputs.py`](../../app/services/crm/inputs.py)
  parses with `phonenumbers` (new dependency) and `DEFAULT_PHONE_REGION = "DE"`, and returns the E.164 form. A value
  is rejected when it contains a letter (the parser would silently convert or drop it), when it does not parse,
  when it is not `IS_POSSIBLE` for its region (`IS_POSSIBLE_LOCAL_ONLY` is not enough: a number without its area
  code has no E.164 form), or when it carries an extension (E.164 has none and formatting would drop it silently;
  the extension belongs into `label`). The check is "possible", deliberately not "valid": number ranges are opened
  faster than metadata ships, and a real number that cannot be stored is worse than a typo that can. Everything else
  stays: the function raises `ValueError` with messages that never repeat the value, it is a fixed point, and
  nothing outside `inputs.py` knows the library. There is no data migration - nothing is live; a value stored before
  this slice keeps its form until it is next written.
- _Technique:_ the descriptions of the three `value` fields name E.164 and the default region; the description of
  `q` says that a phone number is found by its digits without the national leading zero (`171 1234567` or the full
  `+49171...`), since search stays a plain substring match. "Representations" and the non-goals are updated.
- _Acceptance criteria:_
  - [x] `0171 1234567`, `+49 171 1234567`, `0049 171 1234567`, `+49 (0)171 1234567` and `0171/1234567` are all
        stored as `+491711234567`; `+1 650 253 0000` is stored as `+16502530000`.
  - [x] **Fixed point:** for every accepted sample, normalizing the result again returns it; `POST` and `PATCH`
        store the same form.
  - [x] A value with a letter, one that is no number, a too long one and a local-only one (`112`) are invalid: the
        validation 422 with a field path on the create routes, 422 `invalid_contact_value` on `PATCH`. A value with
        an extension is rejected with its own message. No message repeats the value.
  - [x] Two spellings of one number in a create body are the validation 422 (duplicate); adding the second spelling
        to a party that has the first is 409 `contact_info_already_exists`.
  - [x] `phonenumbers` is imported by `inputs.py` only.
- _Deviations:_
  - **No string is an e-mail address and a phone number at once any more.** Before, a phone value was any non-blank
    text, so the same value could sit under both types of one party, and two P0 tests showed that the duplicate key
    is `(type, value)`. That case is unreachable now - through the API and through the services, which normalize
    too. The tests became `test_a_value_is_judged_by_its_type` and the duplicate test for two spellings of one
    number; the key in the database is unchanged.
  - The parser knows the space but neither tab nor line break inside a number, so whitespace is collapsed before
    parsing - the P0 promise "whitespace does not matter" still holds.
  - An extension has its own `ValueError` message (it names `label`), but over HTTP `PATCH` answers with the fixed
    `detail` of `invalid_contact_value` like every other invalid value: the catalog says `expose_message: no`. On
    the create routes the message is part of the validation 422.
  - `MAX_CONTACT_VALUE_LENGTH` no longer decides anything for phone numbers (the parser rejects what is too long
    for E.164); it stays as the bound for e-mail addresses, and the "fits exactly" test uses a 254-character
    address now.
  - That `q` finds a number by its stored digits - and not by `0171` - is a test of its own in
    `test_crm_party_list_api.py`, so the sentence in the parameter's description cannot rot.
  - `ruff format` also formats the Python blocks of this document; the alignment of one comment changed with it.

### Future considerations (P2)

- Addresses; archiving, GDPR erasure and anonymization; optimistic concurrency.
- Object-level authorization for user principals: one dependency over the target party of every route.
- Eventing / outbox for consumers; `ext` links in the party detail; `pg_trgm`; role-specific list endpoints.

## Success metrics

- _Operator flow:_ a new student with a paying parent is entered in **4 calls** without database access.
- _Out-of-band SQL to create people:_ **0**.
- _Boilerplate:_ `HTTPException` and `try/except` under `app/api/v1/crm/`: **0**.
- _Contract:_ every row of the error catalog is pinned by a test on status, `code` and `detail`.

## Decided (formerly open questions)

- **First consumer:** the operator in Swagger UI.
- **`MISSING`** over `model_fields_set`, accepting that it is experimental.
- **The CRM leads;** the bot is a special consumer (ADR 0007). `tutor_of` is writable here.
- **Relations are not embedded** in the party detail: they are two-sided and have their own route.
- **Deleting a party** is guarded by `ext` links only; relations cascade.
- **No `CrmServiceError` marker base,** and role "not found" is one class for both roles.
- **An invalid contact value on update is a domain error** (`invalid_contact_value`): the type is only known from
  the stored row, so the check needs the database (rule I-2). On create it stays a Pydantic validation error.
- **P1-2: student activation is strict.** Both sides need an active link to a party and the `tutor_of` relation;
  there is no lenient path for unlinked users. The CRM says what should be true (ADR 0007).
- **P1-2: the profile lists all Discord accounts** of a party instead of picking one. Nothing consumes the field
  yet, so the contract change breaks nobody.
- **P1-4: default region `DE`, "possible" rather than "valid",** extensions and letters rejected rather than dropped.

## Timeline / phasing

`api-conventions` (P0 and P1) is merged to `main`; the CRM branch is created from `main`. The old
`feat/crm` exploration is reference material, not a base. One PR per requirement in the order above, or one branch
that keeps requirement boundaries at its `docs(specs): tick P0-x` commits. The first PR also adds the row for
ADR 0007 to the [decisions index](../decisions/README.md), below the 0006 row that `main` has by then.

1. **P0-1** proves the stack on subjects, which P0-4 needs anyway.
2. **P0-2**, then **P0-3**: from here the operator can create and find people.
3. **P0-4**, **P0-5**, **P0-6** complete the reference flow.

4. **P1** follows on `feat/crm-p1`, branched from `feat/crm-api`, so that P0 can be merged on its own: **P1-1**,
   **P1-2** (a, b, c), **P1-3**, **P1-4**. The four are independent; the order only keeps the bot work together.

**Dependency:** none open - `api-conventions` is on `main` and ADR 0007 is accepted.

## Rules for implementing agents

- The rules of [`api-conventions.md`](api-conventions.md) apply unchanged (English only, symbol references in docs,
  never hand-edit `openapi.json`, `just check` green before every commit, conventional commits).
- Never import the bot domain from the CRM. Never add a check against Discord state to a CRM write.
- Every write service function ends with `saved(...)` and `load_party(...)`; `from_model` touches only what
  `PARTY_GRAPH` loads. If a representation needs more, extend `PARTY_GRAPH`, never add an ad-hoc load.
- The error catalog is closed. If a requirement seems to need a new class or `code`, stop and report.
- Do not touch `ext` tables beyond reading them in the delete guard.
