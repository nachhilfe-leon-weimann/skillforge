# Spec: Principals & Provisioning (identity, account linking, delegated actors)

> Status: Superseded by [bot-decoupling.md](bot-decoupling.md) (2026-09); was Implemented (P0-P2 shipped on #4)
> Tracking: [#4](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/4)
> Foundational capability: the transition flows already require an **active** principal that the API
> cannot create today. Adjacent to the off-boarding arc ([#47](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/47)),
> which flips the same `active` flags in the other direction.

## Problem statement

The bot API can read *who* a principal is but cannot establish or change it.

- **No entry into the system.** `DiscordUser` (`bot` schema) and `DiscordAccount` (`ext` schema) are
  never created in `app/services` or `app/api` - only read. A grep for inserts of `DiscordUser`,
  `DiscordAccount`, `PermissionGrant`, `DiscordUserPermissionGroup` outside migrations returns nothing.
  Today these rows exist only via the migration baseline or manual DB edits;
  [`bootstrap`](../../app/cli/bootstrap.py) only seeds the *OAuth application client* (`skillbot`),
  not Discord users.
- **The transitions depend on a precondition the API can't set.** `prepare_*` calls
  `_require_active_user` ([`transitions.py`](../../app/services/bot/transitions.py)); an inactive or
  unknown Discord member cannot be activated, and nothing in the API can make them active.
- **Delegation is modeled but not authorized.** `PartyRelationType` has `PARENT_OF`, `TUTOR_OF`,
  `PAYS_FOR` ([`party_relation.py`](../../app/core/db/models/core/party_relation.py)), and the
  operational profile even *filters `PAYS_FOR` out* ([`schemas.py`](../../app/api/v1/bot/schemas.py)).
  So the data says "A pays for / parents B" but the authorization layer has no notion of A *acting on
  behalf of* B.

Every Discord onboarding, role change, or account link is therefore an out-of-band DB operation - the
opposite of the spec-first, contract-first discipline the rest of the bot API follows.

## What already exists (do not rebuild)

The read/evaluation half is in place and should be reused, not replaced:

- **Identity link:** `DiscordAccount` maps `discord_id -> party_id` with `is_primary`/`active` and a
  partial unique index guaranteeing **one primary, active account per party**
  ([`ext/discord_account.py`](../../app/core/db/models/ext/discord_account.py)).
- **Principal assembly:** [`principals.py`](../../app/services/bot/principals.py) resolves a
  `DiscordUser` into group keys + effective permission keys + party profile.
- **Permission engine:** `PermissionGroup`, `DiscordUserPermissionGroup`, `PermissionGrant`
  (subjects `USER` / `GROUP` / `ROLE`, `ALLOW`/`DENY` with priority; highest priority wins, ties DENY).
  Role-scoped grants are guild-specific via role bindings.

The gap is the **write surface** over this model and the **delegation semantics** on top of it.

## Goals

1. **First-class onboarding.** The bot can register/upsert a `DiscordUser` (role, nick, active) and
   link a Discord account to a `Party` through the API, idempotently.
2. **Unblock the transition precondition.** After onboarding, `_require_active_user` is satisfiable
   without touching the DB by hand.
3. **Decouple bot identity from CRM identity.** A `DiscordUser` may exist before any `Party`/profile
   (the principal's `profile` is already optional). Registration and account-linking are distinct steps.
4. **A delegation model (P2).** Make "principal A may act on behalf of party B" explicit via a
   server-side authorization check, deriving the allowed target parties from `PartyRelation` and
   combining them with the existing grant engine.
5. **Contract-first.** Endpoints land in `openapi.json`; consumers regenerate clients (ADR 0001).

## Non-goals

- **Replacing the permission engine.** The grant/group machinery stays; we only add a write surface
  and the delegation layer on top.
- **A general CRM/party CRUD API.** Creating people/companies/contact-infos is the CRM's job; this spec
  links an *existing* party and never auto-creates one (CRM-owned).
- **Discord-side enforcement.** Forge stores and answers authorization; it never calls Discord
  (ADR 0003 two-phase discipline still holds).
- **Self-service role escalation.** Who may change roles is gated, not open.

## Decisions

| Topic | Decision | Rationale |
|---|---|---|
| **Source of truth** | **Bot-driven upsert.** The bot observes Discord events and `PUT`s the `DiscordUser`; Forge does not poll Discord. It registers **only relevant roles** (student, tutor). | The bot is the only component that sees Discord membership; mirrors the prepare/commit "bot acts, Forge records" split. |
| **Party precondition** | **Account linking requires an existing party. No auto-creation.** The CRM party (student/tutor) must already be in the DB; the bot links a `DiscordAccount` to it, never creates it. | Party/CRM ownership stays on the CRM side; Forge only links identities. |
| **Idempotency** | Register/link are **idempotent upserts** (re-register = update, not 409), keyed on `discord_id`. | Same at-least-once retry reality as jobs ([#48](https://github.com/Nachhilfe-Leon-Weimann/skillforge/issues/48)); no double rows. |
| **Identity decoupling** | Two steps: register `DiscordUser` (bot identity), then link `DiscordAccount -> Party`. A registered user may exist before its link, but the link's party must pre-exist. | `BotPrincipal.profile` is already `| None`; registration and CRM linkage are distinct. |
| **Auth scope** | Provisioning (register / link / manage users) = **`BOT_WRITE`**. No new admin scope. `BOT_ADMIN`, if ever, is only for client management - already covered by existing client endpoints + permission grants. | The bot is a trusted `BOT_WRITE` client; per-human gating is by grants (below), not by OAuth scope. |
| **Role & privileged changes** | `DiscordUser.role` and similar privileged mutations are set by **workflows gated by permission grants** in the `bot` schema, not a bespoke admin endpoint. Forge exposes the effective `permissions` on the principal; "who may run which workflow" is evaluated against the grant engine. | The grant engine (`PermissionGrant`, USER/GROUP/ROLE, priority/DENY) already ships and is the single authz mechanism. |
| **(De)activation ownership** | Registration here only sets `active = true`; flipping `active` off + teardown is owned by the off-boarding arc (#47). | One identity model, two directions. |

## P2 delegation decisions

A `PermissionGrant` gates *whether* a principal may perform an `action_key`; it does not encode *on
which target party*. The delegation layer adds that missing "on whose behalf" dimension:

| Topic | Decision | Rationale |
|---|---|---|
| **Mechanism** | Derive the allowed target parties from `PartyRelation` (no schema change); do **not** add a target dimension to `PermissionGrant`. | Relations already model the parent/payer link; keeps grants as the pure "whether" layer. |
| **Delegating relations** | `PARENT_OF` and `PAYS_FOR` (the relation's `to_party`), plus the actor's own party. `TUTOR_OF` does **not** delegate. | Tutor authority runs through role/grants, not delegation. |
| **Enforcement** | A server-side check endpoint (`POST /bot/authz/check`) owns the full "whether AND on-whom" decision; `acts_for` is **not** exposed on the principal view. | One authoritative decision point; the bot asks instead of reimplementing the AND. |
| **Grant scope** | Guild-agnostic `USER`/`GROUP` grants only (as in the principal lookup); role-scoped (guild-specific) grants stay out of scope. | Matches `get_principal_view`; role grants are a later extension. |

## User stories

- *As SkillBot* I want to register a member the moment they join, so a subsequent activation doesn't
  fail on an unknown/inactive principal.
- *As SkillBot* I want to link a member's Discord account to their CRM party, so the principal view
  carries the operational profile.
- *As an operator* I want role and active-state changes to go through the API (and into the OpenAPI
  contract), not raw SQL, so identity changes are auditable and reproducible.
- *As SkillBot* I want to ask whether a parent/payer may act for a student, so delegated commands are
  authorized consistently with direct ones.

## Requirements

### Must-have (P0) - onboarding entry

**P0-1 - Register/upsert a Discord user.** Idempotent write of `discord_id`, `role`, `nick_name`,
`active`.
- [x] New user is created; existing user is updated (no 409).
- [x] Validation matches the model (`nick_name` non-empty).
- [x] `_require_active_user` is satisfiable end-to-end via the API afterwards.

**P0-2 - Link a Discord account to a party.** Write `DiscordAccount(discord_id -> party_id, is_primary,
active)`.
- [x] Respects the one-primary-active-per-party partial unique index (promoting a new primary demotes
      the old one in the same transaction).
- [x] Re-linking the same pair is idempotent.
- [x] Linking to a non-existent party returns a clear error (no auto-create).

**P0-3 - Contract & docs.** Endpoints in `openapi.json`; `just openapi-check` green.
- [x] Provisioning + authz endpoints are in `openapi.json` (`/bot/users/*`, `/bot/authz/check`).

### Nice-to-have (P1) - lifecycle of identity

- **P1-1 - Role and active-state changes** as workflow actions gated by permission grants.
- **P1-2 - Group membership management** (`DiscordUserPermissionGroup`) via API.
- **P1-3 - Unlink / re-point an account** (move a `discord_id` to a different party; deactivate a link).

### P2 - delegated actors

- **P2-1 - Authorization check endpoint.** `POST /bot/authz/check` with `{actor_discord_id,
  action_key, target_party_id} -> {allowed}`, combining the grant engine ("whether") with the
  delegation set ("on whose behalf"). `BotRead`.
- **P2-2 - Delegation derivation.** The allowed target parties are the actor's own party plus the
  `to_party` of its outgoing `PARENT_OF` / `PAYS_FOR` relations. An inactive actor is denied; an
  unknown actor is a 404.
- *Dropped:* exposing `acts_for` on the principal view and un-filtering `PAYS_FOR` in the profile -
  with server-side enforcement the bot never needs the raw delegation data.

## Timeline / phasing

1. **P0** first - the smallest cut that unblocks activation (register + link). This is the only part
   strictly required before #47 / the activation flows are fully self-contained.
2. **P1** as a fast follow once P0 is in prod and the bot drives real onboarding events.
3. **P2 (delegation)** as its own sub-arc with its own decision pass - it is an authorization change,
   not just a write surface, and deserves separate review.

**Depends on:** nothing - all phases decided. **Relates to:** #47 (off-boarding shares the identity
model), #48 (same idempotency-on-retry reality).
