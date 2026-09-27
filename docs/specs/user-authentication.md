# Spec: User authentication (accounts, grant modes, reach-qualified scopes)

> Status: Implemented (2026-09) | Domain arc `auth`
> Code moved on 2026-09-25: the auth services went from `app/core/auth/services/` to `app/services/auth/` and the
> operator commands from `app/core/auth/bootstrap.py` to `app/cli/bootstrap.py`; the paths below name the new home.
> Tracking: [#85](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/85)
> The bot parts are superseded by [`bot-decoupling.md`](bot-decoupling.md) (2026-09): the bot schema, the grant
> engine and `bot:*` leave SkillForge; the token exchange and Discord links are built there, tutor reach in its own arc.
> Builds on the [project sketch](../PROJECT.md), [`api-conventions.md`](api-conventions.md), goal 4 of
> [`crm-api.md`](crm-api.md) ("exactly one target party per route") and
> [ADR 0008](../decisions/0008-user-authentication-and-reach.md).
> Written to be executed by coding agents: every requirement names its symbols, files and checkable criteria.

## Problem statement

SkillForge authenticates applications only. `POST /auth/token` knows one grant (`client_credentials`), a client
has one list of scope grants, and a scope such as `crm:read` means _every record_.

The project sketch asks for more. People use SkillForge through frontends - the portal acts for the person who
logged in, the bot (in its own arc) for the person who typed a command - and the same person gets the same rights
through either. The frontend is the ceiling, some frontends also work for themselves, and a person may be
restricted to their own data. None of that can be expressed today:

- **Nobody can log in.** A person exists as a `Party`; there is no account, no credential, no session.
- **A client's grants mean one thing.** They are what the client may do for itself; there is no way to say "the
  most this client may do for a person".
- **Nobody can be restricted to their own data.** Handing a student `crm:read` would show them every party.

The first consumer of this arc is the operator in Swagger UI: creating accounts, logging in through the
"Authorize" dialog, watching a restricted token get a filtered answer. The portal follows in its own arc in
skillsite; the bot's use of person tokens follows in the bot arc (see
[Designed for the bot arc](#designed-for-the-bot-arc)).

## Goals

1. **People log in with e-mail and password** through a client and get a token the existing guards understand.
2. **Client grants have a mode**: what a client may do for itself (`application`) and the most it may do for a
   person (`delegated`).
3. **One token model.** A token for a person is issued to a client on behalf of that person; its scopes are
   computed once, at issuance. A request is validated exactly once, the same way for both kinds of token.
4. **One permission system.** Routes keep declaring scopes. "Everything" versus "only mine" is a qualifier on the
   scope, not a second mechanism.
5. **Safe by default.** A restricted person reaches only routes that were deliberately made reach-aware.
6. **The account is the door.** An account belongs to exactly one person party, only admins or a client they
   entrust with it create it, and it is separate from the ways a person logs in.
7. **Ready for the bot.** The model takes the token exchange, Discord as a way to log in and tutor reach. The one
   planned widening: `sid` becomes optional for exchanged tokens, which open no session.

## Non-goals

- **The portal itself** and its backend-for-frontend. This spec only records what SkillForge expects from it
  ([BFF contract](#bff-contract)).
- **Building the bot's side:** token exchange, Discord as a login method, tutor reach, account creation by tutors,
  retiring the bot's `PermissionGrant` engine. Designed here, built in the bot arc.
- **Sending e-mail.** The operator receives one-time tokens in the API response (P1-1). Until the portal exists,
  redeeming is an operator action too, so the operator sets every initial password
  ([Operating without a portal](#operating-without-a-portal)).
- **Self-signup** and self-service for people (P1-2).
- **Reach-aware writes** (`crm:write:own`). Restricted people are read-only until P1-4.
- **Federated login beyond Discord, MFA, asymmetric signing, JWKS, token introspection.**
- **Instant revocation of access tokens.** They stay stateless (decision L).
- **Any change to the `bot` schema or the grant engine.** _(Superseded by [bot-decoupling.md](bot-decoupling.md).)_

## Decisions

| Topic                                    | Decision                                                                                                                                                                                                                                                  | Rationale                                                                                                                                                                                                             |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A - Identity provider**                | SkillForge itself. Accounts, hashes and sessions live in the `auth` schema.                                                                                                                                                                               | No second service, no second user store (ADR 0008).                                                                                                                                                                   |
| **B - Password**                         | Argon2 via `hash_secret` in [`secrets.py`](../../app/core/auth/secrets.py); 8 to 128 characters, no composition rules, no list of forbidden passwords.                                                                                                    | One hashing home; a length rule people can live with. The portal's per-IP limit is the only brake on guessing across accounts; MFA for stored roles is P2.                                                            |
| **C - Account origin**                   | Accounts are created through `auth:users:manage` only: by admins, or by a client an admin grants it in `application` mode (an automated intake, for instance). Always for an existing person party. No self-signup.                                       | The account is the door (sketch, principle 8). Granting the scope is an admin act, and it weighs as much as `admin`: its holder can assign stored roles.                                                              |
| **D - Login identifier**                 | `user_account.email`: optional, stored lowercased, unique when set. Not a reference to `ContactInfo`.                                                                                                                                                     | CRM addresses are unique per party only, and must not be changeable through `crm:write`. People who only use Discord may have none.                                                                                   |
| **E - Account vs. login**                | `status` is `active` or `disabled`; an account is `active` from its creation. Logging in is a separate matter: a password (this arc) or Discord (bot arc).                                                                                                | A person who never uses the portal still needs an account; an `active` account without a way to log in is "not set up yet".                                                                                           |
| **F - Grant modes**                      | `application_client_scope_grant.mode`: `application` (for the client itself) or `delegated` (ceiling for people). Client-only scopes are grantable in `application` mode only.                                                                            | The client is the ceiling without its ceiling becoming its own rights.                                                                                                                                                |
| **G - Token endpoint**                   | `POST /auth/token` gains `password` and `refresh_token`. Client authentication is required for every grant; both new grants require `auth:users:login` in `application` mode.                                                                             | One issuing path, one response model, one audit path.                                                                                                                                                                 |
| **H - Token scopes**                     | `client_credentials`: from `application` grants. A person's token: `delegated` grants ∩ the person's role scopes. A request only narrows. Computed at issuance and at every refresh.                                                                      | "Client on behalf of person" without a second check at request time.                                                                                                                                                  |
| **I - Reach qualifier**                  | `<scope>:own` restricts a scope to reachable records; the unqualified scope implies it. `require_scopes` demands the unqualified form and refuses a `:own` scope at import time; `require_access` accepts either.                                         | A forgotten filter is a `403`, never a leak.                                                                                                                                                                          |
| **J - Reach derivation**                 | Own party plus the `to_party` of outgoing `PARENT_OF` / `PAYS_FOR` relations, resolved per request, each with its basis (`self`, `guardian`).                                                                                                             | Same set as the bot's delegation check; always current; the basis leaves room for tutor projections.                                                                                                                  |
| **K - Roles**                            | `student`, `tutor`, `guardian` derived from the CRM, never stored; `admin` is a row in `auth.user_account_role`. Role-to-scope mapping in code. Roles are a set.                                                                                          | No second role source that can drift from the CRM; `admin` is an access right, not a CRM fact.                                                                                                                        |
| **L - Sessions**                         | Access token: stateless JWT, 15 minutes. Refresh token: opaque, rotating, stored as SHA-256, 30 days absolute, reuse detection with a 10-second grace (`REFRESH_REUSE_GRACE`).                                                                            | Disabling an account or removing a role takes effect within 15 minutes without a lookup per request. The grace costs detection: a thief who rotates first goes unnoticed when the victim refreshes inside the window. |
| **M - Out of reach**                     | A party named in the **path** and out of reach: `404` with the resource's own not-found error, never `403`.                                                                                                                                               | A restricted caller cannot probe which parties exist.                                                                                                                                                                 |
| **N - Deleting a party**                 | `user_account.party_id` is `ON DELETE CASCADE`; `delete_party` and its `EXTERNAL_LINKS` guard are not touched.                                                                                                                                            | The CRM stays unaware of accounts (ADR 0007).                                                                                                                                                                         |
| **O - Denials are returned, not raised** | Every denial that writes state (audit entry, failed-login counter, session revocation) is returned as a response.                                                                                                                                         | Raising rolls the session back and would undo the counter - the pattern `create_token` already follows.                                                                                                               |
| **P - First client and first admin**     | `just bootstrap-client` seeds a client with grants in both modes. `just bootstrap-admin --party-id <uuid> --email <address>` ensures an enabled admin account and prints an invitation, or a reset token once a password exists: the break-glass command. | Creating clients needs `auth:clients:manage` and creating accounts an admin: neither first one can be created through the API, and the only admin cannot reset, re-enable or re-promote themselves.                   |
| **Q - How a token was obtained**         | A person's token carries `amr` (RFC 8176): `["pwd"]` in this arc, `["discord"]` from the bot arc.                                                                                                                                                         | The two ways differ in strength; password-only actions (P1-2) can demand `pwd`.                                                                                                                                       |

**Open:** whether `admin` also carries `bot:write`. It starts without it; adding it is one line in `ROLE_SCOPES`. _(Superseded by [bot-decoupling.md](bot-decoupling.md).)_

## Client grants

`ApplicationClientScopeGrant` gains `mode: GrantMode` (`StrEnum`: `APPLICATION = "application"`,
`DELEGATED = "delegated"`); the primary key becomes `(application_client_id, scope_key, mode)`, so a scope can be
granted in both modes. `mode` is `NOT NULL` with `default=GrantMode.APPLICATION` and
`server_default=text("'application'")` (the `ApplicationClient.status` pattern). The server default turns every
existing grant into an `application` grant - SkillBot keeps exactly the rights it has; the Python default keeps
every caller that passes no mode inserting `application` grants until P0-4.

The downgrade deletes every `delegated` grant before it restores the two-column primary key. The old model cannot
express a ceiling, and a row kept without its `mode` would become the client's own right on the next
`client_credentials` token - the very thing decision F rules out. A lost ceiling costs a re-grant; a promoted one is
a leak.

`CLIENT_ONLY_SCOPES: frozenset[Scope]` in [`scopes.py`](../../app/core/auth/scopes.py) holds the scopes that only
make sense for a client: `auth:users:login` in this arc (the bot arc adds `auth:users:exchange`). Granting one of
them in `delegated` mode is refused with `invalid_scope` and grants nothing; no role's scopes contain one.

Client API (all `auth:clients:manage`, as today):

- `POST /auth/clients/{client_id}/scopes` takes `{scopes, mode}`; `mode` is required.
- `DELETE /auth/clients/{client_id}/scopes/{mode}/{scope_key}` revokes one grant.
- `ApplicationClientResponse` replaces `scopes` with `application_scopes` and `delegated_scopes`, each sorted.
- Audit entries of grants and revocations name the mode.

The service functions (`grant_application_client_scopes`, `bootstrap_application_client`, ...) take `mode` as a
keyword that defaults to `application`, so `just bootstrap-skillbot` and every existing caller behave as before.
`just bootstrap-client` calls `bootstrap_application_client` once per mode.

## Scopes and roles

### New scopes

Declared on `Scope` in [`scopes.py`](../../app/core/auth/scopes.py) as `(value, description)`.

| Scope               | Meant for          | Meaning                                                                                                                                                                              |
| ------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `crm:read:own`      | `delegated`        | Read parties within the caller's reach. Useless on an application token (no person, no reach).                                                                                       |
| `account:self`      | `delegated`        | Manage the caller's own account; the first route that requires it comes in P1-2. Every person holds it, so a client that delegates it can log in every person, whatever their roles. |
| `auth:users:manage` | either             | Create, disable and reset accounts; assign stored roles. As strong as `admin`: its holder can assign that role.                                                                      |
| `auth:users:login`  | `application` only | Log people in on their behalf (`password` / `refresh_token` grants, redeem, revoke).                                                                                                 |

Only `CLIENT_ONLY_SCOPES` are mode-restricted; every other scope is grantable in both modes (`account:self` on an
application token is accepted and useless until P1-2).

### Implication

`OWN_VARIANT: dict[Scope, Scope]` maps an unqualified scope to its qualified form; in this arc it has one entry,
`CRM_READ -> CRM_READ_OWN`. `expand(scopes)` adds the `:own` variant of every unqualified scope present;
`canonical(scopes)` drops `x:own` wherever `x` is present; it inverts `expand` on canonical sets only. Both take a
`Set[str]`; `parse_scopes` and `format_scopes` are the only places a scope string is split or joined (they replace
`normalize_scope_set` in `app/services/auth/scopes.py` and `_format_scope` in `tokens.py`). A token always carries the
canonical form; every check expands first (`missing = required - expand(principal.scopes)` in
`get_current_principal`). `require_scopes` raises at import time for any value of `OWN_VARIANT`: a `:own` scope is
demanded only through `require_access` ([Reach](#reach)).

### Token scope computation

```
client_credentials:   available = expand(application grants)
password / refresh:   available = expand(delegated grants) & expand(role scopes)
                                  (refresh: & expand(parse_scopes(session.scope)) as well)

no scope requested  ->  token = canonical(available)
scope requested     ->  requested <= available, else invalid_scope;  token = canonical(requested)
empty result        ->  invalid_scope
```

`resolve_token_scopes` in [`services/auth/scopes.py`](../../app/services/auth/scopes.py) implements it with the
signature `(*, requested: Set[str], granted: Set[str], ceilings: Iterable[Set[str]] = ()) -> frozenset[str]`:
`available` is `expand(granted)` intersected with `expand(c)` for every ceiling (the role scopes; on refresh also
the session's `scope`), and an empty `requested` means none was requested. The caller passes the grants of the mode
that applies and parses the form's `scope` with `parse_scopes`. For `client_credentials` the observable behavior
only widens (a client granted `crm:read` may now request `crm:read:own`).

### Roles

`Role` (`StrEnum`), `STORED_ROLES`, `BASE_USER_SCOPES`, `ROLE_SCOPES` and `scopes_for(roles)` live in
`app/core/auth/roles.py`; the derivation that reads the CRM tables lives in `app/services/auth/roles.py`.

| Role           | Source                                                       | Scopes                                                                          |
| -------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| _(every user)_ | -                                                            | `account:self`, `crm:read:own`                                                  |
| `student`      | the party's person has a `Student` row                       | -                                                                               |
| `tutor`        | the party's person has a `Tutor` row                         | -                                                                               |
| `guardian`     | the party has an outgoing `PARENT_OF` or `PAYS_FOR` relation | -                                                                               |
| `admin`        | row in `auth.user_account_role`                              | `crm:read`, `crm:write`, `auth:users:manage`, `auth:clients:manage`, `bot:read` |

`bot:read` leaves the `admin` row with P0-5 of [`bot-decoupling.md`](bot-decoupling.md); bot-decoupling's P0-2 adds
`auth:discord-links:read`.

The derived roles carry no scopes of their own yet; they tell `/auth/me` and the token's `roles` claim which views
to offer. **SkillForge authorizes by scope only; it never branches on a role.**

`DELEGATION_RELATION_TYPES` (`PARENT_OF`, `PAYS_FOR`) lives in `app/core/auth/reach.py`. The guardian derivation,
the reach and [`authz.py`](../../app/services/bot/authz.py) all import it, so the bot's delegation set and the
API's reach cannot drift apart.

## Accounts

**`auth.user_account`** (`UserAccount`, `TimestampMixin`)

| Column               | Type                         | Notes                                                                  |
| -------------------- | ---------------------------- | ---------------------------------------------------------------------- |
| `id`                 | `uuid` PK                    | The token's `principal_id`; `sub` is `user:<id>`.                      |
| `party_id`           | `uuid` FK -> `core.party.id` | `NOT NULL`, `UNIQUE`, `ON DELETE CASCADE` (decision N).                |
| `email`              | `text`                       | `NULL` allowed, `UNIQUE`, `CHECK (email = lower(email))` (decision D). |
| `password_hash`      | `text`                       | `NULL` until a password is set.                                        |
| `status`             | enum `user_account_status`   | `active` / `disabled`; server default `active`.                        |
| `failed_login_count` | `int`                        | `NOT NULL`, default `0`.                                               |
| `locked_until`       | `timestamptz`                | `NULL` = not locked.                                                   |
| `last_login_at`      | `timestamptz`                | `NULL` until the first login.                                          |

**Life cycle** (routes in the [route map](#route-map)):

- **Create.** `POST /auth/users` with `{party_id, email?, roles?}`: the party must exist and be a `PERSON`, it may
  not have an account yet, the e-mail may not be in use. `roles` names stored roles only. The account is `active`;
  the response carries no token. The party row is read with `with_for_update(key_share=True)` first (as
  `_load_person` in [`roles.py`](../../app/services/crm/roles.py) does), so a concurrent `delete_party` ends in
  `unknown_account_party`, not in a foreign-key error.
- **Invite** - set up the password login. Needs an e-mail address and no password yet, else `user_account_state`.
  Returns the plaintext token once; earlier unused invitations stop working.
- **Reset.** Needs a password, else `user_account_state`. Nothing changes until the token is redeemed.
- **Redeem** (`POST /auth/password/redeem`, a login client on the person's behalf) serves both purposes: sets the
  hash, clears counter and lock, marks the token used and - for a reset - revokes every session. The status does
  not change: an `active` account stays active, a `disabled` one stays disabled.
- **Update.** `PATCH` with the `MISSING`-based model of the CRM: `email` (a new address, or `null` to remove it)
  and `status`. Disabling revokes all sessions; enabling is always allowed. Changing or removing the e-mail
  invalidates the account's unused action tokens; removing the e-mail of an account that has a password is
  `user_account_state` (the password login needs it). A request that changes nothing records nothing.
- **Stored roles.** `PUT` / `DELETE /auth/users/{user_id}/roles/{role}`, row-locked like v1. `PUT` is idempotent:
  a role the account already holds answers `200` and records nothing.
- **Sessions.** `DELETE /auth/users/{user_id}/sessions` revokes every live session.

**`auth.user_account_role`** (`UserAccountRole`, `CreatedAtMixin`) - primary key `(user_account_id, role)`,
`ON DELETE CASCADE`; `role` is `UserAccountRoleName` (`StrEnum`, enum `user_account_role_name`) with the single
value `admin` - the stored subset of `Role`.

**`auth.user_action_token`** (`UserActionToken`, `CreatedAtMixin`) - one-time tokens for invitation and reset:
`purpose` (enum `user_action_token_purpose`: `invitation` / `password_reset`), `token_hash` (`UNIQUE`, SHA-256
hex, plaintext prefix `sf_ua_`), `expires_at`, `used_at`, `invalidated_at`, `issued_by`
(`<principal_type>:<principal_id>` or `cli`); `user_account_id` indexed, `ON DELETE CASCADE`.

**`auth.user_session`** (`UserSession`, `CreatedAtMixin`) - one row per login of a person through a client; the
frontend's own session (the portal's cookie) is separate. Columns: `user_account_id` (indexed),
`application_client_id` (indexed; only this client may refresh or revoke the session), `scope` (canonical scopes
granted at login - the ceiling of every refresh, never rewritten), `refresh_token_hash` (`UNIQUE`; plaintext prefix
`sf_rt_`), `previous_refresh_token_hash` (`UNIQUE`), `rotated_at`, `expires_at` (absolute), `last_used_at` (set on
every successful refresh), `revoked_at`, `revoked_reason` (`text`, values of the code-only `StrEnum`
`SessionRevokedReason`: `logout`, `password_reset`, `account_disabled`, `admin`, `reuse_detected`). Both foreign
keys `ON DELETE CASCADE`.

Refresh and action tokens are `generate_secret(prefix)` values (32 random bytes) stored as `digest` (SHA-256):
they carry full entropy, and the lookup needs a deterministic hash. Passwords use `hash_secret` / `verify_secret`
(Argon2); `verify_and_update` is added for the login. These replace today's `generate_client_secret` /
`hash_client_secret` / `verify_client_secret` - one hashing home for client secrets and passwords (decision B).

**`AuthAuditLog`** is reused. `AuditEventType` gains `user_account.created`, `user_account.updated`,
`user_account.disabled`, `user_account.enabled`, `user_role.added`, `user_role.removed`, `invitation.issued`,
`password_reset.issued`, `password.set`, `session.revoked`, `session.reuse_detected`. Account events name the
account (`principal_type="user"`, `principal_id` its id) and the actor in `detail` as `<principal_type>:<principal_id>`
or `cli`. `token.issued` / `token.denied` of a person's grants carry `principal_type="user"` and, as `principal_id`,
the account's id or `None` when no account matched - never the submitted `username`; the client denials of steps 1
and 2 of `password` stay as today. No e-mail address reaches an audit row or `bind_request_log_context`, and no
audit `detail` contains a secret.

**Settings** (`AuthSettings` in [`config.py`](../../app/core/auth/config.py)): `invitation_expire_hours = 168`,
`password_reset_expire_hours = 24` (one hour once P1-1 sends mail), `refresh_token_expire_days = 30`,
`login_lockout_threshold = 5`, `login_lockout_max_minutes = 15`. Each lands in
[`.env.example`](../../.env.example) as `AUTH__*` with the slice that uses it (P0-6 the two expiries, P0-8 the
rest).

## Tokens

`POST /api/v1/auth/token`, form-encoded, OAuth2 error codes (RFC 6749, section 5.2). Client authentication (HTTP
Basic or `client_id` / `client_secret` in the form) is required for **every** grant.

| `grant_type`         | Additional form fields                        | Result                                                        |
| -------------------- | --------------------------------------------- | ------------------------------------------------------------- |
| `client_credentials` | `scope?`                                      | Unchanged, from `application` grants.                         |
| `password`           | `username` (the e-mail), `password`, `scope?` | Opens a `user_session`; the response carries `refresh_token`. |
| `refresh_token`      | `refresh_token`, `scope?`                     | Rotates the refresh token; the response carries the new one.  |

`AccessTokenResponse` gains `refresh_token` and `refresh_expires_in` (seconds until the session's `expires_at`).
`client_credentials` omits both (`response_model_exclude_none=True` on `create_token`; RFC 6749, section 5.1 makes
`refresh_token` optional), so the application token's response body stays exactly as today.

**`password`, in this order:**

1. Authenticate the client -> `invalid_client`.
2. The client holds `auth:users:login` in `application` mode -> else `unauthorized_client`.
3. Normalize `username` (`normalize_email`) and look the account up. If it does not exist, is `disabled`, has no
   password or is locked (`locked_until > now`): verify the submitted password against `dummy_password_hash()`,
   then answer `invalid_grant`. A locked account's counter does not move.
4. `verify_and_update` the password. On failure: increment `failed_login_count`; from `login_lockout_threshold`
   on, set `locked_until = now + min(max, 1 min * 2^(count - threshold))`; answer `invalid_grant`. On success:
   reset counter and lock, store an upgraded hash if one came back.
5. Derive the roles, compute the scopes from the client's `delegated` grants -> `invalid_scope` (`token.denied`;
   step 4's changes stay, no session is opened).
6. Open the session, set `last_login_at`, issue both tokens with `amr: ["pwd"]`, write `token.issued`.

`invalid_grant` has **one** body for every case in steps 3 and 4.

**`refresh_token`:** authenticate the client and check `auth:users:login` as above, then look the digest up against
both hashes in one statement, `FOR UPDATE` with `populate_existing`, and choose a branch. Every branch applies only
to a session of **this** client; another client's token is "anything else". The login lock does not affect
`refresh_token` or `/revoke`.

- It matches `refresh_token_hash` of a live, unexpired session whose account is `active`: re-derive the roles,
  compute the scopes with the session's `scope` as an additional ceiling, rotate (`previous <- current`,
  `rotated_at = now`, `last_used_at = now`), answer with both tokens. A refresh never writes `scope`: a narrowing
  `scope=` shapes only the returned token, and the next refresh without it returns to the ceiling. The token carries
  `amr: ["pwd"]` - in this arc every session comes from a password login.
- The account is `disabled`: revoke the session (`account_disabled`), answer `invalid_grant`.
- It matches `previous_refresh_token_hash`: within `REFRESH_REUSE_GRACE` (10 seconds, in
  `app/services/auth/sessions.py`) of `rotated_at` answer `invalid_grant`, write `token.denied` with the fixed detail
  `refresh token reused within grace` and leave the session alone (two requests raced); after that revoke the
  session (`reuse_detected`), write `session.reuse_detected`, answer `invalid_grant`. An old refresh token is never
  answered with the current one.
- Anything else: `invalid_grant`.

**`POST /auth/revoke`** (`{refresh_token}`, a login client) revokes the session the token belongs to if this client
opened it (`logout`), and answers `204` whether or not it found one (RFC 7009 semantics).

**Claims of a person's token** (an application token keeps the same claim set with the same values):

```
iss, aud, iat, exp, jti      as today
sub             "user:<user_account.id>"
principal_type  "user"
principal_id    "<user_account.id>"
azp             "<client_id of the client that logged the person in>"
scope           canonical, space-separated
party_id        "<core.party.id>"
sid             "<user_session.id>"
roles           ["admin", "tutor"]        sorted; informational
amr             ["pwd"]                   how the person was authenticated (decision Q)
```

`Principal` in [`principal.py`](../../app/core/auth/principal.py) is the abstract base of two frozen dataclasses,
`ApplicationPrincipal` and `UserPrincipal`; the latter adds `party_id`, `session_id`, `roles: frozenset[Role]`
and `auth_methods: frozenset[AuthMethod]` (`StrEnum`, `PASSWORD = "pwd"`). `principal_type` (`PrincipalType`, a
`StrEnum` that replaces `PRINCIPAL_TYPE_APPLICATION` in `tokens.py`) is a class constant and `subject` is derived;
`client_id` stays on the base - the client itself, or for a person `azp`, the client that logged them in.
`create_access_token(settings, principal)` writes a principal and `validate_access_token` reads it back, both
through one pydantic declaration of the claims - a discriminated union on `principal_type`.
`create_application_access_token` stays as the application token's shorthand. A person's token without
`party_id`, `sid` or `amr`, with an unknown role or method, or whose `sub` is not the canonical
`user:<principal_id>`, is invalid.

The security scheme in [`security.py`](../../app/core/auth/security.py) declares a `password` flow next to
`clientCredentials`, so Swagger UI's "Authorize" dialog logs a person in. The class is renamed to `OAuth2Bearer`
with an explicit `scheme_name="OAuth2"` - a one-time, contract-wide rename of the key in
`components.securitySchemes`, made while no consumer is live.

`GET /auth/me` gains `user_id`, `party_id` and `roles` (`null` / empty for an application principal) and still
answers from the token alone. The descriptions follow: `client_id` names the client that logged a person in, and
`scopes` are canonical - an unqualified scope implies its `:own` form. `bind_request_log_context` receives
`principal_type`, `user_id` and `party_id` for a person, never the session id or an e-mail address.

## Reach

`require_access(scope)` in [`dependencies.py`](../../app/core/auth/dependencies.py) is the reach-aware sibling of
`require_scopes`, used as a parameter: `Annotated[Access, require_access(Scope.CRM_READ)]`.

| Token carries  | Principal                 | Result                                                  |
| -------------- | ------------------------- | ------------------------------------------------------- |
| `crm:read`     | any                       | `Access.all()` - no query                               |
| `crm:read:own` | a `UserPrincipal`         | `Access.of(reach)` - one query on `core.party_relation` |
| `crm:read:own` | an `ApplicationPrincipal` | `403`, the body of a missing scope                      |
| neither        | any                       | `403`                                                   |

`app/core/auth/reach.py` holds:

- `ReachBasis` (`StrEnum`): `SELF`, `GUARDIAN` (the bot arc adds `TUTOR`).
- `Access` (frozen dataclass): `Access.all()` or `Access.of(reach: Mapping[UUID, ReachBasis])`;
  `party_ids: frozenset[UUID] | None` (`None` = all), `allows(party_id) -> bool`,
  `basis(party_id) -> ReachBasis | None`. It knows nothing about HTTP or CRM errors.
- `resolve_reach(session, party_id) -> dict[UUID, ReachBasis]`: the own party as `SELF`, every `to_party` of an
  outgoing relation in `DELEGATION_RELATION_TYPES` as `GUARDIAN`.
- `DELEGATION_RELATION_TYPES`.

`require_access` obtains its session through `DBSession`, which moves to
[`app/core/db/dependencies.py`](../../app/core/db/dependencies.py) (decision N of [`crm-api.md`](crm-api.md)
unchanged) and stays re-exported from `app.api.v1.common`, so the guard and the endpoint share one request session.
A bare `Depends(get_db_session)` would open a second session on every reach-aware request, `crm:read` included:
FastAPI keys its dependency cache on the scope.

`require_access(scope)` raises at import time when `scope` has no entry in `OWN_VARIANT`. It returns
`Security(_access_for(scope), scopes=[OWN_VARIANT[scope]])`: the marker declares the **qualified** scope, so
`get_current_principal` (which `_access_for` depends on) demands `crm:read:own`, and a `crm:read` token passes
through `expand`. `_access_for` then reads the principal: `crm:read` in its scopes gives `Access.all()` without a
query; otherwise a `UserPrincipal` gets `Access.of(await resolve_reach(session, principal.party_id))`, and an
`ApplicationPrincipal` is refused with the `403` body `get_current_principal` uses (`Not enough permissions`, log
reason `wrong_principal_type`), so `FORBIDDEN_EXAMPLES` stays true.

FastAPI merges every marker on one scheme into one requirement with AND semantics, so the alternative is derived:
`customize_openapi` in [`openapi.py`](../../app/api/v1/common/openapi.py), before `_document_auth_errors`, replaces
every requirement that names a value of `OWN_VARIANT` by two under the same key, one with the base scope substituted
and one unchanged - `[{"OAuth2": ["crm:read"]}, {"OAuth2": ["crm:read:own"]}]`. A requirement that names both `x`
and `OWN_VARIANT[x]` (a route mixing `require_scopes(x)` with `require_access(x)`) fails when the schema is built.
The derived `403` reads requirements as alternatives and the scopes inside one as conjunctive:
`Missing required scope: crm:read or crm:read:own`, while one requirement with two scopes stays
`Missing required scopes: a, b`. On a `401` a reach-aware operation answers
`WWW-Authenticate: Bearer scope="crm:read:own"`, derived from the marker exactly as for `require_scopes`.

In the CRM API, two aliases in [`params.py`](../../app/api/v1/crm/params.py): `CrmReadAccess`
(`Annotated[Access, require_access(Scope.CRM_READ)]`) and `VisibleParty`, a path alias that depends on it and raises
`PartyNotFoundError` when `access.allows(party_id)` is false. `list_parties` in
[`parties.py`](../../app/services/crm/parties.py) gains a keyword-only `party_ids: frozenset[UUID] | None = None`
filter. `GET /crm/parties` and `GET /crm/parties/{party_id}` switch to `require_access(Scope.CRM_READ)`; no other
route changes.

## Route map

All under `/api/v1/auth`, tag `auth`. JSON routes follow the API conventions: `ApiModel` schemas, scopes on the
decorator, `error_responses(...)`, `Page[T]` for the list, operation IDs `auth_<function>`. The account routes,
redeem, revoke and `create_token` take `DBSession` (decision N of `crm-api.md`): sessions, rotations, counters and
passwords commit before the response, and a returned denial still commits.

```
# Tokens - form-encoded, OAuth2 errors
POST   /token                              client auth               3 grants

# Accounts - admin surface                 auth:users:manage
POST   /users                              create                    201  UserAccountDetail
GET    /users                              list, filter              200  Page[UserAccountListItem]
GET    /users/{user_id}                                              200  UserAccountDetail
PATCH  /users/{user_id}                    email, status             200  UserAccountDetail
PUT    /users/{user_id}/roles/{role}       idempotent                200  UserAccountDetail
DELETE /users/{user_id}/roles/{role}                                 204
POST   /users/{user_id}/invitation                                   201  ActionTokenResponse
POST   /users/{user_id}/password-reset                               201  ActionTokenResponse
DELETE /users/{user_id}/sessions           revoke all                204

# On a person's behalf - application principal with auth:users:login (application mode)
POST   /password/redeem                    {token, new_password}     204
POST   /revoke                             {refresh_token}           204

# Client grants                            auth:clients:manage
POST   /clients/{client_id}/scopes         {scopes, mode}            200  ApplicationClientResponse
DELETE /clients/{client_id}/scopes/{mode}/{scope_key}                204

# Caller
GET    /me                                 any token                 200  MeResponse
```

- **`GET /users`** takes `UserAccountListParams` (a `PageParams` subclass) with `status`, `party_id` and `email`
  (exact, normalized) filters, ordered by `created_at` then `id` (the e-mail is optional).
- **`UserAccountListItem`** carries `id`, `party_id`, `email`, `status`, `has_password`, `roles` (stored **and**
  derived, sorted), `last_login_at`, `created_at`; **`UserAccountDetail`** adds `locked_until` and `updated_at`. No
  hash, no counter.
- **`ActionTokenResponse`** carries `token` and `expires_at` - the only time the plaintext exists outside the hash.
- The redeem and revoke routes use `Security(require_application, scopes=[Scope.AUTH_USERS_LOGIN])`: a person's
  token is `403` whatever it carries.

## Error catalog

Taxonomy errors (`app/services/auth/errors.py`, mapped by `STATUS_BY_ERROR`):

| Class                           | Status | `code`                        | Raised when                                                                                                                |
| ------------------------------- | ------ | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `UserAccountNotFoundError`      | 404    | `user_account_not_found`      | Unknown `user_id`.                                                                                                         |
| `UserAccountAlreadyExistsError` | 409    | `user_account_already_exists` | The party already has an account.                                                                                          |
| `UserEmailAlreadyInUseError`    | 409    | `user_email_already_in_use`   | Another account uses the e-mail (create, patch).                                                                           |
| `UserAccountStateError`         | 409    | `user_account_state`          | Invitation without e-mail or with a password; reset without a password; removing the e-mail of an account with a password. |
| `UnknownAccountPartyError`      | 422    | `unknown_account_party`       | `party_id` of a new account does not exist.                                                                                |
| `AccountPartyNotAPersonError`   | 422    | `account_party_not_a_person`  | The party is a company.                                                                                                    |
| `UserRoleNotFoundError`         | 404    | `user_role_not_found`         | Removing a stored role the account does not hold.                                                                          |
| `InvalidActionTokenError`       | 422    | `invalid_action_token`        | Unknown, used, invalidated or expired token - one error for all four.                                                      |
| `WeakPasswordError`             | 422    | `weak_password`               | Password shorter than 8 or longer than 128 characters.                                                                     |

`UnknownAccountPartyError` is a `DomainValidationError` (message "Unknown party"): `party_id` in `POST /auth/users`
is a body reference, so both of its failures are `422` (decision I-1 of `crm-api.md`); a missing `user_id` in the
path is `404`. `role` in the path and in `roles` is validated by `UserAccountRoleName`, `mode` in the path and body
by `GrantMode`; an unknown or derived role, or an unknown mode, is a request validation error.

OAuth2 errors (`ApiError` constants in [`errors.py`](../../app/api/v1/auth/errors.py)), new: `INVALID_GRANT` (400,
`invalid_grant`, "Invalid credentials or refresh token") and `UNAUTHORIZED_CLIENT` (400, `unauthorized_client`,
"Client may not use this grant"). `INVALID_REQUEST` is reworded to cover any missing grant parameter.
`INVALID_SCOPE` also answers a client-only scope granted in `delegated` mode.

The catalog is closed: a requirement that seems to need another class or `code` is a reason to stop and report.

## Security rules

- **Passwords:** 8 to 128 characters, nothing else. Checked in the service (`WeakPasswordError`), not in the
  schema: a deliberate exception to decision I-2 of `crm-api.md`, so that redeem and P1-2's password change share
  one policy in `passwords.py` and the portal can branch on one `code`.
- **No enumeration:** `invalid_grant` and `invalid_action_token` have one body each; the dummy-hash verification
  keeps "unknown account" as slow as "wrong password".
- **Lockout is per account.** SkillForge sees only the portal's address; limiting by client IP is the job of the
  portal and the reverse proxy. Accepted trade-off: whoever knows an e-mail can keep its password login locked (15
  minutes per attempt); existing sessions are unaffected.
- **Plaintext exists once.** Refresh and action tokens appear in the response that creates them and are stored as
  digests only; never in logs, audit details or error messages.
- **Denials are returned** (decision O). `create_token` is the single place that turns a denial into an
  `ApiError` response, for all three grants.
- **Client-only scopes never reach a person's token:** they are grantable in `application` mode only and are in no
  role's scopes; `test_no_role_carries_client_only_scopes` pins the second half.
- **Access tokens are not revocable.** Every revocation acts on the session; the access token dies within 15
  minutes (decision L).

## BFF contract

What SkillForge expects from the portal's backend-for-frontend; implemented in the skillsite arc.

- The browser talks to the portal only. SkillForge gets no CORS configuration and sets no cookies.
- The client secret and both tokens stay on the server; the browser holds an `HttpOnly`, `Secure`, `SameSite=Lax`
  session cookie - the portal's own session, with a `user_session` behind it.
- Refresh is **single-flight** per session. The 10-second grace only absorbs an accidental race.
- A refresh answering `invalid_grant` ends the portal session; a portal session never outlives
  `refresh_expires_in`.
- The portal client holds `auth:users:login` as an `application` grant and, as `delegated` grants, `account:self`
  and the unqualified scopes its users may hold at most (`crm:read`, `crm:write`, ...). The ceiling is not what a
  student gets.
- The portal also holds a `client_credentials` token of its own for redeem and revoke.
- The portal or its reverse proxy limits login attempts per client IP; SkillForge locks per account only.
- For a narrowed view the portal refreshes with `scope=...` instead of filtering on its side; to follow a role
  change it refreshes without `scope`.

## Operating without a portal

1. Seed the operator client; the command prints its secret once:

   ```
   just bootstrap-client operator --application "auth:users:login crm:write" \
     --delegated "account:self crm:read crm:write auth:users:manage auth:clients:manage bot:read"
   ```

   The `delegated` set is `BASE_USER_SCOPES` plus `ROLE_SCOPES[admin]`; `crm:write` in `application` mode exists
   only to create the first party.

2. Authorize as `operator` (`clientCredentials`) and create the admin's person party with `POST /crm/persons`.
3. `just bootstrap-admin --party-id <uuid> --email <address>` prints the invitation token.
4. Still as the client, redeem it with `POST /auth/password/redeem`.
5. Log out, then authorize with the `password` flow (same client, no scope ticked): the token is now the admin's.
   From here clients and accounts are managed as a person;
   `DELETE /auth/clients/operator/scopes/application/crm:write` removes the transitional grant.
6. Locked out (forgotten password, self-disabled, own `admin` role removed, mistyped address): rerun step 3 and
   redeem the printed token as the client, as in step 4. A lock (`locked_until`) expires after 15 minutes at most.

Swagger UI holds one token per scheme: log out before switching between the client's token and the person's;
redeem and revoke need the client's. Until the portal exists, the operator redeems every invitation and so knows
every initial password.

## Designed for the bot arc

Not built here; recorded so that this arc's shapes take it with no change but the widening goal 7 names. The token
exchange, Discord links, the retirement of the grant engine and the change signals are specified in
[`bot-decoupling.md`](bot-decoupling.md); tutor reach and accounts by tutors wait for the
[reach arc](../PROJECT.md#roadmap).

- **Token exchange.** An extension grant (RFC 6749, section 4.5) on `POST /auth/token`: the bot authenticates as a
  client holding `auth:users:exchange` (an `application`, client-only scope) and names the Discord user who sent a
  command, optionally with the scopes the command needs. SkillForge follows active `ext.discord_account` -> party
  -> account -> `active`, computes the scopes from the bot's `delegated` grants and the person's roles, and issues a
  token with `amr: ["discord"]` and no refresh token; `sid` becomes optional for such tokens. Any broken link is
  the same `invalid_grant`; the bot then offers only what needs no identity.
- **Tutor reach.** `ReachBasis.TUTOR` via `TUTOR_OF`, with representations that differ by basis (a tutor does not
  see who pays for a student).
- **Accounts by tutors.** A reach-limited scope lets a tutor create accounts for people in their reach, without
  stored roles. A party out of reach reads exactly like a missing one: `unknown_account_party`.
- **The bot's grant engine retires.** What `PermissionGrant` decides today becomes scopes and reach of the person.
- **Change signals.** The bot pulls changes (`updated_since`, plus a periodic comparison for deletions) and runs its
  own workflows in its own database; a new ADR supersedes ADRs 0003 and 0004.

## Requirements

### Must-have (P0)

**Standing criteria - they hold for every slice and are ticked with P0-8.**

- [x] Nothing under `app/core/auth` imports `app.services` or `app.api` (it may read CRM _models_); nothing under
      `app/services/crm` or `app/api/v1/crm` imports the bot domain (ADR 0007).
- [x] No plaintext password, refresh token or action token is written to a log, an audit `detail` or an error
      `detail`, and no e-mail address to a log or an audit row (the lifecycle tests grep their captured JSON log
      output, as the archived `test_auth_users_logging.py` does).
- [x] Every property of every new schema, every new property of a changed schema and every new path and query
      parameter has a description in `openapi.json`; an auth schema a slice changes derives from `ApiModel`; the
      operation IDs of the auth routes match `^auth_[a-z_]+$`.
- [x] `just check-all` is green; `openapi.json` is regenerated with `just openapi`, never edited.
- [x] **SkillBot keeps working.** `client_credentials`, the claims of an application token and every `/bot` route
      behave exactly as before for an application principal: the `client_credentials` response body keeps exactly
      its four keys, the bot-domain tests pass **unmodified**, and `issue_client_token` keeps its signature and its
      `CreatedAccessToken` result. The only contract changes SkillBot can see are additive - two optional
      properties of `AccessTokenResponse`, the new fields of `MeResponse` - plus the one-time scheme rename of P0-5.
      SkillBot does not call the client routes, so their new grant shape does not reach it. _(Superseded by [bot-decoupling.md](bot-decoupling.md).)_

**P0-1 - Spec and ADR.** _Docs only._

- [x] This spec and ADR 0008 are on `main`; the decisions index lists 0008; issue #85 links the spec.

**P0-2 - Scope model.** _No schema change._

- _Technique:_ the four new `Scope` members, `CLIENT_ONLY_SCOPES`, `OWN_VARIANT`, `expand`, `canonical`,
  `parse_scopes`, `format_scopes` in `scopes.py`; `app/core/auth/roles.py`; `resolve_token_scopes` as specified;
  `get_current_principal` expands before comparing; `require_scopes` refuses a `:own` scope;
  `DELEGATION_RELATION_TYPES` moves from [`authz.py`](../../app/services/bot/authz.py) to `app/core/auth/reach.py`
  and `authz.py` imports it. A test tying `STORED_ROLES` to `UserAccountRoleName` needs P0-3 and lands with P0-6.
- _Tests that change:_ `test_seed_default_scopes_is_idempotent` (`tests/db/test_auth_service.py`): the seeded set
  grows by the four scopes.
- _Acceptance criteria:_
  - [x] `expand({crm:read}) == {crm:read, crm:read:own}`; for every subset `S` of `Scope`,
        `canonical(expand(S)) == canonical(S)` and `expand(canonical(S)) == expand(S)`.
  - [x] Given role scopes `{account:self, crm:read:own}`: delegated grants `{account:self, crm:read, crm:write}`
        yield `account:self crm:read:own`; `{crm:read, crm:write}` yield `crm:read:own`; `{bot:read}` yield
        `invalid_scope`.
  - [x] A client granted `crm:read` may request `crm:read:own`; requesting an ungranted scope is `invalid_scope`.
  - [x] A token carrying `crm:read:own` gets `403` from a route guarded with `require_scopes(Scope.CRM_READ)`; a
        token carrying `crm:read` passes `Security(get_current_principal, scopes=["crm:read:own"])` in the test app
        of `tests/auth/test_dependencies.py`; `require_scopes(Scope.CRM_READ_OWN)` raises.
  - [x] `test_no_role_carries_client_only_scopes`; every scope appears with its description in
        `components.securitySchemes`.
  - [x] The bot's delegation check answers exactly as before (its tests unmodified). _(Superseded by [bot-decoupling.md](bot-decoupling.md).)_

**P0-3 - Data model.**

- _Technique:_ `GrantMode` and the `mode` column with its defaults as specified in [Client grants](#client-grants);
  the four account models exported from `app/core/db/models/auth/__init__.py`;
  [`DATABASE_SCHEMA.md`](../DATABASE_SCHEMA.md) with the four tables, `mode` and the three-column key. The four
  enum types are created like `application_client_status` (no `schema=`, so in `public`). **One** revision,
  autogenerated and then completed by hand: autogenerate neither creates the enum type of an added column nor
  detects a primary-key change, and `alembic check` is blind to both.
  By hand in `upgrade`: create `public.grant_mode` before `op.add_column` (raw `CREATE TYPE` as
  `0007_off_boarding_transitions.py` does, or `postgresql.ENUM(...).create(op.get_bind())`), then
  `op.drop_constraint("application_client_scope_grant_pkey", ..., type_="primary")` and `op.create_primary_key`
  with the same name on `(application_client_id, scope_key, mode)`. `downgrade` mirrors it, commented like
  `0009_cancel_operation.py`: delete `delegated` grants, restore the two-column key, drop `mode`, then
  `DROP TYPE IF EXISTS public.<type>` for all four types, as `0001_baseline.py` does. Until P0-4 takes the mode as a
  parameter, `revoke_application_client_scope` looks up `(client.id, scope_key, GrantMode.APPLICATION)`;
  `grant_client_scopes` stays untouched (its grants become `application` through the default).
- _Tests that change:_ none; `tests/db/test_migration_apply.py` gains the seeded test.
- _Acceptance criteria:_
  - [x] Upgrade and downgrade run clean against an empty and a seeded database. Seeded means
        (`test_grant_mode_and_user_account_migration_is_reversible`): at `0010_subject_title_unique` a
        `permission_scope`, a `PERSON` party, a client and one grant of that scope; after the upgrade, which must
        have turned that grant into `application`, an account with an `admin` role, a session, an action token and a
        `delegated` grant of the same scope. After the downgrade to `0010` the four enum types (`grant_mode`,
        `user_account_status`, `user_account_role_name`, `user_action_token_purpose`) are gone; the party, the client
        and its original grant survive, and that grant is the only grant row.
  - [x] The test reads the primary-key columns of `auth.application_client_scope_grant` from `pg_constraint` (the
        way `_enum_labels` reads `pg_enum`): three after the upgrade, two after the downgrade.
  - [x] One scope can be granted to one client in both modes (a model test that inserts its own `PermissionScope`
        row, like `test_application_client_scope_grant_relationship`; it needs nothing from P0-2).
  - [x] An uppercase e-mail violates the check constraint; two accounts for one party, or two with the same
        e-mail, violate their unique constraints; two accounts without an e-mail do not.
  - [x] A new account defaults to `active`.
  - [x] Deleting a party through `delete_party` removes its account, roles, sessions and action tokens and leaves
        the audit log untouched.

**P0-4 - Grant modes.**

- _Technique:_ the client services and routes take the mode as specified in [Client grants](#client-grants);
  `grant_client_scopes` deduplicates on `(scope_key, mode)`, not `scope_key`; `issue_client_token` reads
  `application` grants only; `ApplicationClientResponse` and `ApplicationClientScopeGrantRequest` derive from
  `ApiModel`. [`bootstrap.py`](../../app/cli/bootstrap.py) gets argparse subcommands: `skillbot` (the
  `bootstrap-skillbot` recipe runs `python -m app.cli.bootstrap skillbot`) and, behind
  `just bootstrap-client`, `client <client_id> --application <scopes> --delegated <scopes>`; both scope lists go
  through `parse_scopes`.
- _Tests that change:_ `tests/api/test_auth_clients_error_contract.py` (the grant body gains `mode`, the revoke path
  `/{mode}`).
- _Acceptance criteria:_
  - [x] Granting with `mode` in the body, revoking by `/{mode}/{scope_key}`, and the client detail listing
        `application_scopes` and `delegated_scopes` work as specified; the audit entries name the mode.
  - [x] Granting `auth:users:login` as `delegated` answers `invalid_scope` and grants nothing of the request.
  - [x] A client holding a scope only as `delegated` does not get it through `client_credentials`.
  - [x] `just bootstrap-client` is idempotent like `bootstrap-skillbot`, grants in both modes, refuses a client-only
        scope in `--delegated` with `invalid_scope`, and prints the secret only when it created one;
        `just bootstrap-skillbot` prints exactly what it prints today, and every existing caller of the grant
        services behaves as before. _(The `bootstrap-skillbot` part is superseded by [bot-decoupling.md](bot-decoupling.md).)_

**P0-5 - Person tokens.**

- _Technique:_ `ApplicationPrincipal` / `UserPrincipal`, `AuthMethod`; `PrincipalType` replaces
  `PRINCIPAL_TYPE_APPLICATION` in `tokens.py`, `dependencies.py` and `tests/auth/test_tokens.py`, no alias; claims
  as a pydantic discriminated union; `create_access_token` for both types; `MeResponse` extended, with the
  `client_id` and `scopes` descriptions of [Tokens](#tokens); `OAuth2Bearer` with `scheme_name="OAuth2"` (the
  `password` flow follows in P0-8); the request log context. P0-5 does not edit `app/services/auth/tokens.py`.
- _Tests that change:_ `tests/auth/test_dependencies.py` (`test_require_application_rejects_non_application_principal`
  builds a `UserPrincipal`; `test_require_scopes_declares_the_scopes_in_openapi_for_both_positions` expects the key
  `OAuth2`), `tests/auth/test_tokens.py` (`PrincipalType`), `tests/api/test_auth_me_endpoint.py` (the properties of
  `MeResponse`).
- _Acceptance criteria:_
  - [x] `components.securitySchemes` has exactly one key, `OAuth2`; apart from the key, the `security` requirement
        of every existing operation is unchanged (pinned against the operations of the branch below; P0-5 sits
        above P0-4 in the stack, so its pin carries P0-4's revoke path). A later slice that deliberately changes
        an operation's requirement removes that operation from the pin in the same PR, with a comment naming the
        slice - P0-7 does so for `crm_list_parties` and `crm_get_party`.
  - [x] A person's token round-trips into a `UserPrincipal` with `party_id`, `session_id`, `roles` and
        `auth_methods`; an application token round-trips exactly as before (claims asserted against a fixture).
  - [x] A person's token missing `party_id`, `sid` or `amr`, with an unknown role or method, or whose `sub` is not
        `user:<principal_id>`, is a `401` in the error envelope.
  - [x] `GET /auth/me` answers both principal types without a database session.
  - [x] `require_application` rejects a person's token; the request log identifies the person behind a request.

**P0-6 - Accounts.**

- _Technique:_ `app/services/auth/accounts.py` (get and lock an account row), `users.py` (create, load, list,
  update, stored roles), `action_tokens.py` (issue, redeem), `sessions.py` (`SessionRevokedReason`, revoke),
  `roles.py` (`derive_roles`), all in `app/services/auth/`; `inputs.py` (`LoginEmail`,
  `normalize_email`); `passwords.py` (policy, `dummy_password_hash`); `app/api/v1/auth/users.py`, the redeem route
  in `app/api/v1/auth/password.py`; the `admin` subcommand behind a `just bootstrap-admin` recipe. `secrets.py`
  renames its functions to `generate_secret(prefix, nbytes=SECRET_BYTES)`, `hash_secret` and `verify_secret`
  (`SECRET_PREFIX` stays) and adds `digest` (SHA-256 hex); the rename follows through
  `app/core/auth/__init__.py` (the three re-exports go), `app/services/auth/secrets.py` and
  `app/services/auth/tokens.py`. Tests insert `UserSession` rows through the model (an `add_user_session` fixture
  on an `ApplicationClient` row, as in the archive): nothing opens sessions before P0-8.
- _Tests that change:_ `tests/auth/test_secrets.py` and `tests/db/models/test_auth_models.py` (the rename);
  `PAGED_ENDPOINTS` in `tests/api/test_openapi_contract.py` gains `/api/v1/auth/users`.
- _Acceptance criteria:_
  - [x] Creating an account for a person party answers `201` with an `active` account and no token, with or
        without an e-mail; the same party again is `user_account_already_exists`; an unknown `party_id` is `422`
        `unknown_account_party`; a company is `account_party_not_a_person`; another account's e-mail is
        `user_email_already_in_use`; `Anna@Example.org` is stored as `anna@example.org`.
  - [x] An invitation for an account without an e-mail, or with a password, is `user_account_state`; a reset for
        an account without a password is `user_account_state`; issuing invalidates earlier unused tokens of the
        same purpose, and two issues arriving together leave one live token.
  - [x] Redeeming an invitation sets the password and leaves the status as it was; redeeming it again, an expired
        token and a replaced token all answer the same `invalid_action_token` body; two overlapping redeems of one
        token set one password.
  - [x] Redeeming a reset revokes every session of the account; redeeming an invitation revokes none.
  - [x] A password of 7 or 129 characters is `weak_password`; 8 and 128 are accepted.
  - [x] Disabling revokes the sessions; enabling an account without a password succeeds; changing or removing the
        e-mail invalidates unused action tokens; removing the e-mail of an account with a password is
        `user_account_state`; a `PATCH` that changes nothing records nothing.
  - [x] `roles` lists `student` for a party with a `Student` row, `guardian` for an outgoing `PAYS_FOR`, and
        `admin` + `tutor` for a tutor holding the stored role; `TUTOR_OF` does not make a tutor a guardian.
  - [x] Every account route is `403` for a token without `auth:users:manage`; the redeem route is `403` for a
        person's token, whatever its scopes, and for a client without `auth:users:login`.
  - [x] `just bootstrap-admin` is idempotent: run again it keeps the account, ensures the `admin` role, sets
        `status = active`, replaces the e-mail when `--email` differs (`user_email_already_in_use` when another
        account holds it; the change invalidates unused action tokens as `PATCH` does), and issues an invitation
        while the account has no password or a `password_reset` token once it has one - earlier unused tokens of
        that purpose stop working. Every change writes its audit entry (`user_role.added`, `user_account.enabled`,
        `user_account.updated`, `invitation.issued` / `password_reset.issued`); a rerun that changes nothing records
        only the issued token. `--email` is validated by the API's rule.
  - [x] `just bootstrap-admin` needs nothing but an existing person party: `bootstrap-client`, `POST /crm/persons`
        and `bootstrap-admin` on an empty database yield an admin account with a live invitation (a DB test drives
        the three through their service entry points).
  - [x] The create-invite-redeem flow logs neither the token, the password nor the e-mail address.

**P0-7 - Own data (reach).**

- _Technique:_ `reach.py` (`ReachBasis`, `Access`, `resolve_reach`); `require_access` in `dependencies.py`, taking
  `DBSession` from `app.core.db.dependencies` - the alias moves there and `app.api.v1.common` re-exports it, so
  `test_the_crm_endpoints_carry_no_boilerplate` keeps matching the import; the alternative security requirement and
  the 403 wording in `customize_openapi`; `CrmReadAccess` and `VisibleParty` in the CRM params; the `party_ids`
  filter of `list_parties`; the two party read routes. The file maps of `crm-api.md` and `api-conventions.md` follow
  the move, and decision D of `api-conventions.md` gains one sentence: several requirements on one operation are
  alternatives, joined with `or` in the derived 403. The tests mint person tokens and need no `UserAccount` row.
- _Tests that change:_ `test_crm_route_answers_401_without_a_token` (`tests/api/test_crm_auth.py`) and
  `test_crm_reads_require_the_read_scope_and_everything_else_the_write_scope` (`tests/api/test_crm_openapi.py`), for
  the two operations only; P0-5's pin.
- _Acceptance criteria:_
  - [x] A student reads their own party and gets `404 party_not_found` for any other - the same body an unknown
        UUID produces.
  - [x] A mother with `PARENT_OF` to one child and `PAYS_FOR` to another reads both children and herself; the list
        returns exactly these three with `total == 3`; filters and paging apply within the reach.
  - [x] `Access.basis` reports `self` for the own party and `guardian` for both children.
  - [x] `TUTOR_OF` does not extend reach: a tutor does not read their student's party.
  - [x] An application token with `crm:read` behaves exactly as before on both routes and issues no query on
        `core.party_relation` (asserted by counting statements).
  - [x] A reach-aware request opens exactly one database session, for a `crm:read` and a `crm:read:own` token alike
        (the CRM test override of `get_db_session` runs once).
  - [x] A person's token with `crm:read:own` is `403` on every other CRM route (a test walks every CRM operation of the contract); an
        application token with only `crm:read:own` is `403` on both routes, with the body of a missing scope.
  - [x] `require_access(Scope.CRM_READ)` declares `crm:read:own` on its marker; a route that mixes it with
        `require_scopes(Scope.CRM_READ)` fails when the schema is built.
  - [x] Both operations list the two alternative security requirements under the scheme's key, their `403`
        description reads `Missing required scope: crm:read or crm:read:own`, and a `401` carries
        `WWW-Authenticate: Bearer scope="crm:read:own"`; the `403` of every other operation is unchanged. The two
        tests named above expect exactly that for `crm_list_parties` and `crm_get_party` and keep their rule for
        every other CRM operation; the CRM endpoints still pass `test_the_crm_endpoints_carry_no_boilerplate`; no
        other existing test changes.
  - [x] `_allowed_target_parties` in `authz.py` and `resolve_reach` return the same set for the same party.
  - [x] The conventions in `CLAUDE.md` name `require_access`.

**P0-8 - Login.**

- _Technique:_ `issue_user_token` and `refresh_user_token` in
  [`services/auth/tokens.py`](../../app/services/auth/tokens.py); sessions opened, rotated and revoked in
  `app/services/auth/sessions.py` (with `REFRESH_REUSE_GRACE`); `ClientTokenForm` becomes `TokenForm` (`grant_type`, the
  client credentials, `scope`, `username`, `password`, `refresh_token`; a parameter the grant needs and lacks is
  `invalid_request`); `create_token` takes `DBSession`, dispatches on the grant and sets
  `response_model_exclude_none=True`; `AccessTokenResponse` derives from `ApiModel`; `POST /auth/revoke`;
  `verify_and_update` in `secrets.py`; the `password` flow in `OAuth2Bearer`; the `auth` entry of `OPENAPI_TAGS`
  in [`openapi.py`](../../app/api/v1/common/openapi.py) describes person logins too. `get_issue_client_token`
  stays the dependency seam for `client_credentials` (sibling seams for `password` and `refresh_token`); the
  session fields ride on the result of `issue_user_token` / `refresh_user_token`, never on `CreatedAccessToken`.
- _Tests that change:_ exactly three tests of `tests/api/test_auth_endpoint.py`: the unsupported-grant test posts a
  grant that stays unsupported (`authorization_code`); the `invalid_request` assertion follows the reworded
  constant; the documented 400 examples grow by `invalid_grant` and `unauthorized_client`.
  `test_auth_token_endpoint_returns_access_token` stays unmodified.
- _Acceptance criteria:_
  - [x] The lifecycle test (database): create -> invite -> redeem -> `password` -> `/auth/me` -> `refresh_token` ->
        `revoke` -> the revoked refresh token is `invalid_grant`.
  - [x] Unknown e-mail, wrong password, a `disabled` account, an account without a password and a locked account
        answer the identical `invalid_grant` body; a client without `auth:users:login` is `unauthorized_client`.
  - [x] A denied `password` login writes `token.denied` with the account's id as `principal_id` when the e-mail
        matched an account and `None` when it did not; the lifecycle test finds the e-mail address in no audit row
        and no log line.
  - [x] After five wrong passwords the correct one is refused until `locked_until`; **the counter is persisted
        although the request was denied**.
  - [x] Presenting a rotated-out refresh token after the grace revokes the session and writes
        `session.reuse_detected`; within the grace it only fails and writes `token.denied` with the fixed detail.
  - [x] Two overlapping refreshes of one token yield one rotation; the other gets `invalid_grant` and the session
        stays live.
  - [x] A refresh token is refused for another client than the one that opened the session.
  - [x] A person's scopes come from the client's `delegated` grants: an admin logging in through a client whose
        only `delegated` grant is `account:self` gets exactly `account:self`.
  - [x] Removing the `admin` role and refreshing yields a token without the admin scopes; refreshing with
        `scope=account:self crm:read:own` as an admin yields exactly that, and a later refresh without `scope` the
        full session scope again; requesting more than the session's `scope` is `invalid_scope`.
  - [x] Both the login's and the refresh's token carry `amr: ["pwd"]`.
  - [x] `client_credentials` answers with exactly today's four keys; `password` succeeds with HTTP Basic client
        authentication and with `client_id` / `client_secret` in the form, Basic taking precedence as today.
  - [x] Swagger UI's "Authorize" dialog offers the `password` flow (asserted over `app.openapi()`).
  - [x] [`ARCHITECTURE.md`](../ARCHITECTURE.md) (Auth section) and the "Where we are" table of the
        [project sketch](../PROJECT.md) describe the result; the standing criteria are ticked; the status line
        reads Implemented; the PR carries `Closes #85`.

### Nice-to-have (P1)

- **P1-1 - Mail delivery.** A `Mailer` port with an SMTP implementation and `MAIL__*` settings; invitations and
  resets are sent instead of returned; a public, client-guarded "forgot password" route that always answers `202`;
  `password_reset_expire_hours` drops to 1.
- **P1-2 - Self-service.** `require_user`; `POST /auth/me/password` (current + new password, demands `amr`
  `pwd`, revokes the other sessions); `GET /auth/me/sessions`, `DELETE /auth/me/sessions/{session_id}`. Guarded by
  `account:self`.
- **P1-3 - Housekeeping.** The reaper deletes sessions and action tokens that expired more than 30 days ago.
- **P1-4 - Reach-aware writes.** `crm:write:own` and the first routes that accept it. An `:own` write never widens
  reach or roles, and every party it names must be in reach.
- **P1-5 - Remaining CRM reads** become reach-aware where a restricted view makes sense.

### Future considerations (P2)

- The bot arc items of [Designed for the bot arc](#designed-for-the-bot-arc).
- **MFA** for accounts holding a stored role.
- **Asymmetric signing + JWKS** once a second service validates SkillForge's tokens.

## Timeline / phasing

One PR per requirement, all in one GitHub stack (`gh stack`), bottom to top P0-2, P0-3, P0-4, P0-5, P0-6, P0-7,
P0-8 - an order that respects every dependency in the table. A wave is what can be built at the same time; a slice
built beside another joins the stack by rebasing onto the branch below it. The PRs merge bottom-up, each as its own
squash commit, and GitHub rebases the PRs above a merged one.

| Wave | Slices                                       | Needs                                                        |
| ---- | -------------------------------------------- | ------------------------------------------------------------ |
| 0    | **P0-1** spec and ADR                        | nothing                                                      |
| 1    | **P0-2** scope model, **P0-3** data model    | nothing (both edit `app/services/auth/scopes.py`, see below) |
| 2    | **P0-4** grant modes, **P0-5** person tokens | P0-4: P0-2 and P0-3. P0-5: P0-2                              |
| 3    | **P0-6** accounts, **P0-7** own data         | P0-6: P0-2 to P0-5. P0-7: P0-2 and P0-5                      |
| 4    | **P0-8** login                               | everything above                                             |

- All schema changes live in P0-3's single revision, so no two slices add competing Alembic heads.
- P0-7 does not wait for the login: its tests mint person tokens with `create_access_token` and a
  `UserPrincipal`, the way the CRM tests mint application tokens today.
- **Slices of one wave share files, not functions.** Wave 1: `app/services/auth/scopes.py` (P0-2 `resolve_token_scopes`,
  P0-3 `revoke_application_client_scope`); wave 2: `app/api/v1/auth/schemas.py` and `app/core/auth/__init__.py`.
  The slice that joins the stack above its wave partner resolves them like `openapi.json`.
- **`openapi.json` is never merged by hand.** After every rebase a slice takes either side of the file, reruns
  `just openapi` and `just check-all`.

## Rules for implementing agents

- The rules of [`api-conventions.md`](api-conventions.md) and [`crm-api.md`](crm-api.md) apply unchanged (English
  only, symbol references in docs, never hand-edit `openapi.json`, `just check` green before every commit,
  conventional commits).
- **Code from the withdrawn first version** is on `archive/user-auth-v1`. Take it deliberately, file by file
  (`git show archive/user-auth-v1:<path>`), and adapt it: that version had an `invited` status with the events
  `user_account.invited` / `user_account.activated`, a required e-mail, an `InvitedUserAccount` response carrying
  the invitation, enabling that needed a password, a 12-character minimum (also in docstrings),
  `GUARDIAN_RELATION_TYPES` in `services/roles.py`, one list of client grants, no `amr` and a 404 for an unknown
  `party_id` in the create body. Do not take `0011_user_accounts.py` (P0-3 writes its revision fresh) nor the
  assertions of `tests/db/auth/test_auth_users_api.py`, only its fixtures. Never cherry-pick its commits.
- **Never branch on a role** to authorize. Roles produce scopes at issuance; guards read scopes.
- **Never filter by hand.** A route that serves restricted people takes an `Access` from `require_access`; a route
  that does not keeps `require_scopes`. Do not add a `:own` scope to `OWN_VARIANT` without the routes that honor it.
- **Never raise after writing a denial.** Return the `ApiError` response (decision O).
- **Never log or persist a plaintext secret,** and never put an e-mail address into an audit row or the log
  context.
- **`bootstrap-admin` never creates a party.** The party comes through the CRM API; the auth CLI seeds clients and
  accounts only.
- The error catalog is closed. If a requirement seems to need a new class or `code`, stop and report.
- Do not touch the `bot` schema, the grant engine or `delete_party`. _(Superseded by [bot-decoupling.md](bot-decoupling.md).)_
