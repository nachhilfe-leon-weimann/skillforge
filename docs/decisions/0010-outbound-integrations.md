# ADR 0010 - SkillForge calls third-party systems itself, through one guarded transport

Status: Proposed, 2026-11. It becomes `Accepted` when [`billing.md`](../specs/billing.md) is frozen (its slice
SP-SD): the Exchange restriction to one mailbox ran on the tenant (SP-CAL), the check of the remote account's
identity is not verified on the real system yet (SP-SD), and the body of an accepted record never changes. Once
accepted it amends [0006](0006-error-envelope.md) (point 10); 0006's status line changes then, not before.

## Context

The billing arc ([`billing.md`](../specs/billing.md)) is the first time SkillForge needs another company's system:
sevDesk, where invoices are written, and Microsoft 365, where the lesson calendar lives. The package
`app/integrations/` has been reserved for this since the first commit.

What frames the decision:

- **The sketch says SkillForge pushes nothing** ([`PROJECT.md`](../PROJECT.md), principle 3), and
  [ADR 0009](0009-bot-owns-its-discord-workflows.md) removed the job queue: "A future integration sync (sevDesk,
  Clockodo, Microsoft) gets its own decision." This is that decision.
- **sevDesk does not forgive.** Its API has no sandbox, creating an invoice is not idempotent, no documented call
  deletes a draft, and an issued invoice can only be cancelled. A trial account and the real account answer on the
  same host, and a token belongs to one user ("Verified behavior" in `billing.md`).
- **In Microsoft 365, permissions add up.** An application permission granted in Entra reaches every mailbox of the
  tenant, whatever Exchange restricts.
- **Agents write and test this code.** A test, a script or a mistaken setting must not be able to reach a real
  account.
- **The API knows three kinds of failure** ([ADR 0006](0006-error-envelope.md)): not found, conflict, a broken
  domain rule. "The other system did not answer" is none of them.

## Decision

**SkillForge calls third-party systems itself, synchronously, from the service that needs the result - and every
call leaves through one guarded transport.**

1. **Only `app/integrations/` talks to a third-party system.** Services call an integration client; nothing else
   in `app/` opens an outbound connection. Frontends never call these systems, and SkillForge offers no route that
   passes a request through to one.
2. **A write is four steps: pending row, commit, call, record the outcome.** The row that describes the intended
   write is committed before the call, the outcome is stored in a second transaction. An outcome is _succeeded_,
   _failed_ or _unknown_. _Failed_ needs proof that nothing was created: the system refused the request, or
   SkillForge never sent it. Everything unproven - no answer, an answer that cannot be read, an error that does
   not say what happened - is _unknown_. Unknown is a state of its own: a reconcile - a read that looks for the
   object by a reference SkillForge wrote into it - resolves it to one of the other two.
3. **A write is never retried** - not by the client, not by the transport, not by a worker. A read may be repeated,
   and a failed read never changes stored money or state.
4. **No job queue, no outbox, no background sync** - in line with ADR 0009. A call happens in the request, or the
   command-line run, of the person who wants its result.
5. **This is not pushing.** Principle 3 is about SkillForge's consumers: it does not know them and sends them
   nothing. In a call to sevDesk or Microsoft Graph, SkillForge is the client of a system that one of its own
   domain rules needs - writing an invoice draft is the domain action, the calendar is its input. No frontend is
   told anything, and frontends keep pulling. The sketch says so in principle 3.
6. **One guarded HTTP transport.** Each client names the hosts it may reach; a request to any other host is
   refused. The mode is `dry_run` unless configuration says `live`. The mode decides writes: in `dry_run` a write
   returns the request it would have sent and sends nothing, while a read is sent - a health check of the
   configuration and a preview need reads. Under pytest there is no real network, and `live` is refused - except
   in the trial runs of point 8.
