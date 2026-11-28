# Spec: Calendar categories (events get their category by medium, driven by Graph notifications)

> Status: In progress - P0-1 | Arc `calendar`
> Builds on the [project sketch](../PROJECT.md),
> [ADR 0011](../decisions/0011-integration-jobs-and-graph-notifications.md),
> [ADR 0010](../decisions/0010-outbound-integrations.md), [`api-conventions.md`](api-conventions.md) and the calendar
> client of [`billing.md`](billing.md) (SF4-1).
> Written to be executed by coding agents: every requirement names its symbols, files and checkable criteria.

## Problem statement

The lesson calendar is the default Outlook calendar of a lesson mailbox. Its events carry a category by medium -
`Discord`, `Teams`, `Telefon` - and `Abgesagt` for a late cancellation. The maintainer sets all four by hand. Most
events come from a booking tool, some are made by hand; both follow the title convention `<subject> mit <name>`.

Three of the four categories follow from the event itself, so setting them is work a rule can do. SkillForge already
reads this calendar through Microsoft Graph for billing, but only when a person asks, with read access to one
mailbox, and [ADR 0010](../decisions/0010-outbound-integrations.md) allows neither background work nor a webhook.
[ADR 0011](../decisions/0011-integration-jobs-and-graph-notifications.md) changes that; this spec builds on it.

## Goals

1. **A new or changed event gets its category within about a minute,** without a person, a cron job or a calendar
   poll: Microsoft Graph tells SkillForge that an event changed.
2. **A category set by anyone is never changed** - not at once and not after a later change of the event.
3. **`Abgesagt` stays the maintainer's.** No rule sets it, and marking one occurrence of a series keeps working.
4. **Any number of lesson mailboxes,** added and switched off without a deploy.
5. **Nothing is lost on a restart** of the API or the worker, and a job that fails for good is reported.
6. **Moving many events at once is ordinary load:** a burst of notifications costs one job and one read per event.
7. **The queue can carry later kinds** by adding a kind and its handler; each brings its own decision on its writes
   (ADR 0011, point 4).

## Non-goals

- **Setting `Abgesagt`,** or reading a cancellation out of an event.
- **Touching an occurrence or an exception of a series.** Only a single event and a series master are written.
- **Changing or removing a category,** one in the mailbox's category list included.
- **Notifications with resource data,** and with them a decryption certificate.
- **A second Entra app,** or any Entra application permission (ADR 0010, point 9).
- **A user interface.** The portal may call the mailbox routes later; that is skillsite's work.
- **Changing the billing import.** It keeps reading `GRAPH__MAILBOX` and `GRAPH__CALENDAR_NAME`.
- **A generic scheduler:** no recurring schedules, no priorities, no route into the queue. A handler may store its
  successor with a due time; nothing else repeats.

## Decided defaults

