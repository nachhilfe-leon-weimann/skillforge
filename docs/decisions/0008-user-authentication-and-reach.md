# ADR 0008 - SkillForge authenticates people itself; client grants have a mode; scopes carry a reach qualifier

Status: Accepted, 2026-09; amended by [0009](0009-bot-owns-its-discord-workflows.md) (2026-09)

This record replaces a draft of the same number that was withdrawn before any release (its code and
text are kept on the branch `archive/user-auth-v1`). The draft let one list of client grants count
both for the client itself and as the ceiling for people, and it tied an account's activation to a
password. Both no longer fit the [project sketch](../PROJECT.md).

## Context

SkillForge authenticates applications only: `POST /auth/token` knows `client_credentials`, and a
scope such as `crm:read` means every record. The project sketch sets the direction this has to
serve:

- **People use SkillForge through frontends.** The portal acts for the person who logged in. The bot
  acts for the person who typed a command. Both are clients acting on behalf of a person, and the
  same person must get the same rights through either (principles 5 and 6 of the sketch).
- **The client is the ceiling.** A frontend may never do more for a person than it may do for
  people at all - and some clients also do work no person asked for.
- **The account is the door.** Only people with an account use authenticated features. Some of
  them only ever use Discord and have no e-mail address of their own.
- **"Only mine" needs a meaning.** A student holding `crm:read` would see every party. The only
  record-level rule today is the bot's delegation check (`check_authorization` in
  [`authz.py`](../../app/services/bot/authz.py)), keyed on Discord users.

Neither SkillForge's user-facing surface nor the portal exists yet; nothing but SkillBot's
`client_credentials` flow depends on the auth core. Reshaping it costs nothing now.

## Decision

**1. SkillForge is the identity provider for people.** Accounts, password hashes and sessions live
in the `auth` schema. An account belongs to exactly one person party. Accounts are created only
through `auth:users:manage`: by admins, or by a client an admin entrusts with it, such as an
automated intake.

**2. A client grant has a mode.** `application` grants are what the client may do for itself
(`client_credentials`). `delegated` grants are the most it may do for any person. The same scope can
be granted in either mode or both. Scopes that only make sense for a client - `auth:users:login`,
later `auth:users:exchange` - exist in `application` mode only.

**3. One token endpoint, one token model.** A token for a person is always issued _to a client on
behalf of a person_: `sub` names the account, `azp` the client. Its scopes are computed once, at
issuance, as the client's `delegated` grants intersected with what the person's roles allow, and
narrowed further if the client asked for less. A request is validated the same way for both kinds
of token.

**4. The account is not the login.** An account is `active` or `disabled`; how a person proves who
they are hangs off it. In this arc that is a password, used by the portal. The bot arc adds Discord:
the bot tells SkillForge which linked Discord user sent a command and receives a token for that
person (a token exchange). The two ways differ in strength - with a password the person proves
something, with Discord the bot vouches for them - so every token for a person records how it was
obtained (`amr`), and sensitive account changes can demand a password.

**5. Scopes carry a reach qualifier.** `crm:read` means all records; `crm:read:own` means the records
within the caller's reach. The unqualified scope implies the qualified one. A route guarded with
`require_scopes(Scope.CRM_READ)` keeps demanding the unqualified scope; a route that can filter opts
in with `require_access(Scope.CRM_READ)` and receives an `Access`.

**6. Reach is derived per request and remembers why.** It is the caller's own party plus the
`to_party` of their outgoing `PARENT_OF` and `PAYS_FOR` relations - the delegation set the bot already
uses. It is not stored in the token. Each reachable party carries its basis (`self`, `guardian`), so
that tutor reach can later show a tutor fewer fields than a guardian sees.

**7. Roles are derived from the CRM - except `admin`.** `student`, `tutor` and `guardian` follow from
CRM data; `admin` is stored on the account. A role maps to scopes in code. Roles are a set, and
SkillForge authorizes by scope, never by role.

Deliberately _not_:

- **No external identity provider** (Keycloak, Authentik, Zitadel): a second stateful service and a
  second user store to keep in line with the CRM, for a few hundred accounts.
- **No identity in skillsite:** identity would live outside the system of record, and SkillForge
  would have to believe the portal about who is calling.
- **No `admin` in the CRM:** it is an access right, not a fact about a person. As a CRM role, anyone
  holding `crm:write` could make anyone an admin.
- **No impersonation by any client.** Only a client granted `auth:users:exchange` may speak for a
  person without their credential, and only for a person linked through the identity it vouches
  for. The portal cannot.
- **No global reach claim.** A route that forgets to filter answers `403` instead of showing
  everything.
- **No asymmetric signing, no JWKS:** SkillForge is the only party that validates its tokens.
- **No password rules beyond length:** 8 to 128 characters, no composition rules, no list of
  forbidden passwords. Lockout and the portal's per-IP limit throttle guessing, a restricted
  person only reads their own data, and MFA for stored roles is planned.

## Consequences

- One guard per route, one place to declare it, one validation path. A new capability for people
  is a scope pair plus an opt-in on the routes that serve it.
- **Safe by default.** A restricted person reaches only routes that were made reach-aware on
  purpose; the rollout goes route by route.
- **The `password` grant is a conscious compromise.** OAuth 2.1 drops it because the client sees the
  password. Here the only such client is a first-party backend that renders the login form anyway,
  and using the grant is itself an `application` grant (`auth:users:login`).
- **The bot's secret speaks for every linked person** once the token exchange exists. Accepted for
  this platform; the ceiling keeps it to what the bot may do for people, and `amr` keeps
  password-only actions out of its reach.
- SkillForge now owns security-sensitive code: password hashing, lockout, refresh rotation with
  reuse detection, one-time tokens. The spec pins each of them down.
- **Access tokens stay stateless** (15 minutes). Disabling an account or removing a role takes effect
  at the next refresh at the latest.
- Reach costs one indexed query per request of a restricted token; unrestricted tokens pay nothing.
- The login e-mail address lives on the account, not in `ContactInfo`: CRM addresses are unique per
  party only (siblings share a parent's), a login identifier must be unique everywhere, and it must
  not be changeable through `crm:write`.
- Out-of-reach records answer `404`, not `403`, so a restricted caller cannot probe which parties
  exist.
- Deleting a person party deletes its account (`ON DELETE CASCADE`); the CRM stays unaware of
  accounts. For people linked to Discord the CRM's existing `party_in_use` guard refuses the delete
  anyway.

Implementation: [`user-authentication.md`](../specs/user-authentication.md).