7. **The remote account is checked first.** Before any other call a client reads which account it is talking to
   and compares it with what configuration expects; on a mismatch it refuses to work. The one exception is a
   read-only discovery that prints candidate account ids, so that the expected id can be configured the first
   time.
8. **sevDesk has no dedicated API user.** The token is the token of the maintainer's own sevDesk user. It exists
   in two places only, the maintainer's password manager and the production configuration, and never anywhere an
   agent can reach. A separate trial account with synthetic data takes every write test; it lasts 14 days. Agents
   reach it only through the three trial recipes - `just sevdesk-doctor trial` and `just contract-sevdesk`, read
   only, and `just sevdesk-spike`, introduced by SF1-3a and SF1-3b of `billing.md`, never in CI - and only after
   the maintainer's go in the session. `just sevdesk-doctor live` is the maintainer's alone.
9. **The Entra app `skillforge` is SkillForge's identity in Microsoft 365** - not an app for the calendar alone.
   Every feature adds its own Exchange "RBAC for Applications" role assignment, restricted by its own management
   scope; the first is `Application Calendars.Read` on the one lesson mailbox. The app never receives an org-wide
   application permission in Entra (`Calendars.Read`, for instance): Entra grants and Exchange grants are a union,
   so one Entra grant lifts every restriction. Each place SkillForge is deployed to - the spike on the maintainer's
   machine, production - has its own credential, a certificate, with a reminder before it expires.
10. **A failing third-party system is its own kind of API error.** The taxonomy of
    [ADR 0006](0006-error-envelope.md) gains a fourth category, `UpstreamError`, mapped to `502` in
    `STATUS_BY_ERROR`. Integration clients raise their own errors, which are no `DomainError`; the service that
    called translates them, and endpoints still catch nothing. This amends ADR 0006.
11. **Credentials and account-specific ids are configuration** (`SEVDESK__*`, `GRAPH__*`), never literals in code,
    tests or docs. The integration settings are optional: SkillForge starts without them.

Deliberately _not_:

- **No retry of a write, and no worker that finishes one later.** A second request could create a second invoice.
- **No call from a frontend,** no pass-through route, no token of a third-party system outside SkillForge.
- **No webhooks,** in either direction.
- **No generic integration framework.** A client is thin and hand-written and covers the calls a slice needs.
- **No call to a real account from a test, from CI or from an agent session,** and no test object in one. The
  trial is reached only as in point 8.
- **No shared credential** across deployment locations, and no application permission in Entra "for now".

## Consequences

- A request that writes to a third-party system is as slow as that system and can end as _unknown_. A user
  interface offers reconcile then, never "try again".
- The write path commits twice. It is the one place where a service flow commits in the middle of a request -
  deliberately, so that no call is ever made without a stored trace.
- A timeout costs a manual reconcile instead of a possible duplicate. For one tutoring business's invoices that is
  the better trade.
- The guards in code catch accidents. What keeps agents away from the real sevDesk account is that its token is
  nowhere they can reach - and since the trial answers on the same host, the identity check is what catches a
  swapped token.
- The sevDesk token cannot be revoked apart from the maintainer's own access; rotating it means a new token and a
  change of the production configuration.
- The trial account expires. Recorded fixtures carry the tests afterwards, the trial runs then skip, and CI never
  depends on them.
- The calendar restriction lives in Exchange, outside the repository: a script sets it up, the maintainer runs it
  and checks it with `Test-ServicePrincipalAuthorization`, and a change takes up to two hours to apply. One
  certificate per location makes expiry a calendar entry per location - and a leaked one is revoked alone.
- A client of the API sees a `502` with its own `code` when a third-party system fails, and can tell it from its
  own mistake (`4xx`) and from a bug in SkillForge (`500`).
- `httpx` becomes a direct runtime dependency (it is already installed through `fastapi[standard]`).

Implementation: [`billing.md`](../specs/billing.md), slices SP-CAL, SP-SD, SF1-1 to SF1-3b, SF4-1, SF5-4, SF6-1
and SF6-2.