| Topic | Decision | Rationale |
|---|---|---|
| **A - Trigger** | Graph change notifications (`created`, `updated`) on `users/{mailbox}/events`, without resource data. A notification only names an event; the job reads it. | The maintainer's requirement; ADR 0011, point 2. |
| **B - Queue** | `system.job` in Postgres, claimed with `FOR UPDATE SKIP LOCKED`; status `pending`, `running`, `succeeded`, `failed`. A claim commits `running`, the attempt and a lease of `JOB_LEASE` (10 min). A `running` job whose lease passed is claimed again - or, with no attempt left, finished as `failed` by that claim. `succeed` and `fail` match the job's id and the claimed `attempts`. | Survives restarts without a broker. Counting the attempt at the claim bounds a job that kills its worker; the match keeps a stale holder from overwriting a newer claim. The lease lies above `READ_DEADLINE_SECONDS`. |
| **C - One pending job per thing** | A job may carry a `dedupe_key`; among `pending` jobs a kind and a key are unique, and `enqueue` is one `INSERT ... ON CONFLICT`. A job that fails while a pending job of its kind and key exists is deleted instead of returned to `pending`; that twin takes the greater `attempts`. | Graph sends several notifications for one change, and many when events are moved around. A retry must not collide with the job that already replaces it. |
| **D - Debounce** | A `calendar.tag_event` job is due `TAG_DELAY` (10 s) after the last notification for its event, at most `TAG_MAX_DELAY` (60 s) after the first: the conflict moves `available_at`. | A burst for one event folds into one job that reads the event once it is quiet - a booking tool may create an event and add its meeting a moment later. |
| **E - Retry** | A failed attempt makes the job due again after `RETRY_BASE` (1 min) doubled per attempt, at most `RETRY_CAP` (1 h); a `Retry-After` longer than that wins, up to the cap. After `DEFAULT_MAX_ATTEMPTS` (8) the job is `failed`. | About two hours in total: the time Exchange may take to apply a new grant. |
| **F - Worker** | A second worker process, `python -m app.workers.jobs`, the compose service `jobs`. It looks for a due job every `POLL_INTERVAL` (5 s) and works one job at a time. It beats for `/health` as `WorkerName.JOBS` from its own task, whatever job runs; only a failing claim, `succeed` or `fail` degrades the beat - a failing handler never does. | One job at a time stays far below Outlook's four concurrent requests per mailbox. `/health` gates the deploy and the API container: a calendar that cannot be reached must not take the API down. |
| **G - Unknown kind** | A job whose kind has no handler is not an attempt: it is made due again after `RETRY_CAP` and logged. | During a deploy the old worker can meet a kind the new API stored; wave 1 stores kinds whose handlers come with wave 2. |
| **H - Housekeeping** | The housekeeping worker deletes `succeeded` jobs 30 days and `failed` jobs 90 days after `finished_at`, as one more pass of `PASSES`. | The existing place for expiring rows; a failed job stays longer as evidence. |
| **I - Failure report** | When a job becomes `failed`, the worker posts its kind, id, attempts and error class to `ALERTS__DISCORD_WEBHOOK_URL`. Without the setting it only logs. The post has no dry run and is never repeated. | ADR 0011, point 7. A failing alert must not fail or loop the job. |
| **J - Mailboxes are data** | `calendar.mailbox` holds an address, `active`, an optional rule override and the mailbox's subscription and delta state. Routes under `/api/v1/calendar/mailboxes` write it and store the job that brings Graph in line. Only a mailbox that exists for lessons is added. | Adding a mailbox needs no deploy and no restart. A person's own mailbox holds what SkillForge has no business reading. |
| **K - Scope** | A new scope `calendar:manage`, in the admin role, not in `VOUCHED_SCOPES`. | Who may add a mailbox decides whose calendar SkillForge writes. |
| **L - `clientState`** | A random value per subscription (`secrets.token_urlsafe(48)`). The mailbox stores its SHA-256 digest before the subscription is created; a notification's value is digested and compared with `hmac.compare_digest`. | Graph returns it in every notification and never in a listing. A copy of the database must not be able to forge a notification. |
| **M - Rules are configuration** | `CALENDAR__CATEGORIES` and `CALENDAR__RULES` (JSON) hold the categories with their colours and the ordered rules, each a title pattern with the conditions `teams` and `phone`; a mailbox's `rules` replace the global rules for it. The defaults are the maintainer's rules, in the settings class. A `title` is at most 200 characters and must compile. | Patterns and categories change without a code change; the app still works unconfigured. |
| **N - What stays code** | An event with any category is left alone; only `singleInstance` and `seriesMaster` are written; a rule may not name a category marked `manual`. | These are the promises of goals 2 and 3, not preferences. |
| **O - Teams** | An event is a Teams meeting when `isOnlineMeeting` is true or `onlineMeetingProvider` is `teamsForBusiness`. | The maintainer's rule; SP-CAL found both set on every Teams lesson. |
| **P - Series** | The category goes on the series master. Before and after that write the job reads the master with `$expand=exceptionOccurrences`; a category that changed on an exception is reported through the failure report as `ExceptionCategoryChanged`, the job succeeds, and nothing is written back. | Whether a master update keeps an exception's category is not documented (SP-2). |
| **Q - Delta** | `GET /users/{mailbox}/calendarView/delta` from 7 days back to 365 days ahead. The `@odata.deltaLink` is stored; a window older than `SYNC_WINDOW_MAX_AGE` (30 days), a `410` or `syncStateNotFound` starts a new round. An item marked `@removed` is skipped. | The window is fixed in the token. A new round also is the backfill: no extra code for it, none against it. |
| **R - Delta content** | The delta returns whole events; `$select` is not supported. The client reads the fields of `get_event` from each item and drops the rest in memory: nothing else is logged, recorded or stored. | Data protection: SkillForge needs no body, attendee or organizer. |
| **S - Subscription** | One per active mailbox, for `SUBSCRIPTION_LIFETIME` (6 days), renewed by a job due `RENEW_AHEAD` (3 days) before it ends. `ensure` lists the app's subscriptions and deletes every one on the mailbox's resource whose `notificationUrl` starts with `GRAPH__NOTIFICATION_BASE_URL` and that the mailbox does not name; one with another URL is reported and left. Whenever `ensure` creates a subscription, it stores `calendar.sync`. | Graph's maximum is 10,080 minutes, and lifecycle notifications do not replace renewing. Another deployment of the same app keeps its subscription. What changed while no subscription existed is found by the sync. |
| **T - Default calendar** | The lesson calendar is the mailbox's default calendar (the maintainer, 2026-11-28; SP-3 confirms it). Every client for a mailbox row first confirms that the default calendar's owner is the mailbox's address. | v1.0 documents the subscription and the delta only for the mailbox. The check is ADR 0010's identity check for a mailbox that is data. |
| **U - Category list** | `ensure` creates each configured category the mailbox's list lacks (compared casefolded), with its colour, and changes no existing one. | A new mailbox needs no setup by hand. |
| **V - Dry run** | With `GRAPH__MODE=dry_run` reads go out and writes are planned: no subscription is created, a `calendar.tag_event` job logs the category it would set, and `calendar.sync` stores no delta state. The preview is those log lines, counted by category. | A preview of the first round before it writes; a wrong category is never corrected by a later run (goal 2). The first `live` round must find everything again. |
| **W - If subscriptions are refused** | If SP-1 shows that Graph refuses a subscription under the Exchange grants, this spec is amended: a due job reads the delta instead. | ADR 0011, point 6. |
| **X - Routes for Graph** | `POST /graph/notifications` and `POST /graph/lifecycle`, outside `/api/v1`, without a token, not in `openapi.json`. A body above 1 MiB or with more than 1000 notifications is refused before the database is touched. | Graph cannot present a SkillForge token, and no generated client calls them. Anyone can call them. |

