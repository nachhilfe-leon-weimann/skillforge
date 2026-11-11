# Spec: Billing (lessons, rates, invoice drafts in sevDesk, prepaid credit)

> Status: Draft - SF0, SF1-1 to SF1-3c, SF2-1 to SF2-3, SF3-1, SF3-2, SF4-1 to SF4-3 done (SF2-3's charge box waits for SF5-3); SP-CAL done (Q9 and Q16 answered). SP-SD freezes this spec after both spikes; until
> then nothing is built on a point marked **open**.
> Builds on the [project sketch](../PROJECT.md), [ADR 0010](../decisions/0010-outbound-integrations.md) (outbound
> integrations), [ADR 0007](../decisions/0007-crm-system-of-record.md) (the CRM is the system of record),
> [`api-conventions.md`](api-conventions.md), [`crm-api.md`](crm-api.md) and
> [`user-authentication.md`](user-authentication.md).
> The screens are skillsite's arc (its admin-helper spec); this spec holds no UI.
> Written to be executed by coding agents: every requirement names its symbols, files and checkable criteria.

## Problem statement

The maintainer bills lessons by hand. An appointment sits in the Outlook calendar, its hours are typed into a time
tracker (Clockodo), the tracker hands the positions to sevDesk, and for a PayPal customer the foot text is swapped
by hand. Families that pay in advance are a project with a budget per cycle in the tracker.

SkillForge cannot help today:

- **It knows no lesson.** There is no lesson, no rate, no invoice and no credit in its data.
- **It calls nobody.** `ext.sevdesk_contact` has no writer, and no route reads a calendar or writes an invoice.
- **Nothing guards a mistake.** sevDesk has no sandbox and no documented call that deletes a draft, and an issued
  invoice can only be cancelled. Code that bills must be safe to write and to test before it is right.

## Goals

1. **Lessons are central data.** SkillForge knows which lessons took place, for whom and how long.
2. **The calendar proposes, a person confirms.** Lessons are imported from the lesson calendar; only a lesson the
   maintainer ticked is billed.
3. **One rate per student**, with a valid-from date, frozen on every position.
4. **Invoice drafts that look like today's invoices**, created in sevDesk from a preview the maintainer confirmed.
5. **A lesson is charged at most once** - guaranteed by the database, on whatever path.
6. **Prepaid credit as an account statement** per payer, instead of a project per cycle.
7. **Safe to build.** No test, no CI job and no agent session can reach a real account.
8. **The time tracker can go** after one month in parallel.

## Non-goals

- **The screens.** Lists, previews and dialogs are skillsite's admin helper. A check of PR titles and bodies is
  repository tooling: it is not part of this spec, and no slice waits for it.
- **Sending, cancelling, dunning.** v1 creates drafts. The maintainer sends, cancels (Storno) and reminds in
  sevDesk, and picks the mail template there. SkillForge never calls sevDesk's send, enshrine, cancel or booking
  endpoints and sets no mail text.
- **Detecting payments.** Paid and partly paid show as a badge after a refresh; nothing is booked.
- **Billing an authority (BuT).** By hand in v1 (decision V); no e-invoice.
- **A tutor per lesson.** Only the maintainer teaches.
- **Special pricing:** rates per subject, holiday or course rules, packages, discounts, trial lessons, a shared
  lesson for two children. A special case is a lesson's own rate or a lesson added by hand.
- **Split payment**, and prepaid and postpaid children under one payer.
- **What families see of their credit.** The statement exists for the maintainer; a family's view comes with the
  student portal.
- **Import from or write-back to the time tracker, and writing to the calendar.**
- **A job queue, a background sync, webhooks** (ADR 0010).
- **Hosting the admin screens.** In v1 they run on the maintainer's machine against the production SkillForge, over
  a connection the maintainer sets up. Agents never work against production.
- **The move of the production database** off Neon. It happens outside this arc and before the first real invoice.

## Decided defaults

Rows A to Z are decided - by the maintainer, or as fixed guardrails of the design. No implementing agent reopens
them.

| Topic | Decision | Rationale |
|---|---|---|
| **A - Home** | Billing lives in SkillForge: services in `app/services/billing/`, routes in `app/api/v1/billing/` (tag `billing`), models in `app/core/db/models/billing/` (schema `billing`), third-party calls in `app/integrations/`. The portal is UI and backend-for-frontend only. | Both frontends would need the same rules (sketch); ADR 0010. |
| **B - Drafts only** | SkillForge creates invoice drafts. Sending, Storno, dunning and the mail stay in sevDesk, with the maintainer. | A draft reaches no family; what cannot be undone stays a human step. |
| **C - One invoice per lesson** | By default every lesson gets its own invoice. Several open lessons of one payer can be combined into one invoice by selection. Nothing is preselected by calendar month. | Today's practice. |
| **D - Invoices look as today** | Positions, head text and foot text follow today's invoices ([Family-visible texts](#family-visible-texts)). From its first draft on SkillForge sets head and foot text itself; the text templates in sevDesk are no longer picked by hand. | Families see no change, and the API cannot read the templates. |
| **E - Rate** | The default rate is 30 EUR per 60 minutes, from configuration. A student can have an own rate with a valid-from date: one rate per student, not per subject. 45 and 90 minutes are priced linearly. Resolution: the lesson's own rate, else the student's rate valid on the lesson's day, else the default. | The rate belongs to the student, so siblings with different prices can share a payer; valid-from keeps old lessons at their old price. |
| **F - Frozen rate** | The rate is frozen on each position when the preview is built. A later rate change touches no existing charge. | What the maintainer confirmed is what is billed. |
| **G - Quarter-hour rule** | A rate in cents per hour is divisible by 4 and a lesson's minutes by 15. Every amount is then a whole cent. | No rounding code exists, so SkillForge and sevDesk agree to the cent by construction. No rate in use today breaks the rule. |
| **H - Duration** | A lesson is billed with its planned calendar duration, correctable in 15-minute steps. One position per lesson, quantity = minutes / 60, computed per position and never across a sum. | The appointment is what was agreed, and the correction covers a lesson that ran differently. Arithmetic per position keeps rule G exact; a sum of rounded parts would not be. |
| **I - Payer** | The payer is the party with a `pays_for` relation to the student, otherwise the student. A student has exactly one payer at a time. When the payer changes, the new one applies from a date and older lessons stay with the old one ([PD1](#approved-by-the-maintainer)). An authority that pays is a company party with `pays_for`. | The CRM already holds who pays (ADR 0007). |
| **J - Recipient** | The invoice goes to the payer's sevDesk contact (`ext.sevdesk_contact`). Without that link there is no draft. | sevDesk addresses an invoice to a contact, and the `ext` link is the one place a party meets its contact - nothing is looked up by name. |
| **K - Payment method** | Read from the custom field "Zahlungsverfahren" of the sevDesk contact, with the values `PayPal` and `Überweisung`: read for the preview and frozen there. An empty or unknown value blocks. One method per payer; paying in advance is not a payment method. The maintainer switches the field to these two values before the cut-over. | sevDesk stays the home of how a customer pays; the PayPal link appears only for `PayPal`. |
| **L - Dates and tax** | Invoice date: the day the draft is created. Delivery date: the lesson's day, or the range from the first to the last lesson. A lesson that has not ended is never billed. Payment term: 14 days for everyone. § 19 UStG: `taxRule` 11. | A normal sevDesk invoice refuses a delivery date after its invoice date. |
| **M - Cancellations** | A late cancellation or a no-show is never billable by itself. It shows as cancelled, and the maintainer makes it billable per lesson with one click; it is then the same position as any lesson. A lesson the maintainer cancelled costs nothing, and its replacement is a normal lesson. | Each case is the maintainer's own decision. |
| **N - Calendar** | Lessons come from one Microsoft 365 calendar, read through Microsoft Graph without a signed-in user. Titles follow `<Fach> mit <Vorname Nachname>`. Lessons are online (Discord or Teams), cost the same and all count. A phone appointment and the free first meeting (`Kennenlernen mit <Name>`) are never proposed as a lesson but can be added by hand. A title without ` mit ` shows as unassigned; there is no ignore list. An alias table maps calendar names to students. | The lesson calendar holds lessons and first meetings only. Names can carry typos or belong to a student not yet in the CRM. |
| **O - One charge per lesson** | One table, `billing.lesson_charge`, holds invoice positions and prepaid debits alike. A unique index allows one active charge per lesson. "Billed" is derived from it and never stored. | The database rules out a lesson charged twice, not service discipline. |
| **P - Corrections** | A correction voids a charge and books a new one. An issued invoice never changes: a Storno happens in sevDesk and is recorded in SkillForge with one click, which reopens its lessons. A lesson with charge history is never hard-deleted. The calendar import never changes a ticked or charged lesson; it flags the difference. | Every charge keeps its trail. |
| **Q - Outbound writes** | Pending row, commit, call, outcome. A write is never retried. An unknown outcome is a state that a reconcile resolves. A sevDesk read error never changes money. | ADR 0010. |
| **R - Number and reference** | The invoice number is sevDesk's. SkillForge writes its own reference into `customerInternalNote`, which the maintainer does not use. Fallback, only if placeholders do not resolve in a text set through the API ([Q2](#for-the-spikes)): the number is taken from sevDesk's own sequence when the draft is created. | The PayPal link needs the number; a number is never invented. |
| **S - Credit account** | One credit account per payer, for every child the payer pays for, shown as a statement. Credit is money, not hours. A prepaid lesson is charged to the account when it is ticked and never appears on a normal invoice. Ticking is never blocked: the balance may go negative, and the minus carries into the next top-up. No balance is stored. An account closes only at balance 0. | Replaces a project and a budget per cycle. |
| **T - Top-up** | A top-up is a normal invoice with positions "N x subject" at the child's rate, without a date. Credit counts once the invoice is issued (sevDesk status >= 200), with the amount sevDesk reports; payment is only a badge. The amount per family defaults to 100 EUR and is free on every top-up, small amounts included. The helper hints when the remaining credit is one hour or less at the highest rate valid today among the account's children ([M11](#for-the-maintainer)), and the maintainer triggers the top-up. A prepaid family that wants single lessons for a while stays prepaid and gets a small top-up. | Today's practice. A draft is no invoice, so it is no credit. _Amended 2026-11 by M11: the hint follows the children's rate instead of a fixed 30 EUR._ |
| **U - No opening balance** | A prepaid family moves to SkillForge when its running cycle in the time tracker is used up: it starts at balance 0 with a new top-up. Nothing is computed from the tracker. | No figure has to be verified per family. |
| **V - Authorities** | Lessons an authority pays for (BuT) are billed by hand in v1: they show as "bill manually" and get no draft. | Rare, and an authority may ask for an e-invoice ([L2](#recorded-not-resolved)). |
| **W - What families see** | Nothing new in v1. The maintainer reads a balance from the statement when a family asks. | The admin helper comes first. |
| **X - Scopes** | `billing:read` and `billing:write` on `Scope`; the `admin` role holds both. | One permission system (sketch, principle 5). |
| **Y - Testing** | Every write test against sevDesk runs on a synthetic trial account, through the two trial recipes only - `just contract-sevdesk` (SF1-3a) and `just sevdesk-spike` (SF1-3b) - and an agent runs either only after the maintainer's go in the session. The real account never gets a test object. Checks of the admin screens run only against a local SkillForge with `just seed-demo` data (SF2-3). `httpx` becomes a direct runtime dependency with SF1-1 (it is already installed through `fastapi[standard]`); any other new runtime dependency is asked first. | ADR 0010; a draft cannot be deleted through the documented API. |
| **Z - Cut-over** | The time tracker ends in a hard cut-over on the first day of a month, after one month in parallel and a full export. No import, no write-back. | One month side by side shows every difference while the old way still works; after it, a lesson has one system of record. The export keeps the old records. |

### Proposed with SF0

The maintainer's decisions did not settle the points below. Each is a default with its reason.

#### Approved by the maintainer

Money logic, data protection and a change to an ADR are asked first ([`AGENTS.md`](../../AGENTS.md)). The
maintainer approved every row below in the review of SF0 (2026-11); the last column records the answer.

| Topic | Proposal | Rationale | Maintainer's answer |
|---|---|---|---|
| **PD1 - Payer on the lesson** | The payer is resolved when a lesson becomes billable - it is ticked, or a late cancellation is marked billable - and stored on it (`Lesson.payer_party_id`). Becoming billable again, or changing the student or the day, resolves afresh. When the stored payer differs from what the CRM resolves now, the preview shows the warning `payer_changed` and blocks nothing. The maintainer then chooses per lesson: keep - creating the draft from that preview, whose fingerprint covers the stored payer - or resolve afresh (`resolve-payer`). A billable lesson without a stored payer blocks (`payer_missing`). | Decision I wants the new payer "from a date", and `core.party_relation` carries no dates. Freezing when a lesson becomes billable keeps older lessons with the old payer; the warning makes every remaining case a visible choice. | Approved: the warning and the choice per lesson are all "from a date" needs. |
| **PD3 - A lesson's own rate** | A lesson can carry its own rate in cents per hour, 0 included, under the same rule G. | One invariant for every amount. An own _total_ would not fit a quantity in hours. | Approved; no flat price per lesson is needed. |
| **PD6 - Upstream errors** | A fourth category joins the API's error taxonomy: `UpstreamError`, mapped to 502. It is decided in [ADR 0010](../decisions/0010-outbound-integrations.md) (point 10), which amends [ADR 0006](../decisions/0006-error-envelope.md) once it is accepted. | Endpoints stay free of `try/except`; 502 says "not your request, not our bug". | Approved, as point 10 of ADR 0010, which stays `Proposed` until SP-SD. |
| **PD9 - Party references** | Billing tables reference `core.party(id)` with `ON DELETE RESTRICT`, never `core.student`, and join `EXTERNAL_LINKS`: a party with billing rows cannot be deleted. This amends decision K of [`crm-api.md`](crm-api.md), which guards a delete by `ext` links only. | Taking a student role away must not fail on a lesson, and a charge must not lose its party. | Approved, together with the retention answer of [M10](#for-the-maintainer). |
| **PD12 - Credit entries** | `billing.prepaid_entry` knows two kinds: `topup`, always from a top-up invoice SkillForge created, and `adjustment`, a manual correction with a mandatory note. A top-up invoice written by hand in sevDesk is entered as an adjustment that names it in the note. | A correction needs a trail. With no opening balance (decision U) nothing else has to be registered from outside. Refunds are v1.1. | Approved in this form: no top-up "registered by number" in v1. |
| **PD13 - Calendar data kept** | `calendar_event.title_name` is kept only while an occurrence waits for an assignment. It is never stored for all-day, phone and first-meeting occurrences and is cleared when the occurrence is assigned. Unassigned and dismissed occurrences are deleted 90 days after their start, with the next import. | A calendar title holds a name, sometimes of a person who never becomes a student. Keep what the next step needs, and no longer. | Approved with 90 days. |

#### Defaults that stand

They stand until the maintainer changes them in review; the freeze (SP-SD) turns them into decisions or replaces
them.

| Topic | Proposal | Rationale |
|---|---|---|
| **PD2 - Preview and create** | The preview is pure and stores nothing. It returns the planned invoice and a fingerprint; creating takes the same selection and that fingerprint, rebuilds, and refuses if they differ. | "Frozen at the preview" without a second write path: the maintainer gets exactly what was shown, or a fresh preview. |
| **PD4 - Subject and part** | A lesson's subject is the label from the calendar title (`Mathe`). Configuration maps labels to sevDesk part ids (`SEVDESK__PART_IDS`); a label without a part blocks the preview. No foreign key to `core.subject`. | Parts are account data and so configuration (ADR 0010); `core.subject` and its closed error catalog stay untouched. |
| **PD5 - Payers billed by hand** | A payer is marked in `billing.manual_payer`. Their lessons show as "bill manually" and never reach a draft. | Decision V needs a marker, and "company" is not one: companies also get normal invoices. |
| **PD7 - Refresh only reads** | A refresh updates status, number, sums and the drift flag. It never voids a charge - not on a 404 either. Voiding is an explicit action, `mark-cancelled` or `discard`, and each first reads from sevDesk that the invoice is cancelled or gone. | A read error never changes money (decision Q), and one click must not undo a charge sevDesk still holds. |
| **PD8 - Billing scopes need a password** | `billing:read` and `billing:write` do not join `VOUCHED_SCOPES`. | A token the bot vouched for should not move money ([ADR 0009](../decisions/0009-bot-owns-its-discord-workflows.md), point 6). |
| **PD10 - `dry_run` create** | In `dry_run`, creating a draft stores nothing and sends nothing. It answers `200` with a `PlannedDraft`: the planned invoice and the `saveInvoice` request it would have sent. `201` and an invoice row exist only outside `dry_run`; `invoice_state` has no value for a dry run. | No row claims a draft that does not exist. |
| **PD11 - Command-line stage** | The command-line stage is a client of the billing API with a person's token (`scripts/billing_cli.py`), not a second path into the services. | The real sevDesk token exists only in production (ADR 0010); the same routes then serve the portal. |

## Verified behavior

"Run" means the maintainer ran it on the Microsoft 365 tenant in SP-CAL (2026-11); nothing was probed against
sevDesk yet. "Documented" means read in the vendor's published description; the spikes turn the open rows into
fixtures ([Fixture manifest](#fixture-manifest)).

| Question | Result |
|---|---|
| How does sevDesk authenticate? | Documented: base URL `https://my.sevdesk.de/api/v1`, the raw token in the `Authorization` header, no `Bearer` (OpenAPI description at `https://api.sevdesk.de/openapi.yaml`, read 2026-11). A token belongs to one user. |
| Is there a sandbox? | Documented: none, and no webhooks. Secondary sources: a 14-day trial with API access. Trial and real account share the host. |
| Which read identifies the account? | Not documented. The client is built on `GET /SevClient`, which the published description does not list: **open** (Q7, S2). |
| What can `POST /Invoice/Factory/saveInvoice` create? | Documented: on bookkeeping system 2.0 only drafts (status 100). The positions key is `invoicePosSave`. |
| Can a draft be deleted? | Documented: no `DELETE` for invoices. Whether one works anyway: **open** (S18). |
| Which tax rule is § 19 UStG? | Documented: `taxRule` 11. |
| Does a draft carry a number? | The maintainer observed: every open draft shows the next free number in the UI, and the final number is set when the invoice is issued. What the API returns: **open** (Q1). |
| Can text templates be read? | Documented: no. `headText` and `footText` are set by the caller and may hold HTML; a dictionary endpoint lists placeholders. Whether placeholders resolve in a text set through the API, and which tags survive: **open** (Q2). |
| How is a contact's custom field read? | Documented: `ContactCustomFieldSetting` (the definition), `ContactCustomField` (the value per contact), `Contact/Factory/findContactsByCustomFieldValue`. |
| Where do unit and contact-person ids come from? | Documented: no list endpoint for either; they are read from an existing object. The ids: **open** (Q6). |
| Which statuses exist? | Documented: 100 draft, 200 open, 750 partly paid, 1000 paid. |
| How are values typed? | Secondary sources: numbers and booleans arrive as strings; a validation error can arrive as HTTP 500 with a JSON body. **Open** until recorded (S21). |
| What do today's invoices look like? | Observed on two redacted invoices, one per payment method: number `RE-<n>`; one position per lesson (name `<Fach>-Online`, the description of [Family-visible texts](#family-visible-texts), quantity `1,00`, unit `Std`); the § 19 line; a GiroCode and the bank block on both variants; the PayPal link rendered as `…/zahlung?re=<number>&betrag=<d,dd>%20EUR`. |
| What does `/zahlung` accept? | Read in skillsite's code (`parsePaymentRequest` in `invoice-link.ts`): `re` matches `INVOICE_PATTERN` (letters, digits, `. _ - /`, at most 32 characters); `betrag` is an unambiguous euro figure (`90,00`, `90,00 EUR`, `1.234,56`, `90.00`; a bare `1.234` is refused) within `AMOUNT_LIMITS`. |
| How is one mailbox opened to an app? | Documented (Microsoft Learn, "Role Based Access Control for Applications in Exchange Online"): it replaces Application Access Policies; Entra grants and Exchange grants are a union; a change takes 30 minutes to 2 hours to apply. Run: `exchange-rbac-calendar.ps1` set it up; the service principal pointer takes the object id of the enterprise application, not of the app registration (`AADServicePrincipalNotFound` otherwise). `Test-ServicePrincipalAuthorization` answers `InScope` true for the lesson mailbox and false for the other; the recorder's read of the other mailbox got `403` (G6). The app holds no application permission in Entra (the maintainer's statement). |
| How does Graph return recurring lessons? | Documented: `calendarView` expands a series into occurrences; `originalStart` holds an occurrence's planned start. Run: 70 days held 63 single instances, 48 occurrences and 14 exceptions. An occurrence moved in Outlook keeps its `id`, `iCalUId` and `seriesMasterId` and becomes an `exception` (Q16, G2). |
| What does a lesson event carry? | Run, on 125 events over 70 days: `categories` arrive (50 events had at least one); the late-cancellation category is `Abgesagt` (Q9). `isCancelled` was never true, no event was all-day, `showAs` was always `busy`. 44 Teams meetings carry `isOnlineMeeting` and `teamsForBusiness`; a "Teams" category only colours them. Durations: 15, 30, 45, 60 and 120 minutes. Titles: 122 `<subject> mit <name>`, 1 `Kennenlernen`, 2 without the pattern. |
| Is `httpx` installed? | Yes: `uv.lock` holds it through `fastapi[standard]`. |
| How does a new schema get created? | `get_schemata` in `schemata.py` returns every model package; `migrations/env.py` creates its schema. |
| What refuses the delete of a party? | `EXTERNAL_LINKS` in `app/services/crm/parties.py`: `(kind, model, still_linked)` rows that `_external_link_kinds` matches on `model.party_id`, answered as `party_in_use`. Since SF2-1 a row is an `ExternalLink` naming its column (`party_column`). |
| How does a request commit? | `DBSession` (`get_db_session` over `Database.session`) is one transaction for the whole request; it commits before the response. |

## Domain model

### Words

- **Lesson**: one appointment of one student. It comes from the calendar or is added by hand.
- **Tick**: the maintainer confirms that a lesson took place. Only then can it be billed.
- **Open lesson**: billable, ended, and without an active charge.
- **Charge**: the frozen price of one lesson - on an invoice position or as a debit on a credit account.
- **Void**: a charge or entry is marked as no longer counting (`voided_at`, `void_reason`). The row stays.
- **Payer**: who receives the invoice for a student (decision I).
- **Draft**: an invoice created in sevDesk and not sent.
- **Issued**: sevDesk reports status 200 or higher for the invoice.
- **Top-up**: an invoice that adds credit to a credit account.

### Tables

Schema `billing`. This is the target shape; the slice that creates a table adds it to
[`DATABASE_SCHEMA.md`](../DATABASE_SCHEMA.md). Every table has `id UUID PRIMARY KEY` (application-side `uuid4`) and
the timestamps of `TimestampMixin` unless stated. Every reference to a party is
`REFERENCES core.party(id) ON DELETE RESTRICT` (PD9). Who wrote a row is stored as in `auth.auth_audit_log`, in
two columns: `principal_type` and `principal_id`.

```sql
billing.lesson_status = ('planned', 'held', 'cancelled', 'not_held')
billing.rate_source = ('default', 'student', 'lesson')
billing.payment_method = ('paypal', 'transfer')
billing.invoice_state = ('pending', 'created', 'failed', 'unknown', 'issued', 'cancelled', 'discarded')
billing.invoice_kind = ('lessons', 'topup')              -- SFP-2a
billing.prepaid_entry_kind = ('topup', 'adjustment')     -- SFP-1a

-- billing.rate  (SF2-1) - a student's own rate from a day on
student_party_id UUID NOT NULL
cents_per_hour INTEGER NOT NULL CHECK (cents_per_hour >= 0 AND cents_per_hour % 4 = 0)  -- ck_rate_quarter_hour_cents
valid_from DATE NOT NULL
note TEXT NULL
UNIQUE (student_party_id, valid_from)

-- billing.lesson  (SF2-1)
student_party_id UUID NOT NULL
subject TEXT NOT NULL                                    -- the label of the calendar title (PD4)
starts_at TIMESTAMPTZ NOT NULL
minutes INTEGER NOT NULL CHECK (minutes > 0 AND minutes % 15 = 0)  -- ck_lesson_quarter_hour_minutes
status lesson_status NOT NULL DEFAULT 'planned'
billable BOOLEAN NOT NULL DEFAULT false                  -- ck_lesson_billable_status: only when held or cancelled
rate_cents_per_hour INTEGER NULL                         -- the lesson's own rate (PD3), same CHECK as billing.rate
payer_party_id UUID NULL                                 -- SF3-1; set when the lesson becomes billable (PD1)
calendar_event_id UUID NULL UNIQUE                       -- SF4-3; NULL for a lesson added by hand
note TEXT NULL
UNIQUE (student_party_id, starts_at)                     -- uq_lesson_student_start: no lesson twice

-- billing.manual_payer  (SF3-1) - payers billed by hand (PD5)
party_id UUID PRIMARY KEY
note TEXT NULL

-- billing.calendar_alias  (SF4-2) - a name as written in the calendar, mapped to a student
name_normalized TEXT NOT NULL UNIQUE
student_party_id UUID NOT NULL

-- billing.calendar_event  (SF4-3) - one occurrence as last seen in the calendar
external_id TEXT NOT NULL UNIQUE                         -- the Graph event id (Q16)
starts_at TIMESTAMPTZ NOT NULL
ends_at TIMESTAMPTZ NOT NULL
title_subject TEXT NULL                                  -- text before " mit "
title_name TEXT NULL                                     -- text after " mit ": a name, never logged; kept per PD13
cancelled BOOLEAN NOT NULL
reason TEXT NULL                                         -- why no lesson was proposed (Calendar import)
dismissed BOOLEAN NOT NULL DEFAULT false
changed BOOLEAN NOT NULL DEFAULT false                   -- differs from its ticked or charged lesson
vanished BOOLEAN NOT NULL DEFAULT false                  -- gone from the calendar while its lesson stays
last_seen_at TIMESTAMPTZ NOT NULL

-- billing.invoice  (SF5-3)
payer_party_id UUID NOT NULL
state invoice_state NOT NULL
reference TEXT NOT NULL UNIQUE                           -- written to customerInternalNote; reconcile looks for it
payment_method payment_method NOT NULL                   -- frozen (decision K)
invoice_date DATE NOT NULL
delivery_from DATE NULL                                  -- NULL only for a top-up, if Q15 says so
delivery_until DATE NULL
head_text TEXT NOT NULL                                  -- exactly what was sent
foot_text TEXT NOT NULL
total_cents INTEGER NOT NULL CHECK (total_cents >= 0)
sevdesk_invoice_id TEXT NULL UNIQUE
sevdesk_number TEXT NULL                                 -- final once issued
sevdesk_status INTEGER NULL
sevdesk_total_cents INTEGER NULL                         -- as last read back
drift BOOLEAN NOT NULL DEFAULT false
refreshed_at TIMESTAMPTZ NULL
principal_type TEXT NOT NULL                             -- who created it
principal_id TEXT NOT NULL
kind invoice_kind NOT NULL DEFAULT 'lessons'             -- SFP-2a
prepaid_account_id UUID NULL REFERENCES billing.prepaid_account(id)  -- SFP-2a; set exactly for a top-up

-- billing.lesson_charge  (SF5-3) - the one position model (decision O)
lesson_id UUID NOT NULL REFERENCES billing.lesson(id) ON DELETE RESTRICT
invoice_id UUID NULL REFERENCES billing.invoice(id)      -- NOT NULL until SFP-1b
prepaid_account_id UUID NULL REFERENCES billing.prepaid_account(id)  -- SFP-1b
student_party_id UUID NOT NULL                           -- the snapshot starts here
minutes INTEGER NOT NULL
rate_cents_per_hour INTEGER NOT NULL
rate_source rate_source NOT NULL
amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
description TEXT NOT NULL                                -- the position description as sent; '' for a prepaid debit
principal_type TEXT NOT NULL
principal_id TEXT NOT NULL
voided_at TIMESTAMPTZ NULL
void_reason TEXT NULL
UNIQUE (lesson_id) WHERE voided_at IS NULL               -- uq_lesson_charge_active
CHECK (num_nonnulls(invoice_id, prepaid_account_id) = 1) -- ck_lesson_charge_target (SFP-1b)

-- billing.prepaid_account  (SFP-1a)
payer_party_id UUID NOT NULL
opened_on DATE NOT NULL
closed_on DATE NULL                                      -- exclusive
default_topup_cents INTEGER NOT NULL DEFAULT 10000 CHECK (default_topup_cents BETWEEN 1 AND 500000)
note TEXT NULL
UNIQUE (payer_party_id) WHERE closed_on IS NULL          -- uq_prepaid_account_payer_open

-- billing.prepaid_entry  (SFP-1a) - credits; lesson debits are lesson_charge rows (PD12)
prepaid_account_id UUID NOT NULL REFERENCES billing.prepaid_account(id)
kind prepaid_entry_kind NOT NULL
amount_cents INTEGER NOT NULL                            -- topup > 0, adjustment <> 0
effective_on DATE NOT NULL
invoice_id UUID NULL REFERENCES billing.invoice(id)      -- ck_prepaid_entry_source: set exactly for a topup
note TEXT NULL                                           -- ck_prepaid_entry_note: mandatory for an adjustment
principal_type TEXT NOT NULL
principal_id TEXT NOT NULL
voided_at TIMESTAMPTZ NULL
void_reason TEXT NULL
UNIQUE (invoice_id) WHERE voided_at IS NULL
```

No table stores a balance, a "billed" flag or a link from a lesson to its invoice: all three are derived from
active charges and entries.

Every column that references a party has its row in `EXTERNAL_LINKS`, and the delete guard reports its kind:

| Column | Kind | Slice |
|---|---|---|
| `lesson.student_party_id` | `lesson` | SF2-1 |
| `rate.student_party_id` | `rate` | SF2-1 |
| `lesson.payer_party_id` | `lesson_payer` | SF3-1 |
| `manual_payer.party_id` | `manual_payer` | SF3-1 |
| `calendar_alias.student_party_id` | `calendar_alias` | SF4-2 |
| `invoice.payer_party_id` | `invoice` | SF5-3 |
| `lesson_charge.student_party_id` | `lesson_charge` | SF5-3 |
| `prepaid_account.payer_party_id` | `prepaid_account` | SFP-1a |

### A lesson's life

| Status | Means | Billable |
|---|---|---|
| `planned` | Known, not confirmed: imported from the calendar or added by hand. | no |
| `held` | Ticked: it took place. | yes; the maintainer can switch it off (goodwill) |
| `cancelled` | Cancelled late or not attended; the appointment stayed in the calendar. | no; the maintainer can switch it on (decision M) |
| `not_held` | Did not take place and costs nothing. | no |

- A lesson is **open** when it is billable, has ended and has no active charge.
- Setting `billable` on a `planned` or `not_held` lesson is refused (`lesson_not_billable`); moving a billable
  lesson to `planned` or `not_held` clears `billable`.
- A lesson with an active charge on an invoice cannot be edited (`lesson_charged`); the invoice is discarded or
  cancelled first. A lesson with an active prepaid charge can: the charge follows ([Credit accounts](#credit-accounts)).
- There is no delete route. "Remove" in a screen is `not_held`. Only the calendar import deletes, and only a
  `planned` lesson that was never ticked or charged and whose appointment vanished.

## Calendar import

The import reads one calendar of one mailbox (`GRAPH__MAILBOX`, `GRAPH__CALENDAR_NAME`) for a range of days through
`calendarView`, which expands a series into occurrences. It asks for times in UTC and converts with
`Europe/Berlin`. It requests only the fields of fixture G1 - never a body, attendees or an organizer.

What happens to an occurrence, first match wins:

| Occurrence | Result |
|---|---|
| All day | No lesson; `reason = all_day`. |
| Category "Telefon", or a location that is a phone number | No lesson; `reason = phone`. |
| Title `Kennenlernen mit <Name>` | No lesson; `reason = first_meeting`. `Kennenlernen` is never read as a subject. |
| Title without ` mit ` | No lesson; `reason = no_title_pattern`. Shows as unassigned. |
| Duration not a multiple of 15 minutes | No lesson; `reason = duration`. Shows as unassigned. |
| Name matches no student | No lesson; `reason = unmatched`. Shows as unassigned. |
| Name matches several students | No lesson; `reason = ambiguous`. Shows as unassigned. Never a guess. |
| Carries the late-cancellation category `Abgesagt` (Q9), or `isCancelled` | A `cancelled` lesson, not billable. |
| Anything else - a Teams meeting (`isOnlineMeeting`) included | A `planned` lesson. |

- **Title.** The subject is the text before the first ` mit ` (case-insensitive), the name the text after it.
- **Match tokens are not family-visible text.** The words the import matches on - `mit`, `Kennenlernen`,
  `Telefon` and the late-cancellation category - are constants of `app/services/billing/calendar_rules.py`
  (SF4-2).
- **Matching.** An alias (`billing.calendar_alias`) wins. Otherwise the name is compared with the students'
  `firstname lastname` after normalisation: NFKC, casefold, `ä ö ü ß` to `ae oe ue ss`, hyphens and underscores to
  spaces, single spaces. First an exact match; then the one student whose name tokens contain all tokens of the
  calendar name, if that name has at least two tokens. These are the rules of the legacy SkillBot's matcher
  (`extract_student_name`, `normalize_name`, `match_name` in its `schedule.py`) - with one difference: the legacy
  code skipped Teams meetings, billing must not.
- **Unassigned occurrences** are listed until the maintainer assigns them (a student and a subject, optionally
  stored as an alias) or dismisses them.
- **Idempotent.** An occurrence is upserted by `external_id`; importing the same range twice changes nothing.
- **A lesson nobody touched follows the calendar:** a `planned` lesson moves with its appointment and goes when it
  vanishes. An appointment cancelled early is deleted from the calendar and so never shows.
- **A ticked or charged lesson is never changed.** If its appointment moved, shrank or vanished, the import sets
  `changed` on the event and the maintainer decides.
- **Names are kept only as long as needed** (PD13): `title_name` only while an occurrence waits for an assignment;
  unassigned and dismissed occurrences are deleted 90 days after their start.

## Pricing, charges and invoices

- **Amount.** `amount_cents = minutes // 15 * (rate_cents_per_hour // 4)`. Rule G makes both divisions exact; there
  is no other formula and no rounding.
- **Rate.** `resolve_rate`: the lesson's own rate, else the student's `billing.rate` with the latest `valid_from`
  on or before the lesson's Berlin day, else `BILLING__DEFAULT_RATE_CENTS_PER_HOUR`. The result carries its
  `rate_source` so a preview can show where a price comes from.
- **On the wire.** A draft carries amounts as `int` cents; the client sends quantity and price as exact decimal
  strings built from integers (`0.25`, `1.50`, `30.00`) - never a `float`.
- **Payer.** `resolve_payer`: exactly one incoming `pays_for` - that party; none - the student; several - ambiguous,
  which blocks.
- **Route.** `route_lesson(lesson, payer, accounts)` answers `invoice`, `manual` (PD5) or, from SFP-1a, `prepaid`. The
  same pure function serves the tick and the preview, so no lesson falls between two rules.
- **Preview.** `POST /invoices/preview` takes lesson ids of one payer and returns the planned invoice: positions,
  texts, dates, the total, the frozen payment method, a fingerprint, every blocker at once, and warnings. Blockers are
  a closed list:

  | Code | Means |
  |---|---|
  | `lesson_not_open` | The lesson is not billable or already has an active charge. |
  | `lesson_not_ended` | The lesson ends after now. |
  | `payer_ambiguous` | The student has more than one payer. |
  | `payer_missing` | The lesson is billable and has no stored payer (PD1). |
  | `no_sevdesk_contact` | The payer has no `ext.sevdesk_contact`. |
  | `payment_method_missing` | "Zahlungsverfahren" is empty or holds another value than `PayPal` or `Überweisung`. |
  | `manual_payer` | The payer is billed by hand. |
  | `prepaid_payer` | The lesson belongs to a credit account (SFP-1a). |
  | `subject_without_part` | No sevDesk part is configured for the lesson's subject. |

  A warning blocks nothing. The closed list has one code, `payer_changed`: the payer stored on a lesson is not the
  one the CRM resolves now (PD1).

- **Payment method.** The contact's field is addressed by `SEVDESK__PAYMENT_METHOD_FIELD_SETTING_ID`; its values
  `PayPal` and `Überweisung` map to `payment_method` in one place, `app/services/billing/payment_methods.py`
  (SF5-4). They are match tokens, not family-visible text.
- **Create.** `POST /invoices` repeats the preview and compares the fingerprint (PD2). Then, in order: the invoice
  row in state `pending` and one charge per lesson are inserted and committed; `saveInvoice` is called once; the
  outcome is stored in a second transaction ([Invoice states](#invoice-states)). The request-wide `DBSession` is one
  transaction, so this route does not use it for its writes: the create service takes the `Database` and opens two
  short sessions with `Database.session()`, one before the call and one after.
- **Read-back.** After a successful create the invoice and its positions are read back, and the sum of sevDesk's
  positions is compared with `total_cents` to the cent. A difference sets `drift`; nothing is corrected silently.
- **The sevDesk request** follows today's invoices: `invoiceType` `RE`, `status` 100, `currency` `EUR`, the
  contact from `ext.sevdesk_contact`, the contact person and the hour unit from configuration, `taxRule` 11 with
  the § 19 text, `timeToPay` 14, `invoiceDate`, `deliveryDate` and `deliveryDateUntil`, `header`, `headText`,
  `footText`, `customerInternalNote` = the reference, and per position the subject's `part`, the quantity, the
  price and the description in the field `text`. The part's name is not overridden. The field list comes from the
  published description; fixture S11 is the truth and replaces it.
- **Reference.** A short, neutral token: `SF-` and 8 hexadecimal digits, unique per invoice. Whether sevDesk prints
  it on the PDF is **open** (S14); if it does, a family sees it, and the maintainer decides the format again
  ([M8](#for-the-maintainer)).
- **Payment link.** `payment_link(number, total_cents)` builds the `/zahlung` link of
  [Family-visible texts](#family-visible-texts). How the number gets into it is **open** (Q1 to Q3).

### sevDesk in `dry_run` and without a token

- **`dry_run` decides writes only.** A write is never sent and returns its planned request; a read is sent. The
  doctor and a real preview need reads, so they work in `dry_run` with a token.
- **`SEVDESK__ACCOUNT` and `SEVDESK__MODE` are two questions.** `ACCOUNT` (`trial` or `live`) says which account
  the token must belong to and is what the identity check enforces. `MODE` (`dry_run` or `live`) says whether
  writes leave. Production runs `ACCOUNT=live`, first with `MODE=dry_run`; the trial recipes run `ACCOUNT=trial`
  with `MODE=live`. `ACCOUNT` defaults to `trial`, so a real token without it fails closed.
- **The fake is an explicit mode.** `SEVDESK__MODE=fake` gives the billing services a fake sevDesk client instead
  of the real one. SF1 builds `IntegrationMode` with `dry_run` and `live`; `fake` is SF5-4's addition, with the
  one place the client is injected. The mode is refused together with `SEVDESK__ACCOUNT=live`. `just seed-demo`,
  the checks of the admin screens and the test of the command-line stage set it, and `just seed-demo` fills the
  fake with synthetic contacts, payment methods and parts.
- **A missing token selects nothing.** Without a token and without `fake`, every sevDesk call is
  `sevdesk_refused`. A production that lost its token refuses; it never stores an invoice with a fake id.

### Invoice states

| State | Means | Leaves by |
|---|---|---|
| `pending` | Row and charges are committed; the call has no stored outcome. Seen only if the process died in between. | Reconcile, like `unknown`. |
| `created` | The draft exists in sevDesk; `sevdesk_invoice_id` is stored. | Refresh sees status >= 200: `issued`. `discard`: `discarded`. |
| `failed` | sevDesk provably rejected the request, or SkillForge refused to send it: nothing exists. The charges are voided, the lessons are open again. | Final. |
| `unknown` | The outcome is not proven: the draft may exist. The charges stay active, so the lessons cannot be billed again. | Reconcile finds the reference: `created`. Finds nothing: `failed`. Cannot read: stays. |
| `issued` | Sent in sevDesk. The number is final; charges and snapshot never change again. | `mark-cancelled`: `cancelled`. |
| `cancelled` | The maintainer cancelled the invoice in sevDesk, and SkillForge read that from sevDesk. The charges are voided. | Final. |
| `discarded` | A draft the maintainer deleted in sevDesk, and SkillForge read that it is gone. The charges are voided. | Final. |

- **`failed` needs proof.** Only two answers prove that nothing was created: a `4xx`, and a `5xx` whose body
  matches the validation error recorded in S21. The client decides it: both raise `IntegrationRejectedError`. Until
  SF6-1 teaches the client the recorded shape, every `5xx` on a write is `OutcomeUnknownError`. A refusal before
  the request left (`IntegrationRefusedError`) is `failed` too. Every other answer - any other `5xx`, a lost
  connection, a `2xx` that cannot be read (`OutcomeUnknownError`) - is `unknown`.
- **Reconcile** looks for the reference in sevDesk. How - a filter on `customerInternalNote`, or listing the
  contact's drafts created after the pending row and matching the note - is **open** (Q10). "Finds nothing"
  becomes `failed` only if the listing is proven complete; a listing that may be cut off leaves the invoice
  `unknown`.
- **Refresh** reads status, number, positions and sums. It sets `drift` when positions or the sum differ from the
  snapshot, and it changes no charge (PD7).
- **`discard` and `mark-cancelled` read first.** After the identity check, `discard` reads the draft and proceeds
  only if sevDesk answers that it does not exist (the shape is **open**, Q17); `mark-cancelled` reads the invoice
  and proceeds only if sevDesk reports it as cancelled (the shape is **open**, Q12). Anything else is
  `invoice_state`; a read that fails changes nothing.
- **Drift on a draft** means the maintainer edited it in sevDesk. The way out is to make sevDesk match again, or to
  delete the draft there, discard it here and create a new one.

### Errors from an integration

The integration errors (`app/integrations/errors.py`, SF1-1) are their own hierarchy and no `DomainError`. The
billing services translate them; no endpoint catches one.

| Integration error | On a read | On `saveInvoice` |
|---|---|---|
| `IntegrationRefusedError`, `IdentityCheckError` - mode, allowlist or identity said no; nothing was sent | `sevdesk_refused` (502) | Invoice `failed`. |
| `IntegrationRejectedError` - a `4xx`; on a write also a `5xx` with the body of S21 (SF6-1) | `sevdesk_unavailable` (502) | Invoice `failed`. |
| `IntegrationUnavailableError` - a read failed | `sevdesk_unavailable` (502) | - |
| `UnexpectedResponseError` - a body that does not parse | `sevdesk_unavailable` (502) | - |
| `OutcomeUnknownError` - a write failed in transit, any other `5xx`, an unreadable `2xx` | - | Invoice `unknown`. |

- The 502 category is approved (PD6, ADR 0010 point 10).
- An identity read that fails is an `IdentityCheckError` too, so `sevdesk_refused` tells the operator one of two
  things: the configuration does not fit the account, or sevDesk could not be asked. Either way nothing was sent.
- Every error of the calendar client is `calendar_unavailable`.

## Credit accounts

- **Account.** One open account per payer (`uq_prepaid_account_payer_open`), covering every child the payer pays
  for. Its window is `[opened_on, closed_on)`. A new account for the same payer starts on or after the last
  `closed_on`.
- **Routing.** A lesson is prepaid when its payer has an account whose window contains the lesson's Berlin day.
- **Charging at the tick.** Every lesson write service ends with `settle_prepaid(session, lesson)`, as every CRM
  write ends with `saved(...)`. It inserts the charge with `ON CONFLICT DO NOTHING` on `uq_lesson_charge_active`,
  reads the account `FOR KEY SHARE` and checks `closed_on` again. Ticking is never refused for lack of credit.
- **A late cancellation made billable** is charged to the account like any lesson (decision M).
- **Corrections.** Only the minutes changed: the charge is voided and booked anew at the _same_ rate. The day or
  the student changed, or the lesson was ticked again: route and rate are resolved afresh. The tick was removed or
  the lesson set to `not_held`: the charge is voided.
- **Opening an account** charges the open lessons in its window and reports them.
- **Not charged.** A prepaid lesson without a charge - the account was opened later, the payer was fixed later -
  shows in `GET /billable-lessons` with route `prepaid`, ready to be charged, and never in an invoice.
- **Balance** = the sum of active entries minus the sum of active charges of the account, computed in SQL.
- **Statement.** Rows ordered by day - credits before lessons on the same day - then `created_at`, then `id`, with
  a running balance. Each top-up starts a section: carry-over, top-up (number, date, payment badge), lessons per
  child, remainder. A negative remainder is the next section's carry-over. Voided rows show only on request.
- **Top-up.** Positions "N x subject" per child, at the child's rate: the subject's part, quantity N (an N is 60
  minutes), no date. The preview proposes the composition of the last top-up and shows the family's
  `default_topup_cents` beside the sum; the maintainer changes it freely. The draft is created on the invoice path
  above and carries **no credit**.
- **Credit at issue.** A refresh that sees status >= 200 on a top-up posts one `topup` entry in the same
  transaction: the amount is sevDesk's gross sum, parsed with `Decimal` into cents, and `effective_on` is the
  invoice date. A draft shows as "top-up pending". A sum that does not parse posts nothing and warns.
- **After issue.** A changed amount in sevDesk raises a warning with one action, `accept-sevdesk-amount` (void and
  rebook) - never by itself. `mark-cancelled` voids the credit. Paid and partly paid (status 1000, 750) are a
  badge; a top-up unpaid for more than 14 days is flagged.
- **Adjustment.** A manual entry with a sign and a mandatory note - the one way to correct a balance by hand
  (PD12).
- **Hint.** A balance of one hour or less at the highest rate valid today among the account's children
  (decision E) is flagged, unless a top-up is pending ([M11](#for-the-maintainer)). No threshold is stored.
- **Closing** needs balance 0, checked under `FOR UPDATE`. A minus is settled by a remainder top-up first
  ([M6](#for-the-maintainer)): positions in quarter hours at each child's rate. If no such positions add up to the
  minus exactly, the helper blocks and shows it instead of rounding, and the maintainer settles the minus with an
  `adjustment`. After closing, lessons on or after `closed_on` - and lessons found later inside a
  closed window - go to the normal invoice.
- **A lesson the maintainer cancels** is not charged and nothing is paid back: the credit stays (decision M).
  Refunds, reopening an account and closing with credit left are v1.1.

## Family-visible texts

This section is the single source of every German text a family sees. The billing domain's text module
(`app/services/billing/texts.py`, SF5-2) copies each named block verbatim, and `test_texts_match_the_spec` compares
them; family-visible German text lives nowhere else (the exception in [`AGENTS.md`](../../AGENTS.md)). A block
marked **pending approval** is a proposal: it is not built before the maintainer approved it in the session.

What is _not_ family-visible text and lives elsewhere - German words included, because they are matching data:

- **Match tokens** - `mit`, `Kennenlernen`, `Telefon`, the late-cancellation category
  (`app/services/billing/calendar_rules.py`); `Zahlungsverfahren`, `PayPal`, `Überweisung` as sevDesk reports them
  (`app/services/billing/payment_methods.py`, and the field by its configured id).
- **The contact person.** The name under the foot text comes from sevDesk - its placeholder, or the user behind
  `SEVDESK__CONTACT_PERSON_ID` - and never from `texts.py` or any file of the repository.

### Position

The position's name is the sevDesk part of the subject (`<Fach>-Online`: `Mathe-Online`, `Informatik-Online`, ...).
SkillForge never overrides it. Quantity: `minutes / 60` with two decimals (`1,00`, `1,50`), unit `Std`.

`POSITION_DESCRIPTION` - a lesson:

```text
<Wochentag>, <T>. <Monat> <JJJJ> / HH:MM – HH:MM
```

`POSITION_DESCRIPTION_WITH_NAME` - a lesson on an invoice that holds lessons of several children:

```text
<Vorname> / <Wochentag>, <T>. <Monat> <JJJJ> / HH:MM – HH:MM
```

- `<Wochentag>`: `Montag`, `Dienstag`, `Mittwoch`, `Donnerstag`, `Freitag`, `Samstag`, `Sonntag`.
- `<Monat>`: `Januar`, `Februar`, `März`, `April`, `Mai`, `Juni`, `Juli`, `August`, `September`, `Oktober`,
  `November`, `Dezember`.
- `<T>` has no leading zero. Times are `Europe/Berlin`, 24 hours, start and end. Between them stands an en dash
  (U+2013) with one space on each side.
- Example with synthetic data: `Montag, 3. November 2025 / 14:00 – 15:00`.
- A billable cancellation uses the same description (decision M).

A top-up position has the subject's part as its name and quantity N. Today's top-up invoices carry no
description; from SkillForge's first top-up on it carries one (M4, approved). A remainder top-up (M6) uses the same.

`TOPUP_POSITION_DESCRIPTION` - a top-up for one child:

```text
Vorauszahlung für kommende Unterrichtsstunden
```

`TOPUP_POSITION_DESCRIPTION_WITH_NAME` - a top-up for several children:

```text
<Vorname> / Vorauszahlung für kommende Unterrichtsstunden
```

### Document title

The title of the document is sevDesk's field `header`. `DOCUMENT_TITLE`, as today's template has it (M9, approved):

```text
Rechnung Nr. [%DOKUMENTENNUMMER%]
```

sevDesk's UI resolves the placeholder (`Rechnung Nr. RE-0001`). Whether it resolves in a `header` set through the
API is **open** (Q2, S14); if it does not, decision R's fallback fills in the number.

### § 19 line

`TAX_TEXT`, as printed on today's invoices below the net total:

```text
Umsatzsteuer nicht erhoben gemäß §19UStG.
```

Whether sevDesk prints the `taxText` SkillForge sends or its own line for tax rule 11 is **open** (S14).

### Head text

Today's template in sevDesk, raw (M3). `[%VOLLEANREDE%]` is the full salutation (`Sehr geehrte Frau …`,
`Sehr geehrter Herr …`):

```text
[%VOLLEANREDE%],

vielen Dank für Ihren Auftrag und das damit verbundene Vertrauen!
Nachfolgend finden Sie die Abrechnung der vereinbarten Leistungen im oben genannten Zeitraum:
```

A top-up uses the same head text, as today (approved, M3).

`HEAD_TEXT` - the markup SkillForge sends; approved as today's look (M7), and **open** on S15:

```html
{Vollanrede},<br>
<br>
vielen Dank für Ihren Auftrag und das damit verbundene Vertrauen!<br>
Nachfolgend finden Sie die Abrechnung der vereinbarten Leistungen im oben genannten Zeitraum:
```

`{Vollanrede}` stands for `[%VOLLEANREDE%]` if it resolves in a text set through the API (**open**, Q2, S15). If it
does not, SkillForge has to build the salutation itself, which needs a new decision after SP-SD.

### Foot text

Today's templates in sevDesk, raw, for reference. "Überweisung":

```text
Zahlungsverfahren: [%KONTAKT.ZAHLUNGSVERFAHREN%]
Bitte begleichen Sie den Rechnungsbetrag bis zum [%ZAHLUNGSZIEL%] unter Angabe der Rechnungs- oder Kundennummer.
Bei Rückfragen stehe ich Ihnen gerne zur Verfügung.
Mit freundlichen Grüßen
[%KONTAKTPERSON%]
```

"PayPal" differs in its first line, where `Weiter mit PayPal` is a link:

```text
Zahlungsverfahren: [%KONTAKT.ZAHLUNGSVERFAHREN%] (Weiter mit PayPal)
```

Rendered today, `Zahlungsverfahren:` is bold, its value italic - the link included - and `bis zum <Datum>` bold;
the due date is the invoice date plus 14 days.

From SkillForge's first draft on the first line no longer comes from the contact's field: SkillForge writes
`Überweisung` or `PayPal (Weiter mit PayPal)` itself (decided). The rest keeps today's wording word for word
(approved, M1); a modernised wording is the maintainer's to give. The markup is approved as today's look (M7): S15
may change how it is written (which tags sevDesk keeps, `<br>` or paragraphs), never how it looks.

`FOOT_TRANSFER`:

```html
<strong>Zahlungsverfahren:</strong> <em>Überweisung</em><br>
Bitte begleichen Sie den Rechnungsbetrag <strong>bis zum {Zahlungsziel}</strong> unter Angabe der Rechnungs- oder Kundennummer.<br>
Bei Rückfragen stehe ich Ihnen gerne zur Verfügung.<br>
Mit freundlichen Grüßen<br>
{Kontaktperson}
```

`FOOT_PAYPAL`:

```html
<strong>Zahlungsverfahren:</strong> <em>PayPal (<a href="{Zahlungslink}">Weiter mit PayPal</a>)</em><br>
Bitte begleichen Sie den Rechnungsbetrag <strong>bis zum {Zahlungsziel}</strong> unter Angabe der Rechnungs- oder Kundennummer.<br>
Bei Rückfragen stehe ich Ihnen gerne zur Verfügung.<br>
Mit freundlichen Grüßen<br>
{Kontaktperson}
```

- `{Zahlungslink}` is `https://nachhilfe.leonweimann.de/zahlung?re={Rechnungsnummer}&betrag={Betrag}`. Today's
  invoices render it as `…/zahlung?re=<number>&betrag=<d,dd>%20EUR`: the amount with a decimal comma and ` EUR`.
  The link must satisfy the `/zahlung` contract ([Verified behavior](#verified-behavior)); an invoice without
  PayPal has no link.
- `{Zahlungsziel}`, `{Kontaktperson}`, `{Rechnungsnummer}` and `{Betrag}` stand for sevDesk's placeholders if they
  resolve in a text set through the API, and for values SkillForge fills in otherwise: **open** (Q2, Q3).
- The GiroCode and the bank block are printed by sevDesk on both variants today; whether they still appear under
  an own foot text, and whether they can be switched off for PayPal, is **open** (Q4).

`TOPUP_CREDIT_SENTENCE` - **pending approval**, deferred to SFP (M2): a proposal, not built before then; until
then a top-up carries today's foot text. One more line in the foot text of a top-up, before `Bei Rückfragen …`:

```html
Der Rechnungsbetrag wird Ihrem Guthaben gutgeschrieben und mit den kommenden Unterrichtsstunden verrechnet.<br>
```

### Not set by SkillForge

The mail a family receives with its invoice is sent from sevDesk with sevDesk's mail templates (decision B).

## Route map

Target shape, under `/api/v1/billing`, tag `billing`. R is `billing:read`, W is `billing:write`; every route follows
[`api-conventions.md`](api-conventions.md). A party id in a path that does not exist answers with the CRM's
`PartyNotFoundError`.

| Method and path | Scope | Slice | Does |
|---|---|---|---|
| `GET /lessons` | R | SF2-2 | `Page` of lessons; filters `from`, `to` (Berlin days), `student_id`, `status`. |
| `POST /lessons` | W | SF2-2 | Adds a lesson by hand. |
| `GET /lessons/{lesson_id}` | R | SF2-2 | One lesson, with its active charge once SF5-3 exists. |
| `PATCH /lessons/{lesson_id}` | W | SF2-2 | Status (the tick), `billable`, minutes, start, student, subject, own rate, note. |
| `GET /students/{party_id}/rates` | R | SF2-3 | A student's rates, newest first. |
| `PUT /students/{party_id}/rates/{valid_from}` | W | SF2-3 | Sets the rate from that day on; idempotent. |
| `DELETE /students/{party_id}/rates/{valid_from}` | W | SF2-3 | Removes it; existing charges keep their frozen rate. |
| `GET /students/{party_id}/payer` | R | SF3-1 | The resolved payer, whether a sevDesk contact is linked, whether billed by hand. |
| `POST /lessons/{lesson_id}/resolve-payer` | W | SF3-1 | Resolves the payer of a billable lesson afresh (PD1). |
| `PUT`, `DELETE /parties/{party_id}/sevdesk-contact` | W | SF3-1 | Links or unlinks the party's sevDesk contact. |
| `PUT`, `DELETE /parties/{party_id}/manual-billing` | W | SF3-1 | Marks or unmarks a payer as billed by hand. |
| `GET`, `POST /calendar/aliases`, `DELETE /calendar/aliases/{alias_id}` | R, W, W | SF4-2 | The alias table. |
| `POST /calendar/import` | W | SF4-3 | Imports a range of days; answers with counts only. |
| `GET /calendar/events` | R | SF4-3 | `Page` of unassigned and changed occurrences. |
| `POST /calendar/events/{event_id}/assign` | W | SF4-3 | Student and subject for an occurrence, optionally stored as an alias; creates the lesson. |
| `POST /calendar/events/{event_id}/dismiss` | W | SF4-3 | Takes an occurrence off the list. |
| `GET /billable-lessons` | R | SF5-3 | `Page` of every open lesson with its payer and route. |
| `POST /invoices/preview` | R | SF5-4 | The planned invoice for lesson ids of one payer; stores nothing. |
| `POST /invoices` | W | SF6-1 | Creates the draft from a selection and its fingerprint: `201` with the invoice, or `200` with a `PlannedDraft` in `dry_run` (PD10). |
| `GET /invoices`, `GET /invoices/{invoice_id}` | R | SF6-1 | Invoices with state, number, sums and drift. |
| `POST /invoices/{invoice_id}/reconcile` | W | SF6-2 | Resolves `pending` or `unknown`. |
| `POST /invoices/{invoice_id}/refresh` | W | SF6-2 | Reads status, number, positions and sums from sevDesk. |
| `POST /invoices/{invoice_id}/mark-cancelled` | W | SF6-2 | Reads that sevDesk reports the invoice as cancelled, then voids the charges. |
| `POST /invoices/{invoice_id}/discard` | W | SF6-2 | Reads that the draft is gone in sevDesk, then voids the charges. |
| `GET`, `POST /prepaid-accounts`, `GET`, `PATCH /prepaid-accounts/{account_id}` | R, W, R, W | SFP-1a | Credit accounts with balance and flags. |
| `POST /prepaid-accounts/{account_id}/settle` | W | SFP-1b | Charges every open lesson in the account's window; idempotent. |
| `GET /prepaid-accounts/{account_id}/statement` | R | SFP-1c | The statement in sections. |
| `POST /prepaid-accounts/{account_id}/entries` | W | SFP-1c | An adjustment with its note. |
| `POST /prepaid-accounts/{account_id}/entries/{entry_id}/void` | W | SFP-1c | Voids an adjustment. |
| `POST /prepaid-accounts/{account_id}/close` | W | SFP-1c | Closes at balance 0. |
| `POST /prepaid-accounts/{account_id}/topups/preview` | R | SFP-2a | The planned top-up invoice. |
| `POST /prepaid-accounts/{account_id}/topups` | W | SFP-2a | Creates the top-up draft on the invoice path. |
| `POST /invoices/{invoice_id}/accept-sevdesk-amount` | W | SFP-2b | Rebooks a top-up's credit with the amount sevDesk reports. |

No route sends, enshrines, cancels or books in sevDesk, and none passes a request through to a third-party
system.

## Error catalog

Closed. The classes live in `app/services/billing/errors.py`; the `code` is derived from the class name
(`lesson_not_found`). A slice that seems to need another class stops and reports.

| Category | Classes |
|---|---|
| `NotFoundError` (404) | `LessonNotFoundError`, `RateNotFoundError`, `CalendarEventNotFoundError`, `CalendarAliasNotFoundError`, `InvoiceNotFoundError`, `PrepaidAccountNotFoundError`, `PrepaidEntryNotFoundError` |
| `ConflictError` (409) | `LessonAlreadyExistsError`, `LessonChargedError`, `CalendarAliasAlreadyExistsError`, `CalendarEventAlreadyAssignedError`, `SevdeskContactAlreadyLinkedError`, `PreviewOutdatedError`, `InvoiceStateError`, `PrepaidAccountAlreadyOpenError`, `PrepaidAccountNotSettledError`, `PrepaidAccountStartsBeforeLastCloseError`, `PrepaidEntryAlreadyVoidedError` |
| `DomainValidationError` (422) | `UnknownStudentError`, `RateNotQuarterHourError`, `LessonNotBillableError`, `MixedPayersError`, `InvoiceBlockedError`, `PrepaidEntryNotVoidableError`, `TopupAmountOutOfRangeError` |
| `UpstreamError` (502; PD6) | `SevdeskUnavailableError`, `SevdeskRefusedError`, `CalendarUnavailableError` |

- A preview never raises for a blocker: it lists them. `InvoiceBlockedError` answers a create whose preview has
  one - a lesson that is not open included.
- `LessonNotBillableError` answers `billable` set on a `planned` or `not_held` lesson.
- `InvoiceStateError` answers an action that does not fit the invoice's state, in SkillForge or in sevDesk - a
  discard of a draft that still exists, for instance.
- Creating a draft answers `201` with the invoice in state `created`, `failed` or `unknown`: the row exists in all
  three, so the outcome is returned, not raised (the pattern of decision O in
  [`user-authentication.md`](user-authentication.md)).
- No message repeats a name, a calendar title or a sevDesk id.

## Configuration

Settings are nested in `Settings` (`app/core/config.py`) and optional: SkillForge starts without them (ADR 0010).
Every key gets its line in `.env.example` with the slice that adds it; no real value appears anywhere in the
repository.

| Setting | Slice | Meaning |
|---|---|---|
| `SEVDESK__MODE` | SF1-2 | `dry_run` (default) or `live`: whether writes leave. From SF5-4 also `fake`: the fake client, refused with `SEVDESK__ACCOUNT=live`. |
| `SEVDESK__ACCOUNT` | SF1-2 | `trial` (default) or `live`: which account the token must belong to. |
| `SEVDESK__TOKEN` | SF1-2 | The API token; a secret. |
| `SEVDESK__EXPECTED_ACCOUNT_ID` | SF1-2 | The identity the account must report before any other call. |
| `SEVDESK__CONTACT_PERSON_ID` | SF1-2 | The contact person on invoices. |
| `SEVDESK__HOUR_UNIT_ID` | SF1-2 | The unit `Std`. |
| `SEVDESK__PAYMENT_METHOD_FIELD_SETTING_ID` | SF1-2 | The custom field "Zahlungsverfahren". |
| `SEVDESK__PART_IDS` | SF5-4 | Subject label to sevDesk part id (PD4). |
| `GRAPH__MODE` | SF4-1 | As for sevDesk. The calendar client has no write - its token request is declared a read - so both values behave alike today. |
| `GRAPH__TENANT_ID`, `GRAPH__CLIENT_ID` | SF4-1 | The Entra app `skillforge`. |
| `GRAPH__CERTIFICATE_PEM`, `GRAPH__PRIVATE_KEY_PEM` | SF4-1 | This location's credential in secret settings: the PEM text, base64-encoded into one line. No file is mounted. |
| `GRAPH__MAILBOX`, `GRAPH__CALENDAR_NAME` | SF4-1 | The one lesson calendar. |
| `BILLING__DEFAULT_RATE_CENTS_PER_HOUR` | SF2-1 | The default rate; default `3000`. |

- `SKILLFORGE_CONTRACT_RUN=1` is no setting: only the two trial recipes set it, and it is what lets a test reach
  the trial.
- The trial's settings live in `~/.config/skillforge/sevdesk-trial.env`, outside the repository; only the two
  trial recipes load it. The recorder of SP-CAL reads its own env file with paths to this location's certificate
  and key.

## Fixture manifest

Every response shape SF1, SF6 and SFP-2 build on is recorded once, scrubbed, and never edited by hand - a changed
shape is recorded again and the diff reviewed. sevDesk fixtures come from the trial account through the recorder
of SF1-3b and live under `tests/fixtures/sevdesk/<run id>/`; Graph fixtures are the exception: the maintainer runs
the recorder of SP-CAL once, outside every work tree, and hands over its redacted summary; an agent builds G1 to G7
as synthetic fixtures of that shape under `tests/fixtures/graph/` - G7 in the documented shape, since the recorder
reads tokens through MSAL and records no token exchange. The maintainer compares them with the recording once and
then deletes it: nothing recorded from the calendar enters the repository.

"Script" is the spike script (`just sevdesk-spike`). It reads, creates synthetic contacts and drafts, and deletes
nothing - except in S18. What changes an invoice after its creation the maintainer does by hand in the trial's UI;
the script then only reads.

| Id | Recorded | By | Needed by | Answers |
|---|---|---|---|---|
| S1 | `GET /Tools/bookkeepingSystemVersion` | Script | SF1-2 | Q8 |
| S2 | The account's identity and company name: `GET /SevClient`; for comparison `GET /SevUser` and `GET /ContactCustomFieldSetting` with their client ids | Script | SF1-2 | Q7 |
| S3 | A synthetic contact: `GET /Contact/{id}`, `GET /Contact?customerNumber=...&depth=1` | Script | SF1-2, SF5-4 | |
| S4 | `GET /ContactCustomFieldSetting` with the "Zahlungsverfahren" setting | Script | SF1-2 | |
| S5 | `GET /ContactCustomField` and `GET /Contact/Factory/findContactsByCustomFieldValue`: a contact with `PayPal`, one with `Überweisung`, one empty; the listing with `countAll` | Script | SF5-4 | Q18 |
| S6 | The hour unit as it appears on a position or a part | Script | SF1-2 | Q6 |
| S7 | The contact person as it appears on an invoice | Script | SF1-2 | Q6 |
| S8 | `GET /SevSequence/Factory/getByType?objectType=Invoice&type=RE` | Script | SF6-1 | Q1, Q3 |
| S9 | `GET /Part`: one part per subject, with `countAll` | Script | SF5-4 | Q5, Q18 |
| S10 | `GET /Textparser/fetchDictionaryEntriesByType?objectName=Invoice` | Script | SF5-2 | Q2 |
| S11 | `saveInvoice` drafts, request and response: one lesson; several lessons; several children; a top-up "N x subject" | Script | SF1-2, SF6-1, SFP-2a | Q5 |
| S12 | Read-back of S11: `GET /Invoice/{id}`, `GET /Invoice/{id}/getPositions` with `countAll` | Script | SF6-1 | Q18 |
| S13 | Status values: a draft; the same invoice after each way of sending; a booked payment if reachable | The maintainer sends and books in the UI; script reads | SF6-2, SFP-2b | Q13 |
| S14 | The rendered PDF for both foot texts: the title (`header`), the texts, the § 19 line, GiroCode, bank block, and whether the reference (`customerInternalNote`) is printed | Script | SF5-2 | Q2, Q4 |
| S15 | Placeholders and markup in a head and a foot text set through the API - bold, italic, a link, a placeholder inside a link target, line breaks; rendered | Script | SF5-2 | Q2 |
| S16 | Read-back after an edit: a changed quantity, a changed price, an added position | The maintainer edits in the UI; script reads | SF6-2 | |
| S17 | A cancellation: the original and the cancellation invoice | The maintainer cancels in the UI; script reads | SF6-2, SFP-2b | Q12 |
| S18 | `DELETE /Invoice/{id}` on a draft the run created - the script's only delete, on the trial only | Script | SF6-2 | Q11 |
| S19 | Rounding parity: positions with quantities `0.25`, `0.75`, `1.5` at several rates; sevDesk's sums against local cents | Script | SF6-1 | Q14 |
| S20 | A cut connection on `saveInvoice`, then the lookup by reference: the filter on `customerInternalNote` and the list of the contact's drafts | Script | SF6-1, SF6-2 | Q10, Q18 |
| S21 | Error bodies: a validation error with its status, a wrong token | Script | SF1-2, SF6-1 | Q14 |
| S22 | `invoiceNumber` on one draft, on a second draft, after issue | Script; the maintainer issues in the UI | SF6-1 | Q1 |
| S23 | Two drafts created back to back, each with a number from S8; issued in either order: are the numbers kept | Script; the maintainer opens and issues in the UI | SF6-1 | Q3 |
| S24 | `deliveryDate` and `deliveryDateUntil` around midnight and both clock changes: which day sevDesk shows | Script | SF6-1 | Q15 |
| S25 | A draft deleted in the UI: `GET /Invoice/{id}` and the contact's invoice list afterwards | The maintainer deletes in the UI; script reads | SF6-2 | Q17 |
| G1 | A `calendarView` page of the lesson calendar with its paging link; the selected fields | The maintainer | SF4-1 | |
| G2 | A series: an occurrence, a moved occurrence, the same range read twice | The maintainer | SF4-3 | Q16 |
| G3 | A lesson cancelled late, with its category | The maintainer | SF4-2, SF4-3 | Q9 |
| G4 | A Teams lesson, a phone appointment, an all-day entry, a `Kennenlernen` entry, a title without ` mit ` | The maintainer | SF4-2 | |
| G5 | Lessons in the weeks of both clock changes | The maintainer | SF4-3 | |
| G6 | The answer for a mailbox outside the restriction | The maintainer | SF4-1 | |
| G7 | The token response and an error response, in the documented shape | An agent | SF4-1 | |

The trial lasts 14 days. A second trial is not established: do not plan on one, and ask sevDesk's support on the
first day whether it can be extended. SF6 and SFP-2 are built on the recorded fixtures and on fault injection, not
on a live trial.

## Success metrics

- In the month in parallel, every draft matches the invoice the maintainer wrote by hand - positions, texts and the
  sum to the cent - or the difference is explained.
- No lesson has two active charges - ever, checked by `uq_lesson_charge_active`, not by a report.
- No draft exists in the real account that the maintainer did not create from a preview.
- No test object, real name, token or account id is in the repository or in a fixture.
- After the cut-over the maintainer bills without the time tracker and without swapping a foot text by hand.

## Requirements

Slice ids follow the arc: `SF` is SkillForge, `SP` a spike, `SFP` prepaid. Every slice is one PR of at most about
400 lines of code ([`README.md`](README.md)); a slice that outgrows that is split into a stack, with the reason in
the PR. A slice without a _Tests that change_ line edits no existing test.

### Must-have (P0)

**Standing criteria** - they hold for every slice and are ticked with the last one:

- [ ] No test, CI job or agent session reaches a live system: the suite passes without network. Only the two trial
      recipes reach the trial, and an agent runs them only after the maintainer's go in the session.
- [ ] `tests/api/test_crm_architecture.py` passes unchanged: the CRM imports nothing of `app.services.billing`,
      `app.api.v1.billing` or `app.integrations`.
- [ ] `app/services/billing/` imports `app.core`, `app.integrations`, `app.services.crm` and itself (architecture
      test, from SF2-2 on).
- [ ] Every foreign key from `billing` into `core.party` has its row in `EXTERNAL_LINKS`: a test walks the
      metadata (from SF2-1 on), so a new column without a row fails.
- [ ] No real name, e-mail address, token, tenant id or id of a real account in code, tests, fixtures, logs or
      docs; the fixture scan of SF1-3b is green.
- [ ] No name, calendar title, contact id or token in a log line or an error message.
- [ ] Every route follows the API conventions; `openapi.json` is regenerated, never edited.
- [ ] `just check-all` is green on every slice that touches the database, auth or money, `just check` on the
      others.
- [ ] `ARCHITECTURE.md`, `DATABASE_SCHEMA.md` and `.env.example` change in the slice that changes what they
      describe.

**SF0 - Spec, ADR and roadmap.** _Docs only._

- _Technique:_ this spec as a Draft; [ADR 0010](../decisions/0010-outbound-integrations.md) as `Proposed`, with its
  row in the decisions index; [`PROJECT.md`](../PROJECT.md): the billing arc in the roadmap, lessons out of
  "Later", the two rows of "What SkillForge owns", principle 3, the words, "Where we are".
- _Acceptance criteria:_
  - [x] This spec is on the branch in the shape of [`README.md`](README.md), status Draft; every point that waits
        for a spike or an approval is marked **open** or **pending approval**.
  - [x] ADR 0010 exists and the decisions index lists it; no other ADR changed.
  - [x] `PROJECT.md` names the billing arc with this spec and ADR 0010; principle 3 says why a call to a
        third-party system is not pushing.
  - [x] The four files hold no private path, no real name, no account or tenant id and no plan in hours or dates.
  - [x] `just check` is green.

**SP-CAL - Calendar spike.** _Scripts by agents, run by the maintainer._

- _Technique:_ `scripts/graph/exchange-rbac-calendar.ps1` sets up Exchange "RBAC for Applications" for the Entra app
  `skillforge`: the service principal pointer, a management scope that filters on the lesson mailbox's
  `ExternalDirectoryObjectId`, the role assignment `Application Calendars.Read`. `-OutOfScopeMailbox` is mandatory,
  and the script verifies both mailboxes with `Test-ServicePrincipalAuthorization`; it is idempotent and can undo
  itself. `scripts/graph/record_calendar.py` declares its own dependencies (`msal`, `httpx`) as script metadata, so
  the project gains none. It reads the lesson calendar with this location's certificate from an env file outside
  the repository and pseudonymises every event in memory - names, ids, and categories unless one is allowlisted
  with `--category` - with a fresh secret week offset per recording. It writes only outside any git work tree and
  prints a summary that is safe to paste. Agents write both scripts and never run them against Microsoft. The
  maintainer runs them once and hands over the redacted summary; an agent builds synthetic fixtures of its shape
  under `tests/fixtures/graph/` once the fixture scan of SF1-3b exists, and the maintainer deletes the recording
  after one comparison.
- _Acceptance criteria:_
  - [x] Both scripts exist; tests on synthetic events show that a fixture and a summary hold no name, address or
        id, and that the recorder refuses an output path inside a git work tree.
  - [x] The maintainer ran the setup: the lesson mailbox is in scope, the other mailbox is not, and the app holds
        no application permission in Entra (the maintainer's statement, in the PR).
  - [x] The recorder's probe of a mailbox outside the restriction was refused (G6).
  - [x] Synthetic fixtures G1 to G7 are in the repository and pass the fixture scan; the maintainer compared G1 to
        G5 with the recording and deleted it.
  - [x] Q9 and Q16 are answered in this spec, and "Verified behavior" says what was run.

**SF1-1 - Integration foundation.**

- _Technique:_ the package `app/integrations/`. `config.py`: `IntegrationMode` (`dry_run` by default, `live`).
  `errors.py`: the integration errors, their own hierarchy and no `DomainError` - `IntegrationRefusedError` with
  `IdentityCheckError`, `IntegrationRejectedError`, `IntegrationUnavailableError`, `UnexpectedResponseError`,
  `OutcomeUnknownError`. `guards.py` and `transport.py`: the guarded `httpx` transport - https only, an exact host
  allowlist per client, no redirects, `retries=0`. The client says per call whether it is a read (GET) or a write
  (POST, PUT, DELETE). Every request writes one `integration_request` log line: method, path template
  (`/Contact/{id}`, never an id), status, duration.
  Under pytest the transport refuses any real network and refuses `live`, unless the contract run is active
  (`SKILLFORGE_CONTRACT_RUN=1`; the gate is in `tests/conftest.py`). Integration settings are nested in `Settings`
  and optional. `httpx` becomes a direct runtime dependency (decision Y). A nested `app/integrations/AGENTS.md`,
  with a `CLAUDE.md` that holds `@AGENTS.md`.
- _Acceptance criteria:_
  - [x] A request to a host outside the allowlist, over plain http, or following a redirect is refused before a
        connection opens.
  - [x] Under pytest a real connection is refused and `live` is refused, each unless the contract run is active.
  - [x] No request is repeated by the transport, whatever the error.
  - [x] Architecture tests: no module under `app/` imports `httpx`, except the transport module; `app/integrations/`
        imports only `app.core`.
  - [x] The app starts, and `just check` passes, with no integration setting present.
  - [x] `app/integrations/AGENTS.md` exists; the `CLAUDE.md` beside it holds only `@AGENTS.md`.

**SF1-2 - sevDesk client.** _Lands as two PRs: SF1-2, the client and its identity check; SF1-2b, the invoice
write (`invoices.py`)._

- _Technique:_ `app/integrations/sevdesk/`: `config.py` (the settings of [Configuration](#configuration)),
  `models.py` (typed, lenient parsing: sevDesk sends numbers as strings), `identity.py`, `invoices.py`,
  `client.py`. A thin, hand-written async client over the guarded transport; base `https://my.sevdesk.de/api/v1`;
  the raw token in the header. **Identity:** `GET /SevClient` - not in the published description; the account id
  is the SevClient's id, and a trial's company name contains `TRIAL`, case-sensitive. No other call leaves before
  this check passed; it fails closed (`IdentityCheckError`). Q7 and S2 confirm the endpoint. **Writes:**
  `saveInvoice` creates a draft; in `dry_run` it returns the planned request and sends nothing, while reads are
  sent. A draft carries amounts as `int` cents and sends them as exact decimal strings. A write is never retried:
  a `4xx` is `IntegrationRejectedError`; a failure in transit, a `5xx` or an unreadable `2xx` is
  `OutcomeUnknownError`.
- _Acceptance criteria:_
  - [x] In `dry_run`, `saveInvoice` returns the planned request and the transport saw no write; a read is sent.
  - [x] A write that fails in transit, gets a `5xx` or an unreadable `2xx` raises `OutcomeUnknownError` and was
        sent exactly once; a `4xx` raises `IntegrationRejectedError`.
  - [x] No call leaves before the identity check passed; a wrong account id and a trial whose company name lacks
        `TRIAL` each fail closed; `SEVDESK__ACCOUNT` defaults to `trial`.
  - [x] Numbers that arrive as strings parse (`"100"`, `"100.00"`); an amount never passes through `float`, in
        either direction.
  - [x] The token appears in no log line, exception message or `repr`.

**SF1-3a - Doctor and contract run.**

- _Technique:_ `app/cli/sevdesk_doctor.py` and the recipe `just sevdesk-doctor trial|live [--discover]`:
  read-only, checks that every configured id exists, prints ids and object kinds only - never a person's name. Its
  `--discover` mode is how the expected account id is first learned: it reads `GET /SevClient`, `GET /SevUser` and
  `GET /ContactCustomFieldSetting`, and prints the `sevClient` ids they report and "TRIAL marker: yes/no". It is
  the one path that reads before an expected id is configured. The live run is the maintainer's, from the
  maintainer's own terminal. The recipe `just contract-sevdesk`: loads `~/.config/skillforge/sevdesk-trial.env`
  (`SKILLFORGE_SEVDESK_TRIAL_ENV` overrides the path), sets the contract run, runs `pytest -m contract`. The trial
  recipes refuse in CI, when any `SEVDESK__*` variable is already set in the shell - in any letter case - and an env
  file inside the repository or any other git work tree, symlinks resolved, or with whitespace in its path
  (`scripts/sevdesk_trial_env.sh`); a `401` on the
  identity read skips with "trial expired".
- _Acceptance criteria:_
  - [x] The doctor sends only reads and prints ids and kinds - no name, address or e-mail of a person (test on a
        synthetic response).
  - [x] `--discover` sends exactly its three reads and works without an expected id; without it, no call leaves
        before the identity check.
  - [x] `pytest -m contract` is registered as a marker, deselected by `just check` and `just check-all`, and
        absent from CI.
  - [x] `just contract-sevdesk` refuses in CI and with a `SEVDESK__*` variable already set; without the env file,
        or with a `401` on the identity read, it ends with a skip message and exit code 0.
  - [x] The agent guide says it in these words: agents run the trial recipes only after the maintainer's go in
        the session.

**SF1-3b - Recorder and spike.**

- _Technique:_ `app/integrations/recording.py`: the recorder. Nothing is recorded before the identity check
  passed. It drops the `Authorization` header and cookies; person and company objects - SevClient, SevUser,
  Contact, ContactAddress, CommunicationWay - are scrubbed by allowlist at every depth, so only listed fields
  survive; free text elsewhere, an invoice's address among it, goes unless the run sent exactly that string; a whole
  placeholder (`[%RECHNUNGSNUMMER%]`) stays anywhere; IBANs, e-mail addresses (to `example.org`) and phone numbers
  are replaced. Fixtures go to `tests/fixtures/sevdesk/<run id>/`, and no file is overwritten.
  `tests/test_fixture_scan.py` runs in
  `just check`: token-like strings, an e-mail address outside `example.org`, IBAN patterns, phone numbers, the
  `TRIAL` company marker. `scripts/sevdesk_spike.py` with the recipe `just sevdesk-spike`: trial only, with the
  refusals of `just contract-sevdesk`; it walks the [fixture manifest](#fixture-manifest), never retries a write,
  tags every object it creates with its run id (`run` and 18 digits) in `customerInternalNote`, stops when the
  recorder fails, and its only `DELETE` is S18 - on a draft it read first and found carrying its tag.
- _Acceptance criteria:_
  - [x] The scan fails on each of its five patterns (one bad sample per rule, built in the test) and passes on the
        recorder's output for a response that held all five.
  - [x] A field that is not on the allowlist of a person or company object does not reach a fixture.
  - [x] The recorder writes nothing before the identity check passed.
  - [x] `just sevdesk-spike` refuses `SEVDESK__ACCOUNT=live`, CI, and a `SEVDESK__*` variable already set; the
        agent guide's rule - only after the maintainer's go in the session - names this recipe too.
  - [x] The spike script sends every write once and tags it with the run id; its one `DELETE` targets a draft the
        same run created.

**SF1-3c - The manifest's steps.** _On SF1-3b, before the trial exists, so its 14 days are spent recording._

- _Technique:_ the remaining rows of the [fixture manifest](#fixture-manifest) as steps of
  `scripts/sevdesk_spike.py`: S3 (`contact-reads`), S5 (`payment-methods`: two more contacts with a payment method
  each, the run's first without), the S11 variants (`invoice-variants`: several lessons, several children, the
  transfer foot text, a top-up, and for Q5 a position with a part and no name, whose refusal is Q5's answer; each
  lesson with a description in the form of `POSITION_DESCRIPTION`, the top-up's open; the part from `discovery`
  unless `--part-id` names one), S15 (`--head-text` and `--foot-text`, on every draft: `HEAD_TEXT` and
  `FOOT_PAYPAL` line by line as written here, `<br>` and line break included, with placeholders, also in a link
  target with `re=` and `&betrag=`), S19 (`rounding`: quarter, three quarters and one and a half hours at three
  rates, against the local sum), S20 (`cut-connection`: the spike's transport drops the answer of one
  `saveInvoice` after it went out, then the lookups by reference and the contact's drafts), S21 (`error-bodies`: a
  draft without contact, a wrong token), S23 (`numbers`: two drafts back to back with numbers from S8), S24
  (`delivery-dates`, in the year before the invoice date - decision L) and the read of S25 (`deleted-read`). The
  spike reads the listings of Q18 raw with `countAll` and reports whether `total` came back, so an open answer never
  stops the walk; `discovery` prints the placeholders with their values. A row the maintainer completes in the
  trial's UI stays manual: the script reads afterwards, in a step run on its own (`--run-id`, `--step`), never in
  the walk. The write rules of SF1-3b hold for every new step, and a write whose exchange could not be recorded
  stops the run before the next call.
- _Tests that change:_ in `tests/integrations/sevdesk/test_sevdesk_spike.py`, the `trial` fixture gains routes and
  the walk of SF1-3b more steps:
  `test_a_full_walk_creates_tagged_objects_records_everything_and_saves_the_pdf` counts the new writes and texts
  and one write the cut connection leaves unrecorded; `test_a_single_step_runs_alone`,
  `test_delete_reads_first_and_deletes_only_a_draft_of_this_run` and
  `test_delete_of_this_runs_draft_reads_it_then_deletes_it` see the `GET /SevUser` read after the identity check
  and the raw listing of read-back; `test_a_failure_to_record_stops_the_walk` collides with that read;
  `test_a_write_with_an_unknown_outcome_is_not_retried_and_stops_the_walk` finds more writes before the failing
  one.
- _Acceptance criteria:_
  - [x] Every new write is sent once and carries the run id - a contact in its name, a draft in
        `customerInternalNote` - and the walk's fixtures pass the scan.
  - [x] The cut connection leaves its write unknown, sends it once, and is followed by reads only.
  - [x] A step that reads what the maintainer did in the UI is no part of the walk.

**SP-SD - sevDesk spike and freeze.** _After SF1-3c; the trial runs 14 days._

- _Technique:_ the maintainer creates the trial and its env file - the expected account id comes from
  `just sevdesk-doctor trial --discover`. The trial holds synthetic data only: a company name that contains `TRIAL`,
  § 19, a payment term of 14 days, the custom field "Zahlungsverfahren" with its two values and a public example
  IBAN. After the maintainer's go in the session, an agent runs `just sevdesk-spike`, records the manifest and
  writes the answers into this spec; the maintainer does the manifest's UI steps (send, edit, cancel, issue,
  delete a draft) in the trial. What only the real account can show - the raw templates, a top-up invoice of
  today, the ids - the maintainer reads in person (`just sevdesk-doctor live`) and passes on without
  personal data. Then the spec is frozen: open questions move to
  [Decided](#decided-formerly-open-questions), every proposal becomes a
  decision or is replaced, the status becomes `Accepted`, and so does ADR 0010's status line - with ADR 0006's
  status line gaining "amended by 0010".
- _Acceptance criteria:_
  - [ ] Every row S1 to S25 has a scrubbed fixture, or a written reason why it could not be recorded; the scan is
        green.
  - [ ] Every created object carries the run id, and the script's log shows one request per write.
  - [ ] Q1 to Q8, Q10 to Q15, Q17 and Q18 are answered, each naming its fixture.
  - [ ] Trial and real account both report bookkeeping system 2.0.
  - [ ] Every **pending approval** in [Family-visible texts](#family-visible-texts), every row of
        [Approved by the maintainer](#approved-by-the-maintainer) and every item of
        [For the maintainer](#for-the-maintainer) is approved, deferred or replaced by the maintainer, in the session.
  - [ ] The status block says `Accepted`, ADR 0010 says `Accepted`, and no criterion below rests on an open point.

**SF2-1 - Lesson and rate schema.** _Needs PD3 and PD9._

- _Technique:_ the model package `app/core/db/models/billing/` (schema `billing`; `get_schemata` makes
  `migrations/env.py` create it) with `Lesson` and `Rate`; revision `0015`, enum types dropped on downgrade;
  `Scope.BILLING_READ` and `Scope.BILLING_WRITE`, both in `ROLE_SCOPES` for the admin role and not in
  `VOUCHED_SCOPES` (PD8); `BILLING__DEFAULT_RATE_CENTS_PER_HOUR`; `AGENTS.md`'s schema list gains `billing`.
  **The delete guard**, under the CRM's conventions: an `EXTERNAL_LINKS` row is `(kind, model, still_linked)`
  today and `_external_link_kinds` matches `model.party_id`. Billing tables reference the party through
  `student_party_id` and `payer_party_id`, so from this slice on a row names its column
  (`SevdeskContact.party_id`, `Lesson.student_party_id`, `Rate.student_party_id`), and a test walks the metadata
  for billing columns without a row. `PartyInUseError`'s client-visible detail speaks of "external systems"; it
  becomes "Party is still in use", followed by the kinds, and the code stays `party_in_use`. The class's
  docstring, the comment on `EXTERNAL_LINKS` and the comment in `delete_party` - every foreign key into
  `core.party` cascades - are corrected: billing's are `RESTRICT`, and the guard is what answers before the
  database does. [`crm-api.md`](crm-api.md) gets an **Amended** line: decision K and the `PartyInUseError` row of
  its error catalog.
- _Tests that change:_ the admin role's scope set in `tests/auth/test_roles.py`;
  `test_seed_default_scopes_is_idempotent` in `tests/db/test_auth_service.py`;
  `test_the_models_define_the_five_schemas` in `tests/db/models/test_schemata.py`; `_APPLICATION_SCHEMAS` in
  `tests/db/test_migration_apply.py`; `LINKS` and the detail text in
  `tests/db/crm/test_crm_party_delete_api.py`; `PARTY_IN_USE` in `tests/api/test_crm_error_contract.py`.
- _Acceptance criteria:_
  - [x] Upgrade, downgrade by one and upgrade again pass in `tests/db/test_migration_apply.py`.
  - [x] The database refuses a rate not divisible by 4 and a lesson's minutes not divisible by 15
        (`ck_rate_quarter_hour_cents`, `ck_lesson_quarter_hour_minutes`).
  - [x] Deleting a party that has a lesson or a rate answers `409 party_in_use` and names the kind; no path reaches
        the database's `RESTRICT` as a 500.
  - [x] The existing link kinds answer as before, apart from the detail's first words.
  - [x] A token from the Discord exchange never carries a billing scope.

**SF2-2 - Lessons.**

- _Technique:_ `app/services/billing/lessons.py` (function modules, `session` first, no commit - the CRM's shape),
  `app/api/v1/billing/` with router, `lessons.py`, `params.py`, `schemas.py`; the lesson routes of the
  [route map](#route-map); the statuses of [A lesson's life](#a-lessons-life); the billing architecture test
  (pattern: `tests/api/test_crm_architecture.py`) and error-contract test (pattern:
  `tests/api/test_crm_error_contract.py`).
- _Tests that change:_ `test_the_bot_api_is_gone` in `tests/api/test_openapi_contract.py`: the areas and tags gain
  `billing`; in the same file `OPERATION_ID_PATTERN` gains the `billing` prefix.
- _Acceptance criteria:_
  - [x] A tick sets `held` and `billable`; a `cancelled` lesson is billable only after the maintainer said so;
        `billable` on a `planned` or `not_held` lesson is `lesson_not_billable`, and moving a
        billable lesson to `planned` or `not_held` clears the flag (`ck_lesson_billable_status` holds in the
        database).
  - [x] Minutes not divisible by 15 are a `422`; a second lesson of one student at one start is
        `lesson_already_exists`.
  - [x] The list filters by Berlin days: a lesson late in the evening belongs to its Berlin day, in summer and in
        winter time and in the nights of both clock changes.
  - [x] No route deletes a lesson.
  - [x] The error-contract test pins every route's errors.

**SF2-3 - Rates and demo data.**

- _Technique:_ `app/services/billing/rates.py` and the rate routes; `just seed-demo` (`app/cli/seed_demo.py`):
  synthetic families, students, payers, rates and lessons for local screens. It runs only against a local
  database: the host is a loopback address or a single-label host name, such as a compose service; a host name with
  a dot is refused. `AGENTS.md` names the recipe among the commands that need a local database.
- _Tests that change:_ the registries of the billing route tests gain the rate routes: `PATH_VALUES` in
  `tests/api/test_billing_auth.py`; the endpoint list of `test_the_billing_endpoints_carry_no_boilerplate` in
  `tests/api/test_billing_architecture.py`; in `tests/api/test_billing_error_contract.py`, `ENDPOINTS`,
  `EXPECTATIONS` and `CATALOG` gain the rate routes and `RateNotFoundError`, and
  `test_every_pinned_error_is_documented_on_its_route` and `test_no_route_documents_a_domain_error_that_is_not_pinned`
  also walk the new `CRM_EXPECTATIONS` - the CRM's `party_not_found` on the rate routes.
- _Acceptance criteria:_
  - [x] Setting a rate twice for the same day changes nothing; a rate that breaks rule G is
        `rate_not_quarter_hour`.
  - [ ] Deleting a rate changes no stored charge (test from SF5-3 on).
  - [x] `just seed-demo` writes synthetic data only and can run twice.
  - [x] It accepts `localhost`, a loopback address and a compose host name, and refuses a fully qualified host
        name (one test each).

**SF3-1 - Payer and sevDesk contact.** _Needs PD1._

- _Technique:_ `resolve_payer` in a new `app/services/crm/payers.py` - a CRM read under the CRM's conventions,
  importing only `app.core` and the CRM. In billing: `app/services/billing/payers.py` stores the payer on a lesson
  when it becomes billable - ticked, or a late cancellation marked billable - resolves it afresh on request
  (`resolve-payer`), is the one writer of `ext.sevdesk_contact`,
  and keeps `billing.manual_payer` (PD5); the payer routes; one revision; `Lesson.payer_party_id` and
  `ManualPayer.party_id` join `EXTERNAL_LINKS`.
- _Tests that change:_ `NOT_AGGREGATE_WRITES` in `tests/db/crm/test_crm_write_services.py` gains `resolve_payer`;
  `LINKS` in `tests/db/crm/test_crm_party_delete_api.py` gains `manual_payer` - a `lesson_payer` row names its party
  in `payer_party_id`, not `party_id`, so it gets a test of its own in the same file. The registries of the billing
  route tests gain the payer routes: the endpoint list of `test_the_billing_endpoints_carry_no_boilerplate` in
  `tests/api/test_billing_architecture.py`; in `tests/api/test_billing_error_contract.py`, `ENDPOINTS`,
  `EXPECTATIONS`, `CRM_EXPECTATIONS` and `CATALOG` gain the payer routes and `SevdeskContactAlreadyLinkedError`.
  `test_the_billing_party_references_are_lesson_and_rate_and_restrict` in `tests/db/models/test_billing_models.py`
  gains `lesson.payer_party_id` and `manual_payer.party_id`.
- _Acceptance criteria:_
  - [x] No `pays_for`: the student. One: that party, a company included. Two: ambiguous.
  - [x] A lesson gets its payer when it becomes billable: by a tick, and by a late cancellation marked billable.
  - [x] A billable lesson keeps its payer when `pays_for` changes afterwards; becoming billable again, or
        `resolve-payer`, resolves afresh.
  - [x] Only `app/services/billing/payers.py` writes `ext.sevdesk_contact` (architecture test).
  - [x] A contact already linked to another party, or a second contact for one party, is
        `sevdesk_contact_already_linked`.

**SF3-2 - Backfill.**

- _Technique:_ `just billing-backfill <file>` (`app/cli/billing_backfill.py`) reads a mapping CSV the maintainer
  confirmed - student, payer, sevDesk contact id, own rate with its day, billed by hand - from outside the
  repository and applies it through the services: `pays_for` through the CRM's relation service, the contact link,
  the rate, the marker. It is a data step: the maintainer runs it, an agent sees counts. Every run - the dry run
  and `--apply` - prints, before anything is written, the smallest and the largest rate of the file in euros per
  hour: rule G cannot catch euros typed as cents (`32`). The file's columns:
  `student_party_id`, `payer_party_id`, `sevdesk_contact_id`, `manual_billing`, `rate_cents_per_hour`,
  `rate_valid_from` - parties by id, never by name; the module docstring holds the format.
- _Acceptance criteria:_
  - [x] Without `--apply` nothing is written; with it, a second run changes nothing.
  - [x] One bad row - an unknown party, a rate against rule G, a second payer - rejects the whole file before the
        first write.
  - [x] The output holds counts and row numbers, never a name or a contact id.
  - [x] A path inside the repository is refused.

**SF4-1 - Calendar client.** _After SP-CAL. M5 is approved: `pyjwt[crypto]` joins the runtime dependencies here._

- _Technique:_ a Graph client in `app/integrations/` over the guarded transport, with an allowlist of the Microsoft
  login host and the Graph host; a token for the app with this location's certificate, read from the PEM settings
  ([M5](#for-the-maintainer)); `calendarView` of the configured calendar with the fields of G1, paged by
  `@odata.nextLink`, times in UTC. The token request is a POST that changes nothing; the client declares it a read.
  The identity check: before any calendar read the client reads the mailbox's calendars
  (`GET /users/{mailbox}/calendars?$select=id,name,owner`) - the app holds Exchange RBAC for one mailbox and no
  directory permission, so it cannot read the mailbox itself - and finds the one named `GRAPH__CALENDAR_NAME`; if
  that calendar is missing, has no owner or its `owner.address` is not `GRAPH__MAILBOX`, or the read fails, it
  refuses to work (`IdentityCheckError`).
- _Tests that change:_ `CORE_TO_INTEGRATIONS` in `tests/integrations/test_integrations_architecture.py` gains
  `app.integrations.graph.config`, so that `Settings` can nest `GraphSettings`.
- _Acceptance criteria:_
  - [x] Every test runs through a mocked transport: on fixtures G1 to G7, and on an answer built in the test where
        no fixture holds the case - a `429`, a `5xx`, a body that is no JSON or does not parse, an unusable token,
        a transport error.
  - [x] Paging reads to the end; a `429` is repeated as `Retry-After` says, within a total deadline per read, a
        `403` is not.
  - [x] Only the fields of G1 are requested.
  - [x] No calendar read leaves before the identity check passed; a calendar owned by another address and a
        missing calendar each fail closed.
  - [x] Certificate, key and token appear in no log line or exception.

**SF4-2 - Title rules, matching, aliases.**

- _Technique:_ `app/services/billing/calendar_rules.py`, pure: the table, the match tokens and the matching of
  [Calendar import](#calendar-import); `billing.calendar_alias` with its revision and routes;
  `CalendarAlias.student_party_id` joins `EXTERNAL_LINKS`.
- _Tests that change:_ `BILLING_ROWS` in `tests/db/crm/test_crm_party_delete_api.py` gains `calendar_alias` - the
  alias names its party in `student_party_id`, as the other billing rows do, not in `party_id` as `LINKS` expects.
  The registries of the billing route tests gain the alias routes: `PATH_VALUES` in `tests/api/test_billing_auth.py`
  gains `alias_id`; the endpoint list of `test_the_billing_endpoints_carry_no_boilerplate` in
  `tests/api/test_billing_architecture.py`; in `tests/api/test_billing_error_contract.py`, `ENDPOINTS`,
  `EXPECTATIONS` and `CATALOG` gain the alias routes, `CalendarAliasNotFoundError` and
  `CalendarAliasAlreadyExistsError`. `test_the_billing_party_references_are_lesson_and_rate_and_restrict` in
  `tests/db/models/test_billing_models.py` gains `calendar_alias.student_party_id`.
- _Acceptance criteria:_
  - [x] One test per row of the table, on synthetic titles: a Teams lesson is kept; phone, all-day, `Kennenlernen`
        and a title without ` mit ` propose no lesson.
  - [x] Matching: exact; the one student whose tokens contain the name's tokens; several candidates are ambiguous;
        an alias wins.
  - [x] The module imports no session, no integration and reads no clock (architecture test).

**SF4-3 - Calendar import.** _Needs PD13._

- _Technique:_ `billing.calendar_event` with its revision; `import_calendar` in
  `app/services/billing/calendar.py`; the calendar routes; what is kept of a name follows PD13. `UpstreamError` in
  `app/core/errors.py` with its 502 in `STATUS_BY_ERROR` lands here, not with SF5-4: `CalendarUnavailableError` is
  the first class of the category, and the import is the first route that reads another system.
- _Tests that change:_ `tests/test_errors.py` and `tests/api/test_common_errors.py`, only where they list the
  categories, to add the fourth. The registries of the billing route tests gain the calendar routes: `PATH_VALUES`
  in `tests/api/test_billing_auth.py` gains `event_id`; the endpoint list of
  `test_the_billing_endpoints_carry_no_boilerplate` in `tests/api/test_billing_architecture.py`; in
  `tests/api/test_billing_error_contract.py`, `ENDPOINTS`, `EXPECTATIONS` and `CATALOG` gain the calendar routes,
  `CalendarEventNotFoundError`, `CalendarEventAlreadyAssignedError` and `CalendarUnavailableError`.
- _Acceptance criteria:_
  - [x] Importing a range twice changes no row.
  - [x] A ticked or charged lesson is never changed or removed: a moved, shortened or vanished appointment sets
        `changed` (one test each).
  - [x] A `planned` lesson nobody touched follows its appointment: moved, or removed.
  - [x] A late cancellation becomes a `cancelled` lesson that is not billable (G3).
  - [x] Lessons in the weeks of both clock changes land on the right Berlin day and time (G5).
  - [x] `title_name` is stored and cleared as PD13 says, and old unassigned occurrences are deleted (one test per
        rule).
  - [x] The import answers with counts only.

**SF5-1 - Scenarios and golden fixtures.** _After the freeze. Own PR, no code under `app/`._

- _Technique:_ `tests/billing/scenarios/`, a new test area that `AGENTS.md`'s table gains. Each scenario is
  synthetic input (lessons, rates, payer, payment method) and the expected invoice worked out by hand: positions,
  descriptions, quantities, prices, amounts, texts, dates, total, link, and the planned `saveInvoice` request. The
  set: one lesson by transfer; one by PayPal; 45 and 90 minutes; several lessons of one child with a delivery
  range; two children with different rates on one invoice; a rate change between two lessons; a lesson's own rate,
  0 included; a billable cancellation; lessons around midnight and both clock changes; a total of 1,000 EUR or
  more.
- _Acceptance criteria:_
  - [ ] The scenarios load and validate against their schema; no expected value was produced by code of this arc.
  - [ ] The maintainer signed the scenarios off, in the session; the PR says so.

**SF5-2 - Texts and pure builders.**

- _Technique:_ `app/services/billing/texts.py` (the one home of family-visible German text), `money.py`
  (`amount_cents`, `quantity`), `builder.py` (`resolve_rate`, `position_description`, `payment_link`,
  `build_invoice` returning a frozen `PlannedInvoice`). A nested `app/services/billing/AGENTS.md` carries the
  [money rules](#money-rules) verbatim, with a `CLAUDE.md` that holds `@AGENTS.md`.
- _Tests that change:_ none - and no scenario or golden file.
- _Acceptance criteria:_
  - [ ] Every scenario of SF5-1 passes exactly.
  - [ ] `test_texts_match_the_spec`: every constant of `texts.py` equals its named block in this spec.
  - [ ] Architecture tests: no `float` in `money.py` and `builder.py` (no float literal, no `float(`, no `/` on an
        amount); the builders import no session and no integration and read no clock.
  - [ ] For every duration from 15 to 600 minutes and every rate divisible by 4, `amount_cents` equals quantity
        times price.
  - [ ] Every scenario's link satisfies the `/zahlung` contract.
  - [ ] The PR names its mutation probes - the order of rate resolution, the first-name rule, the Berlin
        conversion - and the reviewing session ran them again.

**SF5-3 - Charges and open lessons.**

- _Technique:_ `Invoice` and `LessonCharge` with their revision; `route_lesson`; `GET /billable-lessons` in
  `app/services/billing/invoices.py`; `LessonChargedError` on lesson edits; `Invoice.payer_party_id` and
  `LessonCharge.student_party_id` join `EXTERNAL_LINKS`.
- _Tests that change:_ `LINKS` in `tests/db/crm/test_crm_party_delete_api.py` gains `invoice` and `lesson_charge`.
- _Acceptance criteria:_
  - [ ] A second active charge for one lesson violates `uq_lesson_charge_active`; a voided charge frees the lesson.
  - [ ] Every open lesson appears in `GET /billable-lessons` exactly once, with its route (`invoice` or `manual`).
  - [ ] A lesson with an active charge on an invoice cannot be edited (`lesson_charged`).
  - [ ] A lesson that has a charge - a voided one included - is never deleted: the import leaves it, and the
        database refuses (`ON DELETE RESTRICT`).

**SF5-4 - Preview.** _Needs PD6._

- _Technique:_ `POST /invoices/preview`: blockers, fingerprint (PD2), the payment method read through the sevDesk
  client (`app/services/billing/payment_methods.py`), `SEVDESK__PART_IDS`. The one place the sevDesk client is
  injected into the billing services, and the mode `fake` with its fake client
  ([sevDesk in `dry_run` and without a token](#sevdesk-in-dry_run-and-without-a-token)); `just seed-demo` sets
  the mode and fills the fake. The sevDesk mapping of [Errors from an integration](#errors-from-an-integration);
  `UpstreamError` itself came with SF4-3.
- _Tests that change:_ `_required_scope` in `tests/api/test_billing_auth.py`, which maps every route but a `GET` to
  `billing:write`, learns the preview - a `POST` under `billing:read`. The registries of the billing route tests gain
  the preview: the endpoint list of `test_the_billing_endpoints_carry_no_boilerplate` in
  `tests/api/test_billing_architecture.py`; `ENDPOINTS`, `EXPECTATIONS` and `CATALOG` in
  `tests/api/test_billing_error_contract.py` with its errors.
- _Acceptance criteria:_
  - [ ] The preview changes no row. The same state gives the same fingerprint; a changed rate, duration, payer or
        payment method gives another.
  - [ ] Every blocker of the closed list is reported, several at once (one test per code); `payer_changed` is
        reported as a warning and blocks nothing; a kept payer and a re-resolved one give different fingerprints.
  - [ ] Lessons of two payers are `mixed_payers`.
  - [ ] A failed read is `502 sevdesk_unavailable`, a refusal `502 sevdesk_refused`; neither stores anything, and
        no endpoint catches an integration error (architecture test).
  - [ ] The fake client is used only with `SEVDESK__MODE=fake`; that mode is refused with `SEVDESK__ACCOUNT=live`;
        without a token and without it, a sevDesk call is `sevdesk_refused` and nothing is stored (one test each).

**SF6-1 - Create the draft.**

- _Technique:_ `POST /invoices` as in [Pricing, charges and invoices](#pricing-charges-and-invoices): fingerprint,
  pending row and charges, commit, one call, outcome, read-back; `GET /invoices...`. The sevDesk client learns the
  validation-error body recorded in S21: a `5xx` on a write with that body raises `IntegrationRejectedError`, any
  other `5xx` stays `OutcomeUnknownError`. The create service takes the
  `Database`, not the request's `DBSession`, and opens two short sessions; the billing architecture test names
  this route as the one that may.
- _Tests that change:_ none - and no scenario or golden file.
- _Acceptance criteria:_
  - [ ] The pending row and its charges are committed before the transport sees the request (a second session
        reads them inside the mocked call).
  - [ ] `saveInvoice` is sent exactly once per create: on success, on an error and on a lost connection.
  - [ ] `failed` only on proof: a `4xx`, a refusal before sending, or a `5xx` with the body of S21 - which the
        client, not the service, recognises. A lost connection, any other `5xx` and an unreadable `2xx` give
        `unknown` and keep the charges (one test each).
  - [ ] Two creates for one lesson at the same time: one wins, the other is `lesson_charged` before any call.
  - [ ] An outdated fingerprint is `preview_outdated`: nothing stored, nothing sent.
  - [ ] The request equals each scenario's planned request (SF5-1).
  - [ ] The read-back sum equals the local total to the cent; a difference sets `drift` (S19).
  - [ ] In `dry_run` the answer is `200` with a `PlannedDraft`; nothing is stored and nothing is sent (PD10).
  - [ ] Mutation probes: without the commit before the call, with a retry, and with every non-`2xx` treated as
        `failed`, a named test fails.

**SF6-2 - Reconcile, refresh, cancel.**

- _Technique:_ the four invoice actions of the [route map](#route-map) and the transitions of
  [Invoice states](#invoice-states).
- _Acceptance criteria:_
  - [ ] Reconcile: reference found - `created`; not found - `failed`, charges voided; lookup failed - unchanged
        (S20).
  - [ ] Refresh never changes a charge - not on a `404`, a `401` or a `5xx` (one test each).
  - [ ] Refresh sets `drift` when positions or the sum differ, and names the difference (S16).
  - [ ] No route changes the charges or the snapshot of an issued invoice, except `mark-cancelled`.
  - [ ] `discard` voids only after sevDesk answered that the draft does not exist (S25); a draft that still exists
        is `invoice_state`, a failed read is a `502`, and neither changes a charge.
  - [ ] `discard` on an issued invoice is `invoice_state`, whatever sevDesk says.
  - [ ] `mark-cancelled` voids only after sevDesk reported the invoice as cancelled (S17); otherwise
        `invoice_state`; a failed read changes nothing.
  - [ ] The sevDesk client offers no call that sends, enshrines, cancels, books or deletes (architecture test).

**SF7 - Release and production.** _The maintainer's steps; agents write the runbook._

- _Technique:_ a section "Operating" in this spec: billing a lesson, what `unknown`, `failed` and `drift` mean and
  what to do, a new sevDesk token, a new certificate, the cut-over list. Agents change nothing on the deploy
  side, neither `compose.yml` nor a secret: the PR lists under "For the maintainer" what production needs. That is
  the sevDesk settings with the ids from the doctor, the Graph certificate and key as PEM secrets, and
  `billing:read` and `billing:write` as delegated grants of the login clients - the portal's backend-for-frontend
  and the command-line stage (`just bootstrap-client`). The maintainer sets it, runs the doctor on the real
  account, ships the release and creates the first real draft in person.
- _Acceptance criteria:_
  - [ ] "Operating" exists and names every state a draft can be left in.
  - [ ] `.env.example` lists every setting of [Configuration](#configuration) without a real value.
  - [ ] The maintainer confirmed: the production database has moved, the configuration is set, the doctor is green
        on the real account, `/health` reports the released version.
  - [ ] The maintainer created the first real draft and checked it in sevDesk: title, texts, link, GiroCode, sum.

**SF-CLI - Command-line stage.** _After SF7, before the admin screens._

- _Technique:_ `just billing-drafts` (`scripts/billing_cli.py`), a client of the billing API with a person's token
  (PD11): it imports the calendar for a range, applies a correction file - ticks, cancellations made billable,
  corrected minutes, lessons added by hand - through the lesson routes, prints the preview of the open lessons and,
  with `--create`, creates the drafts: one per lesson, or one per group the file names. Against production only
  the maintainer runs it.
- _Acceptance criteria:_
  - [ ] It uses only routes of the [route map](#route-map) and the token endpoint; it imports nothing from `app/`.
  - [ ] Without `--create` it creates nothing. With it, a create is sent once; `unknown` is printed with the hint to
        reconcile.
  - [ ] A correction row that matches no lesson stops the run before the first change.
  - [ ] Password and client secret come from a prompt or the environment, never from an argument, and are never
        printed.
  - [ ] A test runs it end to end against the app in-process with demo data and `SEVDESK__MODE=fake`.

**SFP-1a - Credit accounts.** _After SF6-2; needs PD12. The slices before it hold no prepaid code._

- _Technique:_ `PrepaidAccount` and `PrepaidEntry` with their revision; the account routes; `route_lesson` answers
  `prepaid`, and the preview reports `prepaid_payer`; `PrepaidAccount.payer_party_id` joins `EXTERNAL_LINKS`.
- _Tests that change:_ the preview and `GET /billable-lessons` tests of SF5-3 and SF5-4 gain the route `prepaid`;
  `LINKS` in `tests/db/crm/test_crm_party_delete_api.py` gains `prepaid_account`.
- _Acceptance criteria:_
  - [ ] One open account per payer; a new one starts on or after the last close.
  - [ ] A lesson whose Berlin day lies in the window of its payer's account routes to `prepaid`; outside it, to
        the invoice.
  - [ ] A prepaid lesson is never in an invoice preview (`prepaid_payer`).
  - [ ] A topup entry without an invoice, and an adjustment without a note, are refused by the database.

**SFP-1b - Charging at the tick.**

- _Technique:_ `lesson_charge` gains `prepaid_account_id` and `ck_lesson_charge_target`; `settle_prepaid` at the
  end of every lesson write service, pinned by a `WRITES` registry
  (`tests/db/billing/test_billing_write_services.py`, pattern: `tests/db/crm/test_crm_write_services.py`); the
  route `settle`; one revision.
- _Acceptance criteria:_
  - [ ] Every public write coroutine of `app/services/billing/lessons.py` has a scenario in `WRITES` and ends with
        `settle_prepaid`.
  - [ ] A tick on a prepaid lesson creates one charge at the child's frozen rate; two ticks at once create one.
  - [ ] A tick that races a close either lands before it or routes to the invoice (DB test).
  - [ ] Corrections follow [Credit accounts](#credit-accounts): minutes only, day or student, tick removed.
  - [ ] A late cancellation made billable is charged like a lesson.
  - [ ] No lesson has a charge on both paths, and a tick is never refused for lack of credit.

**SFP-1c - Statement, adjustments, close.**

- _Technique:_ the statement query (a running sum in SQL), the entry routes, `close`; `just billing-backfill`
  learns the prepaid columns (account, default top-up) and no opening balance.
- _Tests that change:_ none. The statement scenarios land in their own PR first.
- _Acceptance criteria:_
  - [ ] The balance equals a recomputation in Python over synthetic entries and charges; no column stores it.
  - [ ] The statement's order and sections match the golden statements, a negative carry-over included.
  - [ ] Voided rows appear only on request.
  - [ ] Close succeeds at balance 0 and is `prepaid_account_not_settled` otherwise.
  - [ ] After a close, later lessons and lessons found inside the closed window go to the invoice.
  - [ ] Only an adjustment can be voided through the entry routes; a lesson's charge and a top-up's credit are
        `prepaid_entry_not_voidable`.

**SFP-2a - Top-up invoices.**

- _Technique:_ `invoice.kind` and `invoice.prepaid_account_id`; the top-up preview and create on the path of SF5-4
  and SF6-1; the top-up texts. The top-up scenarios land in their own PR first.
- _Tests that change:_ none - and no scenario or golden file.
- _Acceptance criteria:_
  - [ ] Positions: one per child and subject, the subject's part, quantity N, the child's rate, no date; the
        proposal is the last top-up's composition.
  - [ ] Creating a top-up draft changes no balance.
  - [ ] The create is SF6-1's: pending row, one call, `unknown` and reconcile.
  - [ ] An empty payment method blocks; the foot text is the payer's variant with the top-up sentence.

**SFP-2b - Credit at issue.**

- _Technique:_ the refresh of SF6-2 posts the credit for a top-up; `accept-sevdesk-amount`; `mark-cancelled` voids
  the credit; the payment badge and the two flags.
- _Tests that change:_ the refresh tests of SF6-2 gain the top-up cases.
- _Acceptance criteria:_
  - [ ] A refresh that sees status >= 200 posts one `topup` entry with sevDesk's gross sum and the invoice date; a
        second refresh posts nothing.
  - [ ] A sum that is not a euro amount with at most two decimals posts nothing and warns.
  - [ ] A `404`, a `401` and a `5xx` change no entry.
  - [ ] A changed amount after issue warns; only `accept-sevdesk-amount` rebooks.
  - [ ] `mark-cancelled` on a top-up voids its credit.
  - [ ] Status 750 and 1000 show as a field; a top-up unpaid for more than 14 days is flagged; a balance of
        one hour or less at the highest rate valid today among the account's children is flagged unless a top-up
        is pending.

### Nice-to-have (P1)

- **SF8 - No GiroCode on PayPal invoices.** Only if Q4 shows that the API can switch it off per invoice.
- **SFP-3 - "Record as sent".** Only if Q13 shows that the maintainer's way of sending leaves the status at 100:
  an action that posts a top-up's credit and still reads the amount from sevDesk.

### Future considerations (P2)

v1.1 and later, none of it designed here: how long billing data is kept and how a person's data is erased
([M10](#for-the-maintainer)); the statement for families (text, PDF, mail, the portal with `billing:read:own`);
detecting payments and Storno; a low-balance forecast; reopening an account; refunding credit and the Storno path
when closing; hour packs; prepaid and postpaid under one payer; an overdraft limit; a mail text from SkillForge;
e-invoices for authorities; bookings made in the portal so that names always match; a tutor per lesson.

## Decided (formerly open questions)

SP-CAL and SP-SD move each answered question here, with the fixture that shows it.

| Id | Question | Answer | Fixture |
|---|---|---|---|
| Q9 | The exact name of the Outlook category for a late cancellation, and whether Graph delivers `categories` for the lesson calendar. | `Abgesagt`, for a late cancellation and a no-show alike (decision M); Graph delivers `categories`. The category only presets the status; the maintainer decides in the portal. | G3 |
| Q16 | Which Graph id stays the same when an occurrence of a series moves? | All three: `id`, `iCalUId` and `seriesMasterId`; the occurrence becomes an `exception`. `calendar_event.external_id` is the event `id`, the one `calendarView` addresses. | G2 |

## Open questions

### For the spikes

| Id | Question | Decides | Answered by |
|---|---|---|---|
| Q1 | Which `invoiceNumber` does the API return for a draft, for a second draft, and after issue? In the UI every open draft shows the next free number. | How the PayPal link gets its number. | SP-SD (S8, S22) |
| Q2 | Which placeholders do today's templates use - for the salutation, the due date, the contact person, and in the PayPal link's target for number and amount? Do they resolve in a head or foot text set through the API, inside a link target too? Which markup survives? What stands in the title (`header`), and does a placeholder resolve there? | Whether texts carry placeholders or filled-in values; the markup; the head text; the title. | SP-SD (S10, S14, S15); the raw templates from the maintainer |
| Q3 | If SkillForge sets a draft's number from sevDesk's sequence (decision R), does sevDesk keep it - also for two drafts created back to back and issued in either order? | Whether the fallback works at all. | SP-SD (S23) |
| Q4 | Do GiroCode and bank block render under an own foot text? Can the GiroCode be switched off per invoice through the API? | SF8. | SP-SD (S14) |
| Q5 | The part per subject: ids and fields (name, unit, price). Does a position created with `part` and no `name` show the part's name? | `SEVDESK__PART_IDS`; the request. | SP-SD (S9, S11); the real ids from the doctor |
| Q6 | The ids of the unit `Std` and of the contact person, and where they are read. | Configuration. | SP-SD (S6, S7) |
| Q7 | Is `GET /SevClient` the read that yields the account's identity and company name, and does its id equal the client id on `SevUser` and `ContactCustomFieldSetting`? | The identity check of SF1-2, which is built on it. | SP-SD (S2) |
| Q8 | Does the trial run bookkeeping system 2.0 with the functions of "Buchhaltung Pro", like the real account? | Whether trial fixtures speak for the real account. | SP-SD (S1), the doctor on both accounts |
| Q10 | The real name and matching of the `customerInternalNote` filter; otherwise the list of a contact's drafts by creation time. Is that listing complete - paging, limits, drafts included? | Reconcile: "finds nothing" may only mean `failed` if the listing is complete. | SP-SD (S20) |
| Q11 | Does `DELETE` work on a draft although it is not documented? | Whether a draft could ever be cleaned up through the API; v1 does not rely on it. | SP-SD (S18) |
| Q12 | How does a cancelled invoice read: the original's status, the cancellation invoice's shape? | What `mark-cancelled` must read before it voids; v1.1 detection. | SP-SD (S17) |
| Q13 | Does the maintainer's way of sending move an invoice to status 200? | Credit at issue; SFP-3. | SP-SD (S13) |
| Q14 | How are amounts typed in responses, and what do error bodies look like - is a validation error a `5xx` with a recognisable body? | The client's parsing; which `5xx` proves a rejection. | SP-SD (S19, S21) |
| Q15 | How does sevDesk read the time zone of `deliveryDate`? Which delivery date does a top-up carry today? | The request's dates; `delivery_from` on a top-up. | SP-SD (S24); a top-up invoice of today, read by the maintainer |
| Q17 | How does a draft read after the maintainer deleted it in the UI: the answer of `GET /Invoice/{id}`, and is it gone from the contact's list? | What `discard` must read before it voids. | SP-SD (S25) |
| Q18 | Does every listing the client reads whole - `/Part`, `/ContactCustomField`, `/Invoice/{id}/getPositions`, `/Invoice` - answer `countAll=true` with `total`? | The client refuses a listing without `total` ("listing without countAll total"): a listing that lacks it needs another way to be read whole. | SP-SD (S5, S9, S12, S20) |

### For the maintainer

The maintainer answered every item in the review of SF0 (2026-11), as the six rows of
[Approved by the maintainer](#approved-by-the-maintainer).

| Id | Point | Answer |
|---|---|---|
| M1 | The foot text SkillForge sets after its first line. | Approved: today's wording word for word (`FOOT_TRANSFER`, `FOOT_PAYPAL`). |
| M2 | The sentence about credit on a top-up. | Deferred to SFP: `TOPUP_CREDIT_SENTENCE` stays a proposal; until then a top-up carries today's foot text. |
| M3 | The head text's raw template, and a top-up's head text. | Given: [Head text](#head-text) with `[%VOLLEANREDE%]`; a top-up uses the same head text, as today. |
| M4 | The description of a top-up position. | Approved: `TOPUP_POSITION_DESCRIPTION`, and `TOPUP_POSITION_DESCRIPTION_WITH_NAME` for several children; today's top-ups carry none. |
| M5 | How SkillForge gets its Graph token with a certificate. Signing the request needs `cryptography`, a new runtime dependency; Microsoft's own library (`msal`) brings its own HTTP client, past the guarded transport. | Approved: the token request goes through the guarded transport, signed with `pyjwt[crypto]`; the dependency is added with SF4-1. |
| M6 | A remainder top-up whose amount is not a whole number of hours at the child's rate. | Approved: positions in quarter hours at each child's rate, described as any top-up. If none add up to the minus exactly, the helper blocks instead of rounding and the maintainer settles it with an `adjustment`; SFP works the case out. |
| M7 | The markup of head and foot text (bold, italic, link, line breaks); **open** on S15. | Approved as today's look: S15 may change how the blocks are written, not how they look; a visible difference is asked again. |
| M8 | The reference in `customerInternalNote`. If sevDesk prints it (S14), a family sees it. | Approved: `SF-` and 8 hexadecimal digits. If S14 shows it printed, the maintainer decides again. |
| M9 | The document title (`header`). | Approved: as today, `DOCUMENT_TITLE`; whether its placeholder resolves through the API is **open** (Q2, S14). |
| M11 | The hint at low credit. | Changed: the helper hints at one hour or less at the highest rate valid today among the account's children; no threshold is stored. |
| M10 | Data protection: how long lessons, charges, invoices and calendar occurrences are kept, and what erasing a person means while a party with billing rows cannot be deleted (PD9). | Approved: v1 deletes no billing row; the maintainer sets the periods before the cut-over. Not built in v1 (P2). |

### Recorded, not resolved

These are observations, not legal advice, and no slice depends on them.

- **L1 - Terms and prepaid.** The terms and conditions on the website speak of billing after the lesson and of
  paying back amounts for lessons that did not take place. A top-up in advance, credit that stays when the
  maintainer cancels, and "no refund in v1" do not quite match that wording.
- **L2 - E-invoices for authorities.** Whether an authority that pays for a student (BuT) requires an e-invoice
  (XRechnung) is unclear. v1 bills those lessons by hand (decision V).
- **L3 - Privacy text.** Before the first real invoice, the privacy text should mention lessons and billing data
  in SkillForge, the read of the lesson calendar through Microsoft Graph, and the periods of M10. It is a legal
  text: it changes only on the maintainer's request.

## Timeline / phasing

No slice waits for a merge: dependent PRs form a stack ([`AGENTS.md`](../../AGENTS.md)).

| Slice | Needs | Note |
|---|---|---|
| **SF0** | - | |
| **SF1-1**, **SF1-2**, **SF1-2b**, **SF1-3a**, **SF1-3b**, **SF1-3c** | SF0, each the one before | One stack. |
| **SP-CAL** | SF0 | The scripts can start at once; its fixture PR needs the scan of SF1-3b. |
| **SP-SD** | SF1-3c | The maintainer creates the trial only when SF1-3c is merged, so its 14 days are not spent waiting. Freezes the spec. |
| **SF2-1**, **SF2-2**, **SF2-3** | SF0, PD3, PD9; each the one before | Can run beside SF1; both proposals are approved. |
| **SF3-1**, **SF3-2** | SF2-2, PD1 | |
| **SF4-1** | SP-CAL, SF1-1, M5 | |
| **SF4-2** | SF2-1 | |
| **SF4-3** | SF4-1, SF4-2, SF2-2, PD13 | |
| **SF5-1** | the freeze | |
| **SF5-2**, **SF5-3**, **SF5-4** | SF5-1, each the one before; SF5-3 also SF3-1; SF5-4 also SF1-2 and PD6 | |
| **SF6-1**, **SF6-2** | SF5-4 | |
| **SF7** | SF6-2, SF4-3 | The release that carries the slices so far. |
| **SF-CLI** | SF7 | The maintainer can bill from here on; the admin screens follow. |
| **SFP-1a**, **SFP-1b**, **SFP-1c**, **SFP-2a**, **SFP-2b** | SF6-2, PD12; each the one before | Their scenarios land in their own PRs first. |

- **Before the freeze** only SF1, SF2, SF3 and the scripts of SP-CAL are built - SF2 and SF3 only once their
  proposals are approved. SF4 waits for SP-CAL; SF5, SF6 and SFP-2 wait for the freeze.
- **Revisions:** `0015` is SF2-1's. Every later revision takes the next free number in merge order; a slice that
  is rebased onto a new revision moves its `down_revision` (`tests/test_migrations.py` wants one head).
- **Shared files:** `openapi.json` is never merged by hand - take either side, run `just openapi` and
  `just check-all`. `app/core/config.py`, `.env.example`, `justfile`, `pyproject.toml` and `scopes.py` are touched
  by several slices; the later one rebases.
- **skillsite** builds each module after the release that carries its SkillForge slices, starting with a sync of
  the contract.
- **Cut-over:** families billed after the lesson switch on the first day of a month, after one month in parallel
  and the full export from the time tracker. A prepaid family switches when its running cycle is used up
  (decision U).

## Rules for implementing agents

[`AGENTS.md`](../../AGENTS.md) applies in full, the nested guides of `app/integrations/` and
`app/services/billing/` too. Billing slices run only in Claude Code. Agents write all code of this arc, the CRM
parts included - payer resolution and `EXTERNAL_LINKS` - under the CRM's conventions.

### Money rules

They are absolute. A slice that cannot keep one stops and reports.

| # | Rule | Checked by |
|---|---|---|
| 1 | Money is `int` cents. No `float` near an amount; an amount from sevDesk goes through `Decimal` into cents once, and an amount to sevDesk leaves as an exact decimal string. | Architecture test (SF5-2); parsing tests (SF1-2). |
| 2 | The rate is frozen on each position at the preview. A later rate change touches no charge. | Fingerprint and charge tests (SF5-4, SF2-3). |
| 3 | Quarter-hour rule: cents per hour divisible by 4, minutes by 15. There is no rounding code. Needing to round means stop. | `ck_rate_quarter_hour_cents`, `ck_lesson_quarter_hour_minutes` (SF2-1); the exactness test (SF5-2). |
| 4 | Issued is immutable. An issued invoice's charges and snapshot change only through `mark-cancelled`. | Route tests (SF6-2). |
| 5 | One active charge per lesson, on whatever path. A correction voids and rebooks; it never deletes a charge. | `uq_lesson_charge_active` (SF5-3, SFP-1b). |
| 6 | No stored balance. A balance is a sum of active entries minus active charges. | Statement test (SFP-1c). |
| 7 | Builders are pure: no session, no network, no clock. | Architecture test (SF5-2, SF4-2). |
| 8 | A pending row is committed before the call. A POST is never retried. `failed` needs proof; everything unproven is `unknown`, resolved only by a reconcile. | Create tests and their mutation probes (SF6-1). |
| 9 | Times are stored in UTC and billed in `Europe/Berlin`, daylight saving included. | Tests in the weeks of both clock changes (SF2-2, SF4-3, SF5-1). |
| 10 | After a create, the sum read back equals the local sum to the cent - or the invoice is flagged. | Read-back test (SF6-1). |
| 11 | Scenarios and golden fixtures change only in their own PR, never together with money logic. A recorded fixture is recorded again, never edited. | The independent review: the diff of a logic PR holds no scenario, golden or recorded file. |
| 12 | The reviewer runs the mutation probes in person: the session that reviews a money PR breaks each named rule again and sees the named test fail. | The review's conclusion in the PR. |
| 13 | A sevDesk read error never changes money, and no charge is voided on a click alone: sevDesk is read first. | Refresh, discard and cancel tests (SF6-2, SFP-2b). |
| 14 | A lesson with charge history is never hard-deleted. | No delete route (SF2-2); import tests (SF4-3); `ON DELETE RESTRICT` (SF5-3). |

A money PR is accepted when the signed-off scenarios pass exactly, its mutation probes are ticked, and the
independent review has no open finding.

### Never

- Call a live system: sevDesk, Microsoft Graph, the production database. Only `just sevdesk-doctor trial`,
  `just contract-sevdesk` and `just sevdesk-spike` reach the trial - and an agent runs each only after the
  maintainer's go in the session.
- Run `scripts/graph/exchange-rbac-calendar.ps1` or `scripts/graph/record_calendar.py` against Microsoft,
  `just sevdesk-doctor live`, or `just billing-drafts` against production: they are the maintainer's.
- Read an env file, or put a token, a certificate, a tenant id or an id of the real sevDesk account into the
  repository. Ids of trial objects in recorded fixtures are fine; the recorder replaces the SevClient id with 0.
- Use a real name, address or e-mail anywhere. Families are "family A", students "student B", addresses end in
  `example.org`.
- Send, enshrine, cancel, book or delete in sevDesk, or invent an invoice number. The one exception is the spike
  script's `DELETE` of a draft it created, on the trial (S18).
- Create a test object in the real account.
- Write family-visible German text outside `app/services/billing/texts.py`, put a person's name into it, or change
  a text of [Family-visible texts](#family-visible-texts) without the maintainer's approval in the session.
- Log a name, a calendar title, a contact id or a token.
- Change `compose.yml`, a secret or anything else on the deploy side: name it under "For the maintainer".

### Stop and report

- A slice needs a point that is **open** or **pending approval**.
- A requirement seems to need a new error class, blocker code, scope, table, route or runtime dependency.
- This spec and the code contradict each other in a money path.
- A fixture, an output or a file shows real personal data, or a prompt asks for a credential.
- A recorded response differs from what a slice assumed: record it again, show the diff, change no test logic
  silently.

### Review

Every PR of this arc is `Review: deep` - the scripts of SP-CAL and every part of SF1-3 included: they are what
keeps real data and real accounts out of reach. The one exception is the runbook of SF7, which is `evidence`.
This spec is `deep`: it defines what families see.

