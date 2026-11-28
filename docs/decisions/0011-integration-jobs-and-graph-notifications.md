# ADR 0011 - Integration work may run as jobs, and Microsoft Graph may notify SkillForge

Status: Accepted, 2026-11. Amends [0010](0010-outbound-integrations.md) (points 2, 3, 4, 6, 7, 9 and 11, and
"No webhooks").
Takes the decision [0009](0009-bot-owns-its-discord-workflows.md) left open: "A future integration sync (sevDesk,
Clockodo, Microsoft) gets its own decision."

## Context

The lesson calendar's events carry an Outlook category by medium, set by hand so far. SkillForge is to set it
([`calendar-categories.md`](../specs/calendar-categories.md)): when an event appears or changes, without a person
asking, in every lesson mailbox.

[ADR 0010](0010-outbound-integrations.md) cannot carry that:

- **A call happens "in the request, or the command-line run, of the person who wants its result"** (point 4). Here
  nobody asks: the calendar changes, and SkillForge reacts.
- **"No webhooks, in either direction."** Microsoft Graph reports a changed event only through a change
  notification, an HTTP request to SkillForge that expects an answer within three seconds.
- **"A write is never retried"** (point 3). The rule protects sevDesk, where a second request creates a second
  invoice. A category that is set only on an event without one cannot be set twice.
- **The app holds `Application Calendars.Read` on one mailbox** (point 9). Setting a category is a write, the
  category list is a mailbox setting, and there will be several mailboxes.
- **`live` is refused under pytest** (point 6), and Graph has no trial: no test can see a write's answer, such as
  the subscription Graph created.
- **The mailbox is configuration** (points 7 and 11): the identity check compares the remote account with a
  setting. A mailbox that can be added without a deploy is a row.

[ADR 0009](0009-bot-owns-its-discord-workflows.md) removed the job queue of
[ADR 0004](0004-forge-first-job-queue.md) because a consumer was meant to poll it and none did.

## Decision

**SkillForge may do its own integration work in the background, as jobs in its own database, and may let Microsoft
Graph tell it that something changed.**

1. **A job queue for SkillForge's own work.** `system.job` in Postgres, claimed with `FOR UPDATE SKIP LOCKED` by
   SkillForge's own worker process. No consumer reads it and no route exposes it: it is not the queue of ADR 0004.
   Delivery is at least once, so every handler is idempotent. The queue has kinds, a due time, a bounded number of
   attempts with backoff and a final `failed` state - no priorities, no recurring schedules, no broker.
2. **A change notification is a signal, never data.** SkillForge accepts Microsoft Graph's change and lifecycle
   notifications, checks the `clientState` it gave the subscription, stores a job and answers. Nothing in a
   notification is trusted beyond "look at this event again": the job reads the event itself. A notification
   SkillForge cannot attribute is dropped. This is the one inbound webhook; another source needs its own decision.
3. **SkillForge still pushes nothing.** Principle 3 of the [project sketch](../PROJECT.md) is about SkillForge's
   consumers. Receiving a signal from a system whose client SkillForge is tells no consumer anything.
4. **A job may repeat a write that reads first and finds its own effect.** Setting a category on an event that has
   none, creating a category the list lacks, ensuring a subscription by listing the existing ones: the repeat reads
   the current state and stops if the effect is there. For such a write the job is the stored trace that point 2
   of ADR 0010 asks for, and reading again is its reconcile. Every other write keeps points 2 and 3 of ADR 0010 -
   a sevDesk write above all: never retried, reconciled.
5. **Exchange grants, still without any Entra application permission.** The app `skillforge` holds
   `Application Calendars.Read` (a subscription needs a read permission), `Application Calendars.ReadWrite` (the
   category of an event) and `Application MailboxSettings.ReadWrite` (the category list), all restricted by one
   management scope: the direct members of one mail-enabled security group. A new lesson mailbox joins the group.
   Which mailboxes SkillForge works on is data, not configuration; before anything else a client confirms that
   the mailbox's default calendar belongs to the address the row names.
6. **If Graph refuses a subscription under these grants, SkillForge polls.** Whether "RBAC for Applications" alone
   authorises a subscription is not documented. If the probe fails, a job reads `calendarView/delta` on a schedule
   instead. An Entra `Calendars.Read` grant fenced by an application access policy stays excluded: it is tenant-wide
   the moment the policy is gone.
7. **A final failure is reported to a Discord webhook.** It is an outbound integration like any other - the guarded
   transport, a host allowlist, the URL a secret - and an operations channel: it carries a job's kind, id and error
   class and nothing of the calendar. It has no `dry_run`: a report that is only planned reports nothing. It is
   not the Discord API the bot works with, and it tells no consumer anything about central data.
8. **Under pytest, `live` is allowed on an `httpx.MockTransport`.** A mock transport cannot reach a network, so a
   test may see the answer of a Graph write. Real network under pytest stays refused as before.

Deliberately _not_:

- **No retry of a write that creates something a read cannot find again.** sevDesk stays as ADR 0010 decided.
- **No notification with resource data.** A subscription carries no event content, so SkillForge needs no
  decryption certificate and stores no calendar content it did not read itself.
- **No cron and no scheduler.** What must happen later - renewing a subscription - is a job with a due time.
- **No route for a frontend into the queue,** and no job that exists for one frontend's workflow.
- **No message broker.** Postgres carries the load of one tutoring business.

## Consequences

- SkillForge gains a second worker process and a second way in: two unauthenticated routes that anyone can call.
  What they accept is bounded by the `clientState` check, and what they cause is one deduplicated job.
- A category appears seconds after the event, not at once. What a lost notification or a missing subscription
  left out is found by a delta read: when Graph reports missed notifications, and whenever SkillForge creates a
  subscription.
- `/health` gates the deploy and the API container, and it counts every worker. The jobs worker therefore beats
  whatever its jobs do: a calendar that cannot be reached fails jobs, never the API.
- A subscription lives seven days at most. SkillForge renews it from a due job; a worker that is down for days
  loses the subscription and creates it again at its next start.
- `Application MailboxSettings.ReadWrite` also reaches the other settings of the mailboxes in the group, such as
  automatic replies. The scope keeps that to the lesson mailboxes.
- Handlers carry the idempotency obligation, and a job stuck in `running` is taken again once its lease passed.
- The guards of ADR 0010 hold for everything else: one transport, host allowlists, `dry_run` by default, no call to
  a real account from a test, CI or an agent session.

Implementation: [`calendar-categories.md`](../specs/calendar-categories.md).