## Verified behavior

"Docs" means Microsoft Learn, read on 2026-11-28. "Open" is settled by a probe (SP-1 to SP-3).

| Question | Result |
|---|---|
| How long does an event subscription live? | Docs ([subscription](https://learn.microsoft.com/en-us/graph/api/resources/subscription?view=graph-rest-1.0)): at most 10,080 minutes without resource data. A renewal after it expired answers `404`: create a new one ([update](https://learn.microsoft.com/en-us/graph/api/subscription-update?view=graph-rest-1.0)). |
| Do lifecycle notifications replace renewing? | Docs ([lifecycle events](https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events)): no - "Authorization challenges don't replace the need to renew a subscription before it expires." All three events are sent for Outlook events. `lifecycleNotificationUrl` can only be set when the subscription is created. |
| What does Graph expect from the endpoint? | Docs ([webhooks](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)): the validation request is a `POST` with `validationToken` in the query, answered within 10 seconds with `200`, `text/plain` and the decoded token. A notification wants a `2xx` within 3 seconds; a `5xx` is retried for up to 4 hours. An endpoint where more than 10 % of the answers take longer than 3 seconds is delayed; above 15 % past 10 seconds its notifications are dropped. |
| What does a notification carry? | Docs ([changeNotification](https://learn.microsoft.com/en-us/graph/api/resources/changenotification?view=graph-rest-1.0)): `subscriptionId`, `clientState`, `changeType` or `lifecycleEvent`, and `resourceData.id`; several notifications of several subscriptions may share one request. `clientState` is at most 128 characters. |
| May one resource have two subscriptions? | Docs ([create](https://learn.microsoft.com/en-us/graph/api/subscription-post-subscriptions?view=graph-rest-1.0)): "Duplicate subscriptions aren't allowed", a `409`. **Open (SP-1):** whether another `notificationUrl` makes a subscription a different one, and how a listing spells `resource`. |
| Which permission does a subscription need? | Docs (create): a read permission on the resource (`Calendars.Read`); "subscriptions don't support write access permissions when only read access permissions are needed". |
| Does an Exchange "RBAC for Applications" grant alone authorise a subscription? | **Open (SP-1).** Not documented either way. |
| Which roles does the rest need? | Docs ([application RBAC](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac)): `Application Calendars.ReadWrite` to update an event, `Application MailboxSettings.ReadWrite` for the category list. A scope by `MemberOfGroup` counts direct members of a mail-enabled security group only. A change takes 30 minutes to 2 hours to apply. |
| Does a category on the master overwrite the category of an exception? | **Open (SP-2).** Not documented. |
| Does an event update honour `If-Match`? | **Open (SP-2).** If it does, `set_categories` sends the event's `@odata.etag`, and a `412` is a retry that reads first. |
| What does `calendarView/delta` return? | Docs ([event: delta](https://learn.microsoft.com/en-us/graph/api/event-delta?view=graph-rest-1.0)): single instances, occurrences and exceptions with all their properties, and removed items as `@removed`; `$select` is not supported; the tokens encode the window. An expired token is a 40x such as `syncStateNotFound`, or a `410` ([delta query](https://learn.microsoft.com/en-us/graph/delta-query-overview)). |
| Is the lesson calendar the default calendar? | The maintainer: yes, always. **Open (SP-3)** as a check. |
| Which colours do the existing categories have? | **Open (SP-3).** The defaults follow the maintainer's description. |
| What do many notifications cost? | Docs ([throttling](https://learn.microsoft.com/en-us/graph/throttling-limits)): 10,000 requests per 10 minutes and four concurrent requests per app and mailbox. |

## Rules

A rule has a `title` (a regular expression, searched case-insensitively), the optional conditions `teams` and
`phone`, and a `category`. The first rule that matches decides; no match leaves the event alone. `phone` asks
whether the event's location is a phone number, by billing's definition (`is_phone_number` in `calendar_rules.py`):
stripped, an optional leading `+`, then only digits, spaces and `/ ( ) -`, with 5 to 15 digits.

```json
{
  "categories": [
    {"name": "Discord", "color": "preset4"},
    {"name": "Teams", "color": "preset8"},
    {"name": "Telefon", "color": "preset7"},
    {"name": "Abgesagt", "color": "preset12", "manual": true}
  ],
  "rules": [
    {"title": "^kennenlernen mit ", "category": "Telefon"},
    {"title": "^.+ mit .+$", "phone": true, "category": "Telefon"},
    {"title": "^.+ mit .+$", "teams": true, "category": "Teams"},
    {"title": "^.+ mit .+$", "teams": false, "category": "Discord"}
  ]
}
```

What a `calendar.tag_event` job does, in this order:

| The event | Result |
|---|---|
| Is gone (`404`) | Nothing. |
| Is an `occurrence` or an `exception` | Nothing. |
| Has at least one category | Nothing. |
| Matches no rule | Nothing. |
| Matches a rule | Its category is set, as the event's only one. |

- **The first meeting comes first.** `Kennenlernen mit <name>` also matches the lesson pattern, so its rule stands
  before it. The order is the configuration's; a test pins the default.
- **The rules are the maintainer's, not billing's.** Billing reads a first meeting from any subject that contains
  `kennenlern` (`calendar_rules.py`); the rule here matches only a title that starts with `Kennenlernen mit`, so
  `Erstes Kennenlernen mit <name>` gets a lesson rule. A first meeting gets `Telefon` even as a Teams meeting.
- **A phone number as location** makes a lesson title `Telefon`, before the Teams rule. Billing reads such an
  event as a phone appointment already, so the category changes nothing there. The definition is written a second
  time in the pure `rules.py` - the calendar domain imports no other - and a test ties the two together.
- **Billing reads the same categories.** The import of [`billing.md`](billing.md) treats `Telefon` as "no lesson"
  before it looks at the title. The default rules set `Telefon` only on a first meeting, which is no lesson
  either; its stored reason changes from `first_meeting` to `phone`, and a first meeting that was assigned a
  lesson by hand is flagged once at the next import. A rule that set `Telefon` on a lesson would take that lesson
  out of billing: the comment on `CALENDAR__RULES` in `.env.example` says so.
- **The job's own write comes back** as an `updated` notification. Its job reads an event with a category and
  stops.

## Design

```
POST /graph/notifications --> calendar.tag_event(mailbox, event) --> read event, apply rules, set category
POST /graph/lifecycle     --> calendar.renew_subscription | calendar.ensure_mailbox | calendar.sync
worker start, mailbox routes --> calendar.ensure_mailbox --> categories, subscription, renewal job, sync
calendar.sync             --> read the delta, store one calendar.tag_event per candidate, keep the deltaLink
```

| Kind | `dedupe_key` | Stored by | Does |
|---|---|---|---|
| `calendar.tag_event` | mailbox id and event id | the notification route, `calendar.sync` | The table in "Rules". |
| `calendar.ensure_mailbox` | mailbox id | the worker at its start for every mailbox, the mailbox routes, `subscriptionRemoved`, a renewal answered `404` | Active: category list, subscription created or renewed, the renewal job, and `calendar.sync` if it created the subscription, none ran yet or the window is old. Inactive: subscription deleted, its state and the delta state cleared. |
| `calendar.renew_subscription` | mailbox id | `calendar.ensure_mailbox` (due later), `reauthorizationRequired` | Renews the subscription and stores the next renewal job; a `404` stores `calendar.ensure_mailbox` instead. Does nothing while `subscription_expires_at` lies more than `SUBSCRIPTION_LIFETIME` minus one hour ahead. |
| `calendar.sync` | mailbox id | `calendar.ensure_mailbox`, `missed`, `subscriptionRemoved`, `POST .../sync` | Reads the delta. A single instance without a category whose title matches a rule, and the master of every occurrence and exception, become `calendar.tag_event` jobs. |

**Layers.** `app/services/jobs/` knows no domain: it imports only `app.core`, and a kind is a string to it.
`app/services/calendar/` imports `app.core`, `app.integrations` and `app/services/jobs/`, no other domain; its kinds
are constants of `app/services/calendar/jobs.py`. `app/workers/jobs.py` holds `HANDLERS`, the map from a kind to
its handler, as `PASSES` does in `housekeeping.py`.

**Tables.**

| Table | Columns |
|---|---|
| `system.job` | `id`, `kind`, `payload` (JSONB), `status`, `attempts`, `max_attempts`, `available_at`, `locked_until`, `dedupe_key`, `last_error`, `finished_at`, `created_at`, `updated_at` |
| `calendar.mailbox` | `id`, `address` (unique, casefolded), `active`, `rules` (JSONB), `client_state_digest`, `subscription_id` (unique), `subscription_expires_at`, `delta_link`, `delta_window_start`, `synced_at`, `created_at`, `updated_at` |

**Routes.**

| Route | Guard | Answers |
|---|---|---|
| `POST /graph/notifications`, `POST /graph/lifecycle` | `clientState` | `200` with the token for a validation request; `202` once the jobs are stored |
| `GET /api/v1/calendar/mailboxes` | `calendar:manage` | `Page[Mailbox]` |
| `POST /api/v1/calendar/mailboxes` | `calendar:manage` | `201`; `mailbox_already_exists` (`409`) |
| `PATCH /api/v1/calendar/mailboxes/{mailbox_id}` | `calendar:manage` | `active` and `rules`; `mailbox_not_found` (`404`) |
| `POST /api/v1/calendar/mailboxes/{mailbox_id}/sync` | `calendar:manage` | `202`; `mailbox_not_found` (`404`) |

The error catalog of the arc is closed at `mailbox_not_found` and `mailbox_already_exists`. A `Mailbox` shows `id`,
`address`, `active`, `rules`, `subscription_expires_at`, `synced_at`, `created_at` and `updated_at` - never
`client_state_digest`, `subscription_id` or `delta_link`.

**Settings.**

| Setting | Meaning |
|---|---|
| `GRAPH__NOTIFICATION_BASE_URL` | The public https origin of this SkillForge, without a path; the two notification URLs are built from it. Unset: no subscription is created. |
| `CALENDAR__CATEGORIES`, `CALENDAR__RULES` | The JSON above; both have defaults. |
| `ALERTS__DISCORD_WEBHOOK_URL` | A secret; only a `discord.com` webhook URL is accepted. Unset: a failed job is only logged. |

**Logs.** A line names a job by its id and kind and a mailbox by its id. No line, error, `repr` or `last_error`
holds a title, a location, an attendee, an address, an event id, a `clientState`, a delta link or the alert URL;
`last_error` is an error class and a status.

## Success metrics

- A booked lesson shows its category in Outlook within a minute.
- Moving twenty events in a row stores at most one pending job per event and ends without a failed job.
- A week without a deploy ends with a subscription that is still alive.
- No category set by hand is ever replaced: no report of it, and the tests of N hold.

## Requirements

### Must-have (P0)

Standing criteria, for every slice:

- [ ] `just check-all` is green.
- [ ] No test, script run by an agent or CI job reaches Microsoft Graph or Discord; fixtures are synthetic.
- [ ] No log line, error message, `repr` or `last_error` holds what "Logs" excludes; one test per slice that logs.
- [ ] `app/services/jobs/` imports only `app.core`; `app/services/calendar/` imports no other domain (an
      architecture test, added with P0-1 and P0-5).
- [ ] The slice updates the docs it makes wrong - `ARCHITECTURE.md`, `DATABASE_SCHEMA.md`, `AGENTS.md`,
      `app/integrations/AGENTS.md`, `.env.example` - in the same PR.

**P0-1 - Job queue.**
- _Technique:_ `Job` and `JobStatus` in `app/core/db/models/system/job.py`; revision `0020_system_job` with the
  partial indexes `ix_job_claimable` on `available_at` (`status = 'pending'`) and `ix_job_lease` on `locked_until`
  (`status = 'running'`) and the partial unique index `uq_job_pending_dedupe` on (`kind`, `dedupe_key`). `enqueue` and
  `enqueue_many` (with `delay` and, for a debounced kind, `max_delay`), `claim`, `succeed`, `fail` and `defer` in
  `app/services/jobs/queue.py`; `claim` takes one due `pending` job or one `running` job whose `locked_until` passed,
  in one statement.
- _Tests that change:_ none.
- _Acceptance criteria:_
  - [x] Two concurrent claims never return the same job (`tests/db/jobs/`).
  - [x] Fifty `enqueue` calls with one key store one job; a call while that job is `running` stores one more.
  - [x] Decision D: each further call moves the due time, never past `max_delay` after the first.
  - [x] `fail` follows decision E: the delays, the cap, a longer `Retry-After`, and `failed` after the last attempt.
  - [x] Decision C: a job that fails beside a pending twin is deleted, and the twin has the greater `attempts`.
  - [x] Decision B: a `running` job is claimed again only after its lease, that claim counts as an attempt, a job
        without an attempt left is finished as `failed`, and `succeed` by a stale holder changes nothing.
  - [x] A debounced or failed job never becomes due earlier through a further call or through a twin it merges
        into.
  - [x] The downgrade drops the table and `system.job_status`.

**P0-2 - Jobs worker.**
- _Technique:_ `app/workers/jobs.py`: `HANDLERS`, `run_once`, `run_forever`, a beat task of its own,
  `WorkerName.JOBS`. The compose service `jobs`, the recipe `just worker-jobs` - in `AGENTS.md` beside
  `just worker-housekeeping`, among the recipes that need the maintainer's confirmation - and the pass
  `jobs_deleted` (`delete_finished_jobs` in `app/services/jobs/housekeeping.py`) in `PASSES`. `just openapi`:
  `WorkerName` is part of the contract.
- _Tests that change:_ `tests/workers/test_worker_wiring.py` (the `jobs` service),
  `tests/workers/test_housekeeping_cycle.py` (the third counter), `tests/api/test_health.py` (the second worker).
- _Acceptance criteria:_
  - [ ] A handler that raises leaves its job `pending` with the next due time and `/health` ok.
  - [ ] A handler that runs longer than the beat stays fresh leaves `/health` ok.
  - [ ] Decision G: a job of an unknown kind keeps its `attempts` and is due again after `RETRY_CAP`.
  - [ ] A job stored while the worker is stopped is worked after its start.
  - [ ] `/health` is unhealthy without a fresh beat of `jobs`.
  - [ ] The pass deletes by decision H and nothing else.
  - [ ] `just test-clients` is green.

**P0-3 - Failure report.**
- _Technique:_ `app/integrations/alerts/` with `AlertSettings` (`ALERTS__*`, the URL a `SecretStr`) nested in
  `Settings`, and `post_alert`, which always sends (`IntegrationMode.LIVE`); `IntegrationHttp.write` takes a
  `template`; `ensure_live_allowed` allows `live` on an `httpx.MockTransport` under pytest (ADR 0011, point 8). The
  worker calls `post_alert` when a job becomes `failed`.
- _Tests that change:_ the tests of `ensure_live_allowed` in `tests/integrations/test_integration_transport.py`, the
  two refusals of `live` in `tests/integrations/graph/test_graph_token.py`, and `CORE_TO_INTEGRATIONS` in
  `tests/integrations/test_integrations_architecture.py`.
- _Acceptance criteria:_
  - [ ] The post goes to `discord.com` only; a URL on another host does not load, and its error repeats no input.
  - [ ] No log line, error or `repr` holds the URL or a segment of its path; the log line names a template.
  - [ ] A failing post is logged and changes nothing about the job.
  - [ ] Under pytest, `live` on a real transport is refused as before.

**P0-4 - Rules.**
- _Technique:_ `app/services/calendar/rules.py`, pure like `calendar_rules.py`: `classify(event, rules)`.
  `CalendarSettings` (`CALENDAR__*`) in `app/core/config.py` validates the JSON of "Rules".
- _Tests that change:_ none.
- _Acceptance criteria:_
  - [ ] One test per row of the table in "Rules", on synthetic titles, the order of the first meeting included.
  - [ ] Decision O: either sign of a Teams meeting is enough.
  - [ ] The `phone` condition agrees with billing's `is_phone_number` on a shared list of locations, and a phone
        number comes before a Teams meeting.
  - [ ] A configuration whose rule names a `manual` or an unknown category, or whose `title` does not compile or
        is too long, does not load.
  - [ ] The module imports no session, no network and no clock (an architecture test).

**P0-5 - Mailboxes.**
- _Technique:_ the model package `app/core/db/models/calendar/` with `Mailbox`; revision `0021_calendar_mailbox`;
  `app/services/calendar/mailboxes.py`, `errors.py` and `jobs.py` (the kinds); `app/api/v1/calendar/` with its
  entry in `OPENAPI_TAGS`; `Scope.CALENDAR_MANAGE` in `ROLE_SCOPES[Role.ADMIN]`; `just openapi`. Every write stores
  `calendar.ensure_mailbox` in its transaction.
- _Tests that change:_ `tests/auth/test_scopes.py` and `tests/auth/test_roles.py` (the inventories),
  `tests/db/models/test_schemata.py`, `_APPLICATION_SCHEMAS` in `tests/db/test_migration_apply.py`, and the area,
  tag and `PAGED_ENDPOINTS` pins of `tests/api/test_openapi_contract.py`.
- _Acceptance criteria:_
  - [ ] Two spellings of one address are `mailbox_already_exists`, racing requests included.
  - [ ] A `rules` value that `CalendarSettings` would refuse is a `422`.
  - [ ] A token from the Discord exchange never carries `calendar:manage`.
  - [ ] Each route is pinned to its errors (`tests/api/test_calendar_error_contract.py`).
  - [ ] `just test-clients` is green.

**P0-6 - Notification routes.**
- _Technique:_ `app/api/graph/notifications.py`, mounted by `app/api/__init__.py` with `include_in_schema=False`
  and a tag (`operation_id` needs one); `accept_notifications` and `accept_lifecycle` in
  `app/services/calendar/notifications.py` read every named subscription in one query and store all jobs of one
  request in one statement.
- _Tests that change:_ none.
- _Acceptance criteria:_
  - [ ] A validation request is answered with `200`, `text/plain`, `X-Content-Type-Options: nosniff` and the
        decoded token; a token above 4096 characters or with a control character is refused.
  - [ ] A notification with an unknown `subscriptionId`, a wrong `clientState` or an inactive mailbox stores
        nothing and is answered `202`.
  - [ ] Fifty notifications for one event, in one request or in fifty, store one job; one that arrives while that
        job runs stores exactly one more.
  - [ ] Decision X: a body or a batch above the bound is refused and reads no row.
  - [ ] Each lifecycle event stores the jobs of the table in "Design".
  - [ ] A failing insert answers `5xx`.
  - [ ] `openapi.json` does not change.

**SP-1, SP-2, SP-3 - Probes.** _Scripts by agents, run by the maintainer, after P0-6 is deployed._
- _Technique:_ `scripts/graph/probe_notifications.py` in the shape of `record_calendar.py` (inline dependencies,
  its own env file, output safe to paste). `exchange-rbac-calendar.ps1` gains `-Group`: the scope
  `skillforge-lesson-mailboxes` with the filter `MemberOfGroup -eq '<distinguished name>'` and one role assignment
  per role of ADR 0011, point 5. The maintainer creates the mail-enabled security group.
- _Acceptance criteria:_
  - [ ] The script reuses or creates each object as it does today, checks all three roles with
        `Test-ServicePrincipalAuthorization` for a member and for an outsider, and `-Remove -Group` takes away
        exactly what `-Group` created. The single-mailbox setup stays until the maintainer removes it.
  - [ ] SP-1: the probe creates a subscription for the lesson mailbox, prints `created` or the error code, the
        answer to a second create with another URL and how the listing spells `resource`, and deletes what it
        created; for a mailbox outside the scope it expects a refusal.
  - [ ] SP-2: on a synthetic test series it gives one occurrence the category `Abgesagt`, then the master
        `Discord`, and prints whether the exception kept its category, whether `$expand=exceptionOccurrences`
        answered and whether a stale `If-Match` was refused; it removes the series.
  - [ ] SP-3: it prints the four category names with their colour presets, and whether the calendar named
        `GRAPH__CALENDAR_NAME` is the default calendar.
  - [ ] The results stand in "Verified behavior", and decisions P, S, T and W are confirmed or amended.

**P0-7 - Calendar client: events.**
- _Technique:_ `GraphClient.connect(settings, *, mailbox=None)`: without `mailbox` it confirms the named calendar
  as today, so billing is untouched; with it, it confirms decision T (`default_calendar_owner`).
  `IntegrationHttp.write` takes `PATCH`; `get_event` (with `$select` and, for a master, `$expand`) and
  `set_categories` in `app/integrations/graph/`.
- _Tests that change:_ none; a constructor that must change in `tests/integrations/graph/` or
  `tests/db/billing/calendar_support.py` is named in the PR.
- _Acceptance criteria:_
  - [ ] `get_event` asks only for `id`, `type`, `subject`, `categories`, `isOnlineMeeting`, `onlineMeetingProvider`,
        `location` and `seriesMasterId`; a `404` is "gone", not an error.
  - [ ] A default calendar owned by another address ends as `IdentityCheckError` before any other call.
  - [ ] In `dry_run`, `set_categories` returns the planned request and sends nothing.
  - [ ] A `429` on a write is raised with its `Retry-After` and not repeated by the client.

**P0-8 - Calendar client: subscriptions, categories, delta.**
- _Technique:_ `GraphSettings.notification_base_url`; `list_subscriptions`, `create_subscription`,
  `renew_subscription`, `delete_subscription`, `master_categories`, `create_category` and `calendar_delta` (follows
  `@odata.nextLink`, returns the `@odata.deltaLink`, bounded by `MAX_PAGES`).
- _Tests that change:_ the `GRAPH__` inventory in `tests/integrations/graph/test_graph_settings.py`.
- _Acceptance criteria:_
  - [ ] Decision R: `calendar_delta` returns only the fields `get_event` asks for, whatever the response holds, and
        skips an item marked `@removed`.
  - [ ] A `410` and a `syncStateNotFound` are one error the caller can tell from every other.
  - [ ] A delta that never ends is refused after `MAX_PAGES`.
  - [ ] A base URL with a path, or without https, does not load.

**P0-9 - `calendar.tag_event`.**
- _Technique:_ `tag_event` in `app/services/calendar/tagging.py`, registered in `HANDLERS`.
- _Tests that change:_ none.
- _Acceptance criteria:_
  - [ ] An event with a category, an occurrence, an exception and a vanished event cause no write.
  - [ ] The job for the echo of its own write causes no write.
  - [ ] Decision P: a master is written, and an exception whose category differs afterwards is reported.
  - [ ] A write whose outcome is unknown is retried, and the retry reads first.
  - [ ] In `dry_run` the job logs the category and succeeds.
  - [ ] Mutation probe: without the check of N, the test of the first criterion fails.

**P0-10 - `calendar.ensure_mailbox` and `calendar.renew_subscription`.**
- _Technique:_ `app/services/calendar/subscriptions.py`; the worker stores `calendar.ensure_mailbox` for every
  mailbox at its start.
- _Tests that change:_ none.
- _Acceptance criteria:_
  - [ ] Two runs for one mailbox leave one subscription; decision S decides what is deleted and what is left.
  - [ ] Creating a subscription stores `calendar.sync`; renewing one does not.
  - [ ] A missing category is created, an existing one is neither changed nor created again.
  - [ ] A renewal answered `404` stores `calendar.ensure_mailbox`.
  - [ ] An inactive mailbox ends without a subscription and without delta state.
  - [ ] In `dry_run` no subscription is created and the job succeeds.

**P0-11 - `calendar.sync`.**
- _Technique:_ `sync` in `app/services/calendar/sync.py`.
- _Tests that change:_ none.
- _Acceptance criteria:_
  - [ ] A first round stores one job per matching single instance without a category and one per series.
  - [ ] A second round starts from the stored `delta_link`; decision Q's three reasons start a new one.
  - [ ] A failing round keeps the previous `delta_link`.
  - [ ] Decision V: after a round in `dry_run`, the next round in `live` is a new round.

**P0-12 - Closing pass.**
- _Acceptance criteria:_
  - [ ] `README.md` describes the two workers, and every doc named in the standing criteria matches the code.
  - [ ] This spec's status is `Implemented`, with the date.

### Nice-to-have (P1)

- **P1-1 - Wake the worker with `LISTEN`/`NOTIFY`** instead of the poll.

### Future considerations (P2)

- The billing import reads the mailboxes of `calendar.mailbox` instead of `GRAPH__MAILBOX`.
- Further kinds, each with its own decision: a sevDesk sync, timesheet PDFs.

## Decided (formerly open questions)

Decided by the maintainer on 2026-11-28:

- **A phone number as location** makes a lesson title `Telefon`; the `phone` condition is part of the rules from
  the start.
- **The delta stays,** although it returns whole events that the client drops (decision R); `calendarView` with
  `$select` was the alternative.
- **The first-meeting rule matches only a title that starts with `Kennenlernen mit`.** Such an event is a phone
  appointment in practice.

## Open questions

1. SP-1: does Graph create a subscription under the Exchange grants alone? Otherwise decision W.
2. SP-2: does an exception keep its category? Otherwise decision P is reopened before `GRAPH__MODE=live`.
3. SP-3: the colour presets of the existing categories, and the default calendar.

## Timeline / phasing

- **Wave 1:** P0-1 to P0-6, then the probe scripts - one stack, in this order. `compose.yml`,
  `app/core/config.py`, `.env.example` and `openapi.json` are shared: each slice rebases, `openapi.json` is
  regenerated.
- **Probes:** the maintainer releases wave 1, makes `/graph/` reachable for Microsoft Graph, creates the group, runs
  the RBAC script and the three probes.
- **Wave 2:** P0-7 to P0-12, one stack.
- **Going live:** the maintainer sets `GRAPH__NOTIFICATION_BASE_URL` and `ALERTS__DISCORD_WEBHOOK_URL`, adds the
  first mailbox with `GRAPH__MODE=dry_run`, reads the preview, then sets `live`.

## Rules for implementing agents

- Never call Microsoft Graph or Discord: tests use `httpx.MockTransport`, and the probes are the maintainer's.
- A requirement that seems to need another error class, scope, job kind or setting: stop and report.
- A slice that changes `app/services/billing/` or what the billing import does: stop and report.
- Review level: `deep` for P0-1, P0-3, P0-5, P0-6, P0-7, P0-9, P0-10, P0-11 and the RBAC script; `evidence` for
  the others.
