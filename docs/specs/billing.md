# Spec: Billing (lessons, rates, invoice drafts in sevDesk, prepaid credit)

> Status: Draft - SF0 done. SP-SD freezes this spec after both spikes; until then nothing is built on a point marked
> **open**.
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
- **It calls nobody.** `app/integrations/` is empty, `ext.sevdesk_contact` has no writer, and no setting holds a
  sevDesk token or a Microsoft identity.
- **Nothing guards a mistake.** sevDesk has no sandbox and no call that deletes a draft, and an issued invoice can
  only be cancelled. Code that bills must be safe to write and to test before it is right.

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

- **The screens.** Lists, previews and dialogs are skillsite's admin helper. The check of PR titles and bodies
  (SF-C) belongs to the repository tooling, not to this spec.
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
| **H - Duration** | A lesson is billed with its planned calendar duration, correctable in 15-minute steps. One position per lesson, quantity = minutes / 60, computed per position and never across a sum. | |
| **I - Payer** | The payer is the party with a `pays_for` relation to the student, otherwise the student. A student has exactly one payer at a time. When the payer changes, the new one applies from a date and older lessons stay with the old one. An authority that pays is a company party with `pays_for`. | The CRM already holds who pays (ADR 0007). |
| **J - Recipient** | The invoice goes to the payer's sevDesk contact (`ext.sevdesk_contact`). Without that link there is no draft. | |
| **K - Payment method** | Read from the custom field "Zahlungsverfahren" of the sevDesk contact, with the values `PayPal` and `Überweisung`: read for the preview and frozen there. An empty or unknown value blocks. One method per payer; paying in advance is not a payment method. The maintainer switches the field to these two values before the cut-over. | sevDesk stays the home of how a customer pays; the PayPal link appears only for `PayPal`. |
| **L - Dates and tax** | Invoice date: the day the draft is created. Delivery date: the lesson's day, or the range from the first to the last lesson. A lesson that has not ended is never billed. Payment term: 14 days for everyone. § 19 UStG: `taxRule` 11. | A normal sevDesk invoice refuses a delivery date after its invoice date. |
| **M - Cancellations** | A late cancellation or a no-show is never billable by itself. It shows as cancelled, and the maintainer makes it billable per lesson with one click; it is then the same position as any lesson. A lesson the maintainer cancelled costs nothing, and its replacement is a normal lesson. | The maintainer decides each case himself. |
| **N - Calendar** | Lessons come from one Microsoft 365 calendar, read through Microsoft Graph without a signed-in user. Titles follow `<Fach> mit <Vorname Nachname>`. Lessons are online (Discord or Teams), cost the same and all count. A phone appointment and the free first meeting (`Kennenlernen mit <Name>`) are never proposed as a lesson but can be added by hand. A title without ` mit ` shows as unassigned; there is no ignore list. An alias table maps calendar names to students. | The lesson calendar holds lessons and first meetings only. Names can carry typos or belong to a student not yet in the CRM. |
| **O - One charge per lesson** | One table, `billing.lesson_charge`, holds invoice positions and prepaid debits alike. A unique index allows one active charge per lesson. "Billed" is derived from it and never stored. | The database rules out a lesson charged twice, not service discipline. |
| **P - Corrections** | A correction voids a charge and books a new one. An issued invoice never changes: a Storno happens in sevDesk and is recorded in SkillForge with one click, which reopens its lessons. A lesson with charge history is never hard-deleted. The calendar import never changes a ticked or charged lesson; it flags the difference. | Every charge keeps its trail. |
| **Q - Outbound writes** | Pending row, commit, call, outcome. A write is never retried. An unknown outcome is a state that a reconcile resolves. A sevDesk read error never changes money. | ADR 0010. |
| **R - Number and reference** | The invoice number is sevDesk's. SkillForge writes its own reference into `customerInternalNote`, which the maintainer does not use. Fallback, only if placeholders do not resolve in a text set through the API ([Q2](#for-the-spikes)): the number is taken from sevDesk's own sequence when the draft is created. | The PayPal link needs the number; a number is never invented. |
| **S - Credit account** | One credit account per payer, for every child the payer pays for, shown as a statement. Credit is money, not hours. A prepaid lesson is charged to the account when it is ticked and never appears on a normal invoice. Ticking is never blocked: the balance may go negative, and the minus carries into the next top-up. No balance is stored. An account closes only at balance 0. | Replaces a project and a budget per cycle. |
| **T - Top-up** | A top-up is a normal invoice with positions "N x subject" at the child's rate, without a date. Credit counts once the invoice is issued (sevDesk status >= 200), with the amount sevDesk reports; payment is only a badge. The amount per family defaults to 100 EUR and is free on every top-up, small amounts included. Below 30 EUR the helper hints; the maintainer triggers the top-up. A prepaid family that wants single lessons for a while stays prepaid and gets a small top-up. | Today's practice. A draft is no invoice, so it is no credit. |
| **U - No opening balance** | A prepaid family moves to SkillForge when its running cycle in the time tracker is used up: it starts at balance 0 with a new top-up. Nothing is computed from the tracker. | No figure has to be verified per family. |
| **V - Authorities** | Lessons an authority pays for (BuT) are billed by hand in v1: they show as "bill manually" and get no draft. | Rare, and an authority may ask for an e-invoice ([L2](#recorded-not-resolved)). |
| **W - What families see** | Nothing new in v1. The maintainer reads a balance from the statement when a family asks. | The admin helper comes first. |
| **X - Scopes** | `billing:read` and `billing:write` on `Scope`; the `admin` role holds both. | One permission system (sketch, principle 5). |
| **Y - Testing** | Every write test against sevDesk runs on a synthetic trial account, through `just contract-sevdesk` (SF1-3) only; the real account never gets a test object. Checks of the admin screens run only against a local SkillForge with `just seed-demo` data (SF2-3). `httpx` becomes a direct runtime dependency with SF1-1 (it is already installed through `fastapi[standard]`); any other new runtime dependency is asked first. | ADR 0010; a draft cannot be deleted through the API. |
| **Z - Cut-over** | The time tracker ends in a hard cut-over on the first day of a month, after one month in parallel and a full export. No import, no write-back. | |

### Proposed with SF0

The maintainer's decisions did not settle the points below. Each is a default with its reason; it stands until the
maintainer changes it in review, and the freeze (SP-SD) turns it into a decision or replaces it.

| Topic | Proposal | Rationale |
|---|---|---|
| **PD1 - Payer on the lesson** | The payer is resolved when a lesson is ticked and stored on it (`Lesson.payer_party_id`). Ticking again, or changing the student or the day, resolves afresh. | Decision I says older lessons stay with the old payer, and `core.party_relation` carries no dates. |
| **PD2 - Preview and create** | The preview is pure and stores nothing. It returns the planned invoice and a fingerprint; creating takes the same selection and that fingerprint, rebuilds, and refuses if they differ. | "Frozen at the preview" without a second write path: the maintainer gets exactly what was shown, or a fresh preview. |
| **PD3 - A lesson's own rate** | A lesson can carry its own rate in cents per hour, 0 included, under the same rule G. | One invariant for every amount. An own _total_ would not fit a quantity in hours. |
| **PD4 - Subject and part** | A lesson's subject is the label from the calendar title (`Mathe`). Configuration maps labels to sevDesk part ids (`SEVDESK__PART_IDS`); a label without a part blocks the preview. No foreign key to `core.subject`. | Parts are account data and so configuration (ADR 0010); `core.subject` and its closed error catalog stay untouched. |
| **PD5 - Payers billed by hand** | A payer is marked in `billing.manual_payer`. Their lessons show as "bill manually" and never reach a draft. | Decision V needs a marker, and "company" is not one: companies also get normal invoices. |
| **PD6 - Upstream errors** | A fourth category joins the error taxonomy: `UpstreamError` in `app/core/errors.py`, mapped to 502 in `STATUS_BY_ERROR`. It arrives with the first route that reads sevDesk during a request (SF5-3). | Endpoints stay free of `try/except` ([ADR 0006](../decisions/0006-error-envelope.md)); 502 says "not your request, not our bug". |
| **PD7 - Refresh only reads** | A refresh updates status, number, sums and the drift flag. It never voids a charge - not on a 404 either. Voiding is an explicit action: `mark-cancelled` or `discard`. | A read error never changes money (decision Q), and a 404 can be a wrong token. |
| **PD8 - Billing scopes need a password** | `billing:read` and `billing:write` do not join `VOUCHED_SCOPES`. | A token the bot vouched for should not move money ([ADR 0009](../decisions/0009-bot-owns-its-discord-workflows.md), point 6). |
| **PD9 - Party references** | Billing tables reference `core.party(id)` with `ON DELETE RESTRICT`, never `core.student`, and join `EXTERNAL_LINKS`. | Taking a student role away must not fail on a lesson; deleting the party answers `party_in_use` instead of a 500. |
| **PD10 - `dry_run` create** | In `dry_run`, creating a draft stores nothing and answers with the request it would have sent. | No row claims a draft that does not exist. |
| **PD11 - Command-line stage** | The command-line stage is a client of the billing API with a person's token (`scripts/billing_cli.py`), not a second path into the services. | The real sevDesk token exists only in production (ADR 0010); the same routes then serve the portal. |
| **PD12 - Credit entries** | `billing.prepaid_entry` knows two kinds: `topup` (from a SkillForge invoice, or an invoice written by hand in sevDesk and registered by its number) and `adjustment` (a manual correction with a mandatory note). | A top-up written by hand must not be lost; a correction needs a trail. Refunds are v1.1. |

## Verified behavior

Nothing here was probed against a live account. "Documented" means read in the vendor's published description;
the spikes turn the open rows into fixtures ([Fixture manifest](#fixture-manifest)).

| Question | Result |
|---|---|
| How does sevDesk authenticate? | Documented: base URL `https://my.sevdesk.de/api/v1`, the raw token in the `Authorization` header, no `Bearer` (OpenAPI description at `https://api.sevdesk.de/openapi.yaml`, read 2026-11). A token belongs to one user. |
| Is there a sandbox? | Documented: none, and no webhooks. Secondary sources: a 14-day trial with API access. Trial and real account share the host. |
| What can `POST /Invoice/Factory/saveInvoice` create? | Documented: on bookkeeping system 2.0 only drafts (status 100). The positions key is `invoicePosSave`. |
| Can a draft be deleted? | Documented: no `DELETE` for invoices. Whether one works anyway: **open** (S18). |
| Which tax rule is § 19 UStG? | Documented: `taxRule` 11. |
| Does a draft carry a number? | The maintainer observed: every open draft shows the next free number in the UI, and the final number is set when the invoice is issued. What the API returns: **open** (Q1). |
| Can text templates be read? | Documented: no. `headText` and `footText` are set by the caller; a dictionary endpoint lists placeholders. Whether placeholders resolve in a text set through the API: **open** (Q2). |
| How is a contact's custom field read? | Documented: `ContactCustomFieldSetting` (the definition), `ContactCustomField` (the value per contact), `Contact/Factory/findContactsByCustomFieldValue`. |
| Where do unit and contact-person ids come from? | Documented: no list endpoint for either; they are read from an existing object. The ids: **open** (Q6). |
| Which statuses exist? | Documented: 100 draft, 200 open, 750 partly paid, 1000 paid. |
| How are values typed? | Secondary sources: numbers and booleans arrive as strings; a validation error can arrive as HTTP 500 with a JSON body. **Open** until recorded (S21). |
| What do today's invoices look like? | Observed on two redacted invoices, one per payment method: number `RE-<n>`; one position per lesson (name `<Fach>-Online`, the description of [Family-visible texts](#family-visible-texts), quantity `1,00`, unit `Std`); the § 19 line; a GiroCode and the bank block on both variants; the PayPal link rendered as `…/zahlung?re=<number>&betrag=<d,dd>%20EUR`. |
| What does `/zahlung` accept? | Read in skillsite's code (`parsePaymentRequest` in `invoice-link.ts`): `re` matches `INVOICE_PATTERN` (letters, digits, `. _ - /`, at most 32 characters); `betrag` is an unambiguous euro figure (`90,00`, `90,00 EUR`, `1.234,56`, `90.00`; a bare `1.234` is refused) within `AMOUNT_LIMITS`. |
| How is one mailbox opened to an app? | Documented (Microsoft Learn, "Role Based Access Control for Applications in Exchange Online"): it replaces Application Access Policies; Entra grants and Exchange grants are a union; a change takes 30 minutes to 2 hours to apply. Not run on the tenant yet: **open** (SP-CAL). |
| How does Graph return recurring lessons? | Documented: `calendarView` expands a series into occurrences. Which id stays stable when an occurrence moves: **open** (Q16). |
| Is `httpx` installed? | Yes: `uv.lock` holds it through `fastapi[standard]`. `app/integrations/` exists and is empty. |
| How does a new schema get created? | `get_schemata` in `schemata.py` returns every model package; `migrations/env.py` creates its schema. |
| What refuses the delete of a party? | `EXTERNAL_LINKS` in `app/services/crm/parties.py`: `(kind, model, still_linked)` rows, answered as `party_in_use`. |

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
`REFERENCES core.party(id) ON DELETE RESTRICT` (PD9).

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
payer_party_id UUID NULL                                 -- SF3-1; set when ticked (PD1)
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
external_id TEXT NOT NULL UNIQUE                         -- which Graph id: open (Q16)
starts_at TIMESTAMPTZ NOT NULL
ends_at TIMESTAMPTZ NOT NULL
title_subject TEXT NULL                                  -- text before " mit "
title_name TEXT NULL                                     -- text after " mit ": a name, never logged
cancelled BOOLEAN NOT NULL
reason TEXT NULL                                         -- why no lesson was proposed (Calendar import)
dismissed BOOLEAN NOT NULL DEFAULT false
changed BOOLEAN NOT NULL DEFAULT false                   -- differs from its ticked or charged lesson
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
created_by TEXT NOT NULL                                 -- "<principal_type>:<principal_id>"
kind invoice_kind NOT NULL DEFAULT 'lessons'             -- SFP-2a
prepaid_account_id UUID NULL REFERENCES billing.prepaid_account(id)  -- SFP-2a; set exactly for a top-up

-- billing.lesson_charge  (SF5-3) - the one position model (decision O)
lesson_id UUID NOT NULL REFERENCES billing.lesson(id) ON DELETE RESTRICT
invoice_id UUID NULL REFERENCES billing.invoice(id)      -- NOT NULL until SFP-1a
prepaid_account_id UUID NULL REFERENCES billing.prepaid_account(id)  -- SFP-1a
student_party_id UUID NOT NULL                           -- the snapshot starts here
minutes INTEGER NOT NULL
rate_cents_per_hour INTEGER NOT NULL
rate_source rate_source NOT NULL
amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
description TEXT NOT NULL                                -- the position description as sent; '' for a prepaid debit
created_by TEXT NOT NULL
voided_at TIMESTAMPTZ NULL
void_reason TEXT NULL
UNIQUE (lesson_id) WHERE voided_at IS NULL               -- uq_lesson_charge_active
CHECK (num_nonnulls(invoice_id, prepaid_account_id) = 1) -- ck_lesson_charge_target (SFP-1a)

-- billing.prepaid_account  (SFP-1a)
payer_party_id UUID NOT NULL
opened_on DATE NOT NULL
closed_on DATE NULL                                      -- exclusive
default_topup_cents INTEGER NOT NULL DEFAULT 10000 CHECK (default_topup_cents BETWEEN 1 AND 500000)
low_balance_cents INTEGER NOT NULL DEFAULT 3000
note TEXT NULL
UNIQUE (payer_party_id) WHERE closed_on IS NULL          -- uq_prepaid_account_payer_open

-- billing.prepaid_entry  (SFP-1a) - credits; lesson debits are lesson_charge rows
prepaid_account_id UUID NOT NULL REFERENCES billing.prepaid_account(id)
kind prepaid_entry_kind NOT NULL
amount_cents INTEGER NOT NULL                            -- topup > 0, adjustment <> 0
effective_on DATE NOT NULL
invoice_id UUID NULL REFERENCES billing.invoice(id)
external_invoice_ref TEXT NULL                           -- the number of a top-up written by hand in sevDesk
note TEXT NULL                                           -- mandatory for an adjustment
created_by TEXT NOT NULL
voided_at TIMESTAMPTZ NULL
void_reason TEXT NULL
CHECK (num_nonnulls(invoice_id, external_invoice_ref) = CASE WHEN kind = 'topup' THEN 1 ELSE 0 END)  -- ck_prepaid_entry_source
UNIQUE (invoice_id) WHERE voided_at IS NULL
UNIQUE (external_invoice_ref) WHERE voided_at IS NULL
```

No table stores a balance, a "billed" flag or a link from a lesson to its invoice: all three are derived from
active charges and entries.

### A lesson's life

| Status | Means | Billable |
|---|---|---|
| `planned` | Known, not confirmed: imported from the calendar or added by hand. | no |
| `held` | Ticked: it took place. | yes; the maintainer can switch it off (goodwill) |
| `cancelled` | Cancelled late or not attended; the appointment stayed in the calendar. | no; the maintainer can switch it on (decision M) |
| `not_held` | Did not take place and costs nothing. | no |

- A lesson is **open** when it is billable, has ended and has no active charge.
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
| Carries the late-cancellation category (name: **open**, Q9), or `isCancelled` | A `cancelled` lesson, not billable. |
| Anything else - a Teams meeting (`isOnlineMeeting`) included | A `planned` lesson. |

- **Title.** The subject is the text before the first ` mit ` (case-insensitive), the name the text after it.
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

## Pricing, charges and invoices

- **Amount.** `amount_cents = minutes // 15 * (rate_cents_per_hour // 4)`. Rule G makes both divisions exact; there
  is no other formula and no rounding.
- **Rate.** `resolve_rate`: the lesson's own rate, else the student's `billing.rate` with the latest `valid_from`
  on or before the lesson's Berlin day, else `BILLING__DEFAULT_RATE_CENTS_PER_HOUR`. The result carries its
  `rate_source` so a preview can show where a price comes from.
- **Quantity** for sevDesk is `minutes / 60` with two decimals, built from integers (`0.25`, `1.00`, `1.50`).
- **Payer.** `resolve_payer`: exactly one incoming `pays_for` - that party; none - the student; several - ambiguous,
  which blocks.
- **Route.** `route_lesson(lesson, payer, accounts)` answers `invoice`, `manual` (PD5) or, from SFP-1a, `prepaid`. The
  same pure function serves the tick and the preview, so no lesson falls between two rules.
- **Preview.** `POST /invoices/preview` takes lesson ids of one payer and returns the planned invoice: positions,
  texts, dates, the total, the frozen payment method, a fingerprint, and every blocker at once. Blockers are a
  closed list:

  | Code | Means |
  |---|---|
  | `payer_ambiguous` | The student has more than one payer. |
  | `no_sevdesk_contact` | The payer has no `ext.sevdesk_contact`. |
  | `payment_method_missing` | "Zahlungsverfahren" is empty or holds another value than `PayPal` or `Überweisung`. |
  | `manual_payer` | The payer is billed by hand. |
  | `prepaid_payer` | The lesson belongs to a credit account (SFP-1a). |
  | `lesson_not_ended` | The lesson ends after now. |
  | `subject_without_part` | No sevDesk part is configured for the lesson's subject. |

- **Create.** `POST /invoices` repeats the preview and compares the fingerprint (PD2). Then, in order: the invoice
  row in state `pending` and one charge per lesson are inserted and committed; `saveInvoice` is called once; the
  outcome is stored in a second transaction ([Invoice states](#invoice-states)).
- **Read-back.** After a successful create the invoice and its positions are read back, and the sum of sevDesk's
  positions is compared with `total_cents` to the cent. A difference sets `drift`; nothing is corrected silently.
- **The sevDesk request** follows today's invoices: `invoiceType` `RE`, `status` 100, `currency` `EUR`, the
  contact, the contact person and the hour unit from configuration, `taxRule` 11 with the § 19 text, `timeToPay`
  14, `invoiceDate`, `deliveryDate` and `deliveryDateUntil`, `headText`, `footText`, `customerInternalNote` = the
  reference, and per position the subject's `part`, the quantity, the price and the description. The part's name
  is not overridden. The field list comes from the published description; fixture S11 is the truth and replaces
  it.
- **Payment link.** `payment_link(number, total_cents)` builds the `/zahlung` link of
  [Family-visible texts](#family-visible-texts). How the number gets into it is **open** (Q1 to Q3).

### Invoice states

| State | Means | Leaves by |
|---|---|---|
| `pending` | Row and charges are committed; the call has no stored outcome. Seen only if the process died in between. | Reconcile, like `unknown`. |
| `created` | The draft exists in sevDesk; `sevdesk_invoice_id` is stored. | Refresh sees status >= 200: `issued`. `discard`: `discarded`. |
| `failed` | sevDesk answered with an error: nothing exists. The charges are voided, the lessons are open again. | Final. |
| `unknown` | No answer arrived: the draft may exist. The charges stay active, so the lessons cannot be billed again. | Reconcile finds the reference: `created`. Finds nothing: `failed`. Cannot read: stays. |
| `issued` | Sent in sevDesk. The number is final; charges and snapshot never change again. | `mark-cancelled`: `cancelled`. |
| `cancelled` | The maintainer cancelled the invoice in sevDesk and recorded it here. The charges are voided. | Final. |
| `discarded` | A draft the maintainer deleted in sevDesk and discarded here. The charges are voided. | Final. |

- **Reconcile** looks for the reference in sevDesk. How - a filter on `customerInternalNote`, or listing the
  contact's drafts created after the pending row and matching the note - is **open** (Q10).
- **Refresh** reads status, number, positions and sums. It sets `drift` when positions or the sum differ from the
  snapshot, and it changes no charge (PD7).
- **Drift on a draft** means the maintainer edited it in sevDesk. He makes sevDesk match again, or deletes the draft
  there, discards it here and creates a new one.

## Credit accounts

- **Account.** One open account per payer (`uq_prepaid_account_payer_open`), covering every child the payer pays
  for. Its window is `[opened_on, closed_on)`. A new account for the same payer starts on or after the last
  `closed_on`.
- **Routing.** A lesson is prepaid when its payer has an account whose window contains the lesson's Berlin day.
- **Charging at the tick.** Every lesson write service ends with `settle_prepaid(session, lesson)`, as every CRM
  write ends with `saved(...)`. It inserts the charge with `ON CONFLICT DO NOTHING` on `uq_lesson_charge_active`,
  reads the account `FOR KEY SHARE` and checks `closed_on` again. Ticking is never refused for lack of credit.
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
- **Hint.** A balance below `low_balance_cents` is flagged, unless a top-up is pending.
- **Closing** needs balance 0, checked under `FOR UPDATE`. A minus is settled by a remainder top-up first
  ([M6](#for-the-maintainer)). After closing, lessons on or after `closed_on` - and lessons found later inside a
  closed window - go to the normal invoice.
- **A lesson the maintainer cancels** is not charged and nothing is paid back: the credit stays (decision M).
  Refunds, reopening an account and closing with credit left are v1.1.

## Family-visible texts

This section is the single source of every German text a family sees. The billing domain's text module
(`app/services/billing/texts.py`, SF5-2) copies each block verbatim, and `test_texts_match_the_spec` compares them;
no other module holds German text (the exception in [`AGENTS.md`](../../AGENTS.md)). A block marked **pending
approval** is a proposal: it is not built before the maintainer approved it in the session.

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

A top-up position has the subject's part as its name, quantity N and an empty description. **Pending approval:** on
a top-up for several children the description is `<Vorname>`.

### § 19 line

`TAX_TEXT`, as printed on today's invoices below the net total:

```text
Umsatzsteuer nicht erhoben gemäß §19UStG.
```

Whether sevDesk prints the `taxText` SkillForge sends or its own line for tax rule 11 is **open** (S14).

### Head text

`HEAD_TEXT`, as rendered on today's invoices:

```text
Sehr geehrte {Anrede/Name},
vielen Dank für Ihren Auftrag und das damit verbundene Vertrauen!
Nachfolgend finden Sie die Abrechnung der vereinbarten Leistungen im oben genannten Zeitraum:
```

`{Anrede/Name}` is filled by sevDesk today. The raw text of the template with its placeholder is **open**: the
maintainer copies it from sevDesk, and Q2 decides whether a placeholder works in a text set through the API.
**Pending approval:** a top-up uses the same head text.

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

Rendered today, `Zahlungsverfahren:` is bold, its value italic and `bis zum <Datum>` bold; the due date is the
invoice date plus 14 days.

From SkillForge's first draft on the first line no longer comes from the contact's field: SkillForge writes it
(decided). **Pending approval** - the rest stays word for word as today; a modernised wording is the maintainer's
to give.

`FOOT_TRANSFER`:

```text
Zahlungsverfahren: Überweisung
Bitte begleichen Sie den Rechnungsbetrag bis zum {Zahlungsziel} unter Angabe der Rechnungs- oder Kundennummer.
Bei Rückfragen stehe ich Ihnen gerne zur Verfügung.
Mit freundlichen Grüßen
{Kontaktperson}
```

`FOOT_PAYPAL`:

```text
Zahlungsverfahren: PayPal (Weiter mit PayPal)
Bitte begleichen Sie den Rechnungsbetrag bis zum {Zahlungsziel} unter Angabe der Rechnungs- oder Kundennummer.
Bei Rückfragen stehe ich Ihnen gerne zur Verfügung.
Mit freundlichen Grüßen
{Kontaktperson}
```

- `Weiter mit PayPal` links to `https://nachhilfe.leonweimann.de/zahlung?re={Rechnungsnummer}&betrag={Betrag}`.
  Today's invoices render it as `…/zahlung?re=<number>&betrag=<d,dd>%20EUR`: the amount with a decimal comma and
  ` EUR`. The link must satisfy the `/zahlung` contract ([Verified behavior](#verified-behavior)); an invoice
  without PayPal has no link.
- `{Zahlungsziel}`, `{Kontaktperson}`, `{Rechnungsnummer}` and `{Betrag}` stand for sevDesk's placeholders if they
  resolve in a text set through the API, and for values SkillForge fills in otherwise: **open** (Q2, Q3).
- The GiroCode and the bank block are printed by sevDesk on both variants today; whether they still appear under
  an own foot text, and whether they can be switched off for PayPal, is **open** (Q4).

`TOPUP_CREDIT_SENTENCE` - **pending approval**, a proposal. One more line in the foot text of a top-up, before
`Bei Rückfragen …`:

```text
Der Rechnungsbetrag wird Ihrem Guthaben gutgeschrieben und mit den kommenden Unterrichtsstunden verrechnet.
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
| `PUT`, `DELETE /parties/{party_id}/sevdesk-contact` | W | SF3-1 | Links or unlinks the party's sevDesk contact. |
| `PUT`, `DELETE /parties/{party_id}/manual-billing` | W | SF3-1 | Marks or unmarks a payer as billed by hand. |
| `GET`, `POST /calendar/aliases`, `DELETE /calendar/aliases/{alias_id}` | R, W, W | SF4-2 | The alias table. |
| `POST /calendar/import` | W | SF4-3 | Imports a range of days; answers with counts only. |
| `GET /calendar/events` | R | SF4-3 | `Page` of unassigned and changed occurrences. |
| `POST /calendar/events/{event_id}/assign` | W | SF4-3 | Student and subject for an occurrence, optionally stored as an alias; creates the lesson. |
| `POST /calendar/events/{event_id}/dismiss` | W | SF4-3 | Takes an occurrence off the list. |
| `GET /billable-lessons` | R | SF5-3 | `Page` of every open lesson with its payer, route and blockers. |
| `POST /invoices/preview` | R | SF5-3 | The planned invoice for lesson ids of one payer; stores nothing. |
| `POST /invoices` | W | SF6-1 | Creates the draft from a selection and its fingerprint. |
| `GET /invoices`, `GET /invoices/{invoice_id}` | R | SF6-1 | Invoices with state, number, sums and drift. |
| `POST /invoices/{invoice_id}/reconcile` | W | SF6-2 | Resolves `pending` or `unknown`. |
| `POST /invoices/{invoice_id}/refresh` | W | SF6-2 | Reads status, number, positions and sums from sevDesk. |
| `POST /invoices/{invoice_id}/mark-cancelled` | W | SF6-2 | Records a Storno done in sevDesk; voids the charges. |
| `POST /invoices/{invoice_id}/discard` | W | SF6-2 | Records that a draft was deleted in sevDesk; voids the charges. |
| `GET`, `POST /prepaid-accounts`, `GET`, `PATCH /prepaid-accounts/{account_id}` | R, W, R, W | SFP-1a | Credit accounts with balance and flags. |
| `POST /prepaid-accounts/{account_id}/settle` | W | SFP-1a | Charges every open lesson in the account's window; idempotent. |
| `GET /prepaid-accounts/{account_id}/statement` | R | SFP-1b | The statement in sections. |
| `POST /prepaid-accounts/{account_id}/entries` | W | SFP-1b | An adjustment, or a top-up written by hand in sevDesk. |
| `POST /prepaid-accounts/{account_id}/entries/{entry_id}/void` | W | SFP-1b | Voids a manual entry. |
| `POST /prepaid-accounts/{account_id}/close` | W | SFP-1b | Closes at balance 0. |
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
| `DomainValidationError` (422) | `UnknownStudentError`, `RateNotQuarterHourError`, `MixedPayersError`, `InvoiceBlockedError`, `PrepaidEntryNotVoidableError`, `TopupAmountOutOfRangeError` |
| `UpstreamError` (502, PD6) | `SevdeskUnavailableError`, `CalendarUnavailableError` |

- A preview never raises for a blocker: it lists them. `InvoiceBlockedError` answers a create whose preview has
  one.
- `InvoiceStateError` answers an action that does not fit the invoice's state - a discard of an issued invoice,
  for instance.
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
| `SEVDESK__MODE` | SF1-2 | `dry_run` (default) or `live`. |
| `SEVDESK__ACCOUNT` | SF1-2 | `trial` or `live`: which account the token must belong to. |
| `SEVDESK__TOKEN` | SF1-2 | The API token; a secret. |
| `SEVDESK__EXPECTED_ACCOUNT_ID` | SF1-2 | The identity the account must report before the first call. |
| `SEVDESK__...` account ids | SF1-2 | Contact person, hour unit, the custom-field setting of the payment method; names are SF1-2's. |
| `SEVDESK__PART_IDS` | SF5-3 | Subject label to sevDesk part id (PD4). |
| `GRAPH__MODE` | SF4-1 | As for sevDesk. |
| `GRAPH__TENANT_ID`, `GRAPH__CLIENT_ID` | SF4-1 | The Entra app `skillforge`. |
| `GRAPH__CERTIFICATE_PATH`, `GRAPH__PRIVATE_KEY_PATH` | SF4-1 | This location's credential. |
| `GRAPH__MAILBOX`, `GRAPH__CALENDAR_NAME` | SF4-1 | The one lesson calendar. |
| `BILLING__DEFAULT_RATE_CENTS_PER_HOUR` | SF2-1 | The default rate; default `3000`. |

`SKILLFORGE_CONTRACT_RUN=1` is no setting: only `just contract-sevdesk` sets it, and it is what lets a test reach
the trial. Whether a read is sent in `dry_run` is SF1-2's to fix and to record here.

## Fixture manifest

Every response shape SF1, SF6 and SFP-2 build on is recorded once, scrubbed, and never edited by hand - a changed
shape is recorded again and the diff reviewed. sevDesk fixtures come from the trial account through the recorder
of SF1-3 and live under `tests/fixtures/sevdesk/`; Graph fixtures come from the maintainer's run of the recorder
of SP-CAL, pseudonymised, and live under `tests/fixtures/graph/`.

| Id | Recorded | Needed by | Answers |
|---|---|---|---|
| S1 | `GET /Tools/bookkeepingSystemVersion` | SF1-2 | Q8 |
| S2 | The read that yields the account's identity and company name | SF1-2 | Q7 |
| S3 | A synthetic contact: `GET /Contact/{id}`, `GET /Contact?customerNumber=...&depth=1` | SF1-2, SF5-3 | |
| S4 | `GET /ContactCustomFieldSetting` with the "Zahlungsverfahren" setting | SF1-2 | |
| S5 | `GET /ContactCustomField` and `GET /Contact/Factory/findContactsByCustomFieldValue`: a contact with `PayPal`, one with `Überweisung`, one empty | SF5-3 | |
| S6 | The hour unit as it appears on a position or a part | SF1-2 | Q6 |
| S7 | The contact person as it appears on an invoice | SF1-2 | Q6 |
| S8 | `GET /SevSequence/Factory/getByType?objectType=Invoice&type=RE` | SF6-1 | Q1, Q3 |
| S9 | `GET /Part`: one part per subject | SF5-3 | Q5 |
| S10 | `GET /Textparser/fetchDictionaryEntriesByType?objectName=Invoice` | SF5-2 | Q2 |
| S11 | `saveInvoice` drafts, request and response: one lesson; several lessons; several children; a top-up "N x subject" | SF1-2, SF6-1, SFP-2a | Q5 |
| S12 | Read-back of S11: `GET /Invoice/{id}`, `GET /Invoice/{id}/getPositions` | SF6-1 | |
| S13 | Status values: a draft, the same invoice after each way of sending the trial offers, a booked payment if reachable | SF6-2, SFP-2b | Q13 |
| S14 | The rendered PDF for both foot texts: text, § 19 line, GiroCode, bank block | SF5-2 | Q4 |
| S15 | Placeholders in a head and a foot text set through the API, one inside a link target; rendered | SF5-2 | Q2 |
| S16 | Read-back after an edit in the sevDesk UI: a changed quantity, a changed price, an added position | SF6-2 | |
| S17 | A cancellation done in the UI: the original and the cancellation invoice | SF6-2, SFP-2b | Q12 |
| S18 | `DELETE /Invoice/{id}` on a draft | SF6-2 | Q11 |
| S19 | Rounding parity: positions with quantities `0.25`, `0.75`, `1.5` at several rates; sevDesk's sums against local cents | SF6-1 | Q14 |
| S20 | A cut connection on `saveInvoice`, then the lookup by reference: the filter on `customerInternalNote` and the list of the contact's drafts | SF6-1, SF6-2 | Q10 |
| S21 | Error bodies: a validation error, a wrong token | SF1-2 | Q14 |
| S22 | `invoiceNumber` on one draft, on a second draft, after issue | SF6-1 | Q1 |
| S23 | A draft created with a number from S8: is it kept when opened and issued | SF6-1 | Q3 |
| S24 | `deliveryDate` and `deliveryDateUntil` around midnight and both clock changes: which day sevDesk shows | SF6-1 | Q15 |
| G1 | A `calendarView` page of the lesson calendar with its paging link; the selected fields | SF4-1 | |
| G2 | A series: an occurrence, a moved occurrence, the same range read twice | SF4-3 | Q16 |
| G3 | A lesson cancelled late, with its category | SF4-2, SF4-3 | Q9 |
| G4 | A Teams lesson, a phone appointment, an all-day entry, a `Kennenlernen` entry, a title without ` mit ` | SF4-2 | |
| G5 | Lessons in the weeks of both clock changes | SF4-3 | |
| G6 | The answer for a mailbox outside the restriction | SF4-1 | |
| G7 | The token response and an error response, scrubbed | SF4-1 | |

The trial lasts 14 days. If a later slice needs a shape the trial no longer serves, a second trial costs a new env
file - every id is configuration - and the fixtures are recorded again and diffed.

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

- [ ] No test, CI job or agent session reaches a live system: the suite passes without network, and only
      `just contract-sevdesk` reaches the trial.
- [ ] `tests/api/test_crm_architecture.py` passes unchanged: the CRM imports nothing of billing or the integrations.
- [ ] `app/integrations/` imports only `app.core`; `app/services/billing/` imports `app.core`, `app.integrations`,
      `app.services.crm` and itself (architecture test, from SF2-2 on).
- [ ] No real name, e-mail address, token, tenant id or account id in code, tests, fixtures, logs or docs; the
      fixture scan of SF1-3 is green.
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
  - [x] The three files hold no private path, no real name, no account or tenant id and no plan in hours or
        dates.
  - [x] `just check` is green.

**SP-CAL - Calendar spike.** _Scripts by agents, run by the maintainer._

- _Technique:_ `scripts/graph/exchange-rbac-calendar.ps1` sets up Exchange "RBAC for Applications" for the Entra app
  `skillforge` - the service principal pointer, a management scope for the one lesson mailbox, the role assignment
  `Application Calendars.Read` - and checks it with `Test-ServicePrincipalAuthorization`; it is idempotent and can
  undo itself. `scripts/graph/record_calendar.py` reads the lesson calendar with this location's certificate from
  an env file outside the repository, pseudonymises every event in memory, writes a fixture outside the repository
  and prints a summary that is safe to paste. Agents write both and never run them against Microsoft. The
  maintainer runs them and hands over the fixture; an agent adds it under `tests/fixtures/graph/` once the fixture
  scan of SF1-3 exists.
- _Acceptance criteria:_
  - [ ] Both scripts exist; tests on synthetic events show that a fixture and a summary hold no name, address or
        id.
  - [ ] The maintainer ran the setup: the lesson mailbox is in scope, another mailbox is not, and the app holds no
        application permission in Entra (his statement, in the PR).
  - [ ] The recorder's probe of a mailbox outside the restriction was refused (G6).
  - [ ] Fixtures G1 to G7 are in the repository, pseudonymised, and pass the fixture scan.
  - [ ] Q9 and Q16 are answered in this spec, and "Verified behavior" says what was run.

**SF1-1 - Integration foundation.**

- _Technique:_ the package `app/integrations/`: `IntegrationMode` (`dry_run` by default, `live`); a guarded `httpx`
  transport that refuses a host outside the calling client's allowlist and, under pytest, refuses any real network
  unless the contract run is active (`SKILLFORGE_CONTRACT_RUN=1`, set only by `just contract-sevdesk`); `live` is
  refused under pytest outside that run. Integration settings are nested in `Settings` and optional, so the app
  starts without them. `httpx` becomes a direct runtime dependency (decision Y). A nested
  `app/integrations/AGENTS.md`, with a `CLAUDE.md` that holds `@AGENTS.md`.
- _Acceptance criteria:_
  - [ ] A request to a host outside the allowlist is refused before a connection opens.
  - [ ] Under pytest a real connection is refused and `live` is refused, each unless the contract run is active.
  - [ ] Architecture test: no module under `app/` imports `httpx`, except the transport module.
  - [ ] The app starts, and `just check` passes, with no integration setting present.
  - [ ] `app/integrations/AGENTS.md` exists; the `CLAUDE.md` beside it holds only `@AGENTS.md`.

**SF1-2 - sevDesk client.**

- _Technique:_ a thin, hand-written async client over the guarded transport; base
  `https://my.sevdesk.de/api/v1`; the raw token in the header; typed, lenient parsing (sevDesk sends numbers as
  strings); the reads the doctor and the spike need; `saveInvoice` as a draft write that, in `dry_run`, returns the
  planned request without sending. A POST is never retried, and a transport error on a write surfaces as "outcome
  unknown". Before the first call the client checks the account's identity and fails closed: `SEVDESK__ACCOUNT`
  (`trial` or `live`), `SEVDESK__EXPECTED_ACCOUNT_ID`, and a trial's company name must contain `TRIAL`. Settings as
  in [Configuration](#configuration); account-specific ids are configuration, never literals. Which endpoint yields
  the identity is Q7.
- _Acceptance criteria:_
  - [ ] In `dry_run`, `saveInvoice` returns the planned request and the transport saw none.
  - [ ] A write whose connection fails raises the client's "outcome unknown" error and was sent exactly once.
  - [ ] No call leaves before the identity check passed; a wrong account id and a trial whose company name lacks
        `TRIAL` each fail closed.
  - [ ] Numbers that arrive as strings parse (`"100"`, `"100.00"`); an amount never passes through `float`.
  - [ ] The token appears in no log line, exception message or `repr`.

**SF1-3 - Contract tooling.**

- _Technique:_ a recorder that scrubs - the `Authorization` header, cookies, company and address data, IBAN, e-mail
  addresses to `example.org`, phone numbers - and writes fixtures under `tests/fixtures/sevdesk/`. A fixture-scan
  test in `just check`: token-like strings, an e-mail address outside `example.org`, IBAN patterns, the `TRIAL`
  company marker. `just contract-sevdesk`: loads `~/.config/skillforge/sevdesk-trial.env`, sets the contract run,
  runs `pytest -m contract`; never in CI; skips with "trial expired". `just sevdesk-doctor`: read-only, checks
  that every configured id exists, prints names and ids only, `--account trial|live`; the live run is the
  maintainer's, from his own terminal. The spike script: walks the [fixture manifest](#fixture-manifest) against
  the trial, never retries a write and tags every object it creates with a run id in `customerInternalNote`.
- _Acceptance criteria:_
  - [ ] The scan fails on each of its four patterns (one bad sample per rule, built in the test) and passes on the
        recorder's output for a response that held all four.
  - [ ] `pytest -m contract` is registered as a marker, deselected by `just check` and `just check-all`, and
        absent from CI.
  - [ ] `just contract-sevdesk` without the env file, or with an expired trial, ends with a skip message and
        exit code 0.
  - [ ] `just sevdesk-doctor` sends only reads and prints no contact data.
  - [ ] The spike script sends every write once and tags it with the run id.

**SP-SD - sevDesk spike and freeze.** _After SF1-3; the trial runs 14 days._

- _Technique:_ the maintainer creates the trial and its env file. The trial holds synthetic data only: a company
  name that contains `TRIAL`, § 19, a payment term of 14 days, the custom field "Zahlungsverfahren" with its two
  values and a public example IBAN. An agent runs the spike script through `just contract-sevdesk`, records the
  manifest and writes the answers into this spec. What only the real account can show - the raw templates, a
  top-up invoice of today, the ids - the maintainer reads himself (`just sevdesk-doctor --account live`) and passes
  on without personal data. Then the spec is frozen: open questions move to
  [Decided](#decided-formerly-open-questions), proposals PD1 to PD12 become decisions or are replaced, the status
  becomes `Accepted`, and so does ADR 0010's status line.
- _Acceptance criteria:_
  - [ ] Every row S1 to S24 has a scrubbed fixture, or a written reason why it could not be recorded; the scan is
        green.
  - [ ] Every created object carries the run id, and the script's log shows one request per write.
  - [ ] Q1 to Q8 and Q10 to Q15 are answered, each naming its fixture.
  - [ ] Trial and real account both report bookkeeping system 2.0.
  - [ ] Every **pending approval** in [Family-visible texts](#family-visible-texts) and every item of
        [For the maintainer](#for-the-maintainer) is approved or replaced by the maintainer, in the session.
  - [ ] The status block says `Accepted`, ADR 0010 says `Accepted`, and no criterion below rests on an open point.

**SF2-1 - Lesson and rate schema.**

- _Technique:_ the model package `app/core/db/models/billing/` (schema `billing`; `get_schemata` makes
  `migrations/env.py` create it) with `Lesson` and `Rate`; revision `0015`, enum types dropped on downgrade;
  `Scope.BILLING_READ` and `Scope.BILLING_WRITE`, both in `ROLE_SCOPES` for the admin role and not in
  `VOUCHED_SCOPES` (PD8); the billing tables that reference a party join `EXTERNAL_LINKS` in
  `app/services/crm/parties.py`, under the CRM's conventions; `BILLING__DEFAULT_RATE_CENTS_PER_HOUR`.
- _Tests that change:_ the admin role's scope set in `tests/auth/test_roles.py`; the delete guard's kinds in
  `tests/db/crm/test_crm_party_delete_api.py`.
- _Acceptance criteria:_
  - [ ] Upgrade, downgrade by one and upgrade again pass in `tests/db/test_migration_apply.py`.
  - [ ] The database refuses a rate not divisible by 4 and a lesson's minutes not divisible by 15
        (`ck_rate_quarter_hour_cents`, `ck_lesson_quarter_hour_minutes`).
  - [ ] Deleting a party that has a lesson or a rate answers `409 party_in_use`, not a 500.
  - [ ] A token from the Discord exchange never carries a billing scope.

**SF2-2 - Lessons.**

- _Technique:_ `app/services/billing/lessons.py` (function modules, `session` first, no commit - the CRM's shape),
  `app/api/v1/billing/` with router, `lessons.py`, `params.py`, `schemas.py`; the lesson routes of the
  [route map](#route-map); the statuses of [A lesson's life](#a-lessons-life); the billing architecture test
  (pattern: `tests/api/test_crm_architecture.py`) and error-contract test (pattern:
  `tests/api/test_crm_error_contract.py`).
- _Acceptance criteria:_
  - [ ] A tick sets `held` and `billable`; a `cancelled` lesson is billable only after the maintainer said so;
        `planned` and `not_held` are never billable (`ck_lesson_billable_status`).
  - [ ] Minutes not divisible by 15 are a `422`; a second lesson of one student at one start is
        `lesson_already_exists`.
  - [ ] The list filters by Berlin days: a lesson late in the evening belongs to its Berlin day, in summer and in
        winter time and in the nights of both clock changes.
  - [ ] No route deletes a lesson.
  - [ ] The error-contract test pins every route's errors.

**SF2-3 - Rates and demo data.**

- _Technique:_ `app/services/billing/rates.py` and the rate routes; `just seed-demo` (`app/cli/seed_demo.py`):
  synthetic families, students, payers, rates and lessons for local screens. `AGENTS.md` names the recipe among the
  commands that run only on a local database.
- _Acceptance criteria:_
  - [ ] Setting a rate twice for the same day changes nothing; a rate that breaks rule G is
        `rate_not_quarter_hour`.
  - [ ] Deleting a rate changes no stored charge (test from SF5-3 on).
  - [ ] `just seed-demo` writes synthetic data only, can run twice, and refuses a database that is not on
        `localhost`.

**SF3-1 - Payer and sevDesk contact.**

- _Technique:_ `resolve_payer` in a new `app/services/crm/payers.py` - a CRM read under the CRM's conventions,
  importing only `app.core` and the CRM. In billing: `app/services/billing/payers.py` stores the payer on a lesson
  when it is ticked (PD1), is the one writer of `ext.sevdesk_contact`, and keeps `billing.manual_payer` (PD5); the
  payer routes; one revision.
- _Tests that change:_ `NOT_AGGREGATE_WRITES` in `tests/db/crm/test_crm_write_services.py` gains `resolve_payer`.
- _Acceptance criteria:_
  - [ ] No `pays_for`: the student. One: that party, a company included. Two: ambiguous.
  - [ ] A ticked lesson keeps its payer when `pays_for` changes afterwards; a new tick resolves afresh.
  - [ ] Only `app/services/billing/payers.py` writes `ext.sevdesk_contact` (architecture test).
  - [ ] A contact already linked to another party, or a second contact for one party, is
        `sevdesk_contact_already_linked`.

**SF3-2 - Backfill.**

- _Technique:_ `just billing-backfill <file>` (`app/cli/billing_backfill.py`) reads a mapping CSV the maintainer
  confirmed - student, payer, sevDesk contact id, own rate with its day, billed by hand - from outside the
  repository and applies it through the services: `pays_for` through the CRM's relation service, the contact link,
  the rate, the marker. It is a data step: the maintainer runs it, an agent sees counts.
- _Acceptance criteria:_
  - [ ] Without `--apply` nothing is written; with it, a second run changes nothing.
  - [ ] One bad row - an unknown party, a rate against rule G, a second payer - rejects the whole file before the
        first write.
  - [ ] The output holds counts and row numbers, never a name or a contact id.
  - [ ] A path inside the repository is refused.

**SF4-1 - Calendar client.** _After SP-CAL._

- _Technique:_ a Graph client in `app/integrations/` over the guarded transport, with an allowlist of the Microsoft
  login host and the Graph host; a token for the app with this location's certificate
  ([M5](#for-the-maintainer)); `calendarView` of the configured calendar with the fields of G1, paged by
  `@odata.nextLink`, times in UTC. Before its first read the client resolves the configured calendar in the
  configured mailbox and refuses to work if it is missing.
- _Acceptance criteria:_
  - [ ] Every test runs on fixtures G1 to G7 through a mocked transport.
  - [ ] Paging reads to the end; a `429` is repeated as `Retry-After` says, a `403` is not.
  - [ ] Only the fields of G1 are requested.
  - [ ] Certificate, key and token appear in no log line or exception.

**SF4-2 - Title rules, matching, aliases.**

- _Technique:_ `app/services/billing/calendar_rules.py`, pure: the table and the matching of
  [Calendar import](#calendar-import); `billing.calendar_alias` with its revision and routes.
- _Acceptance criteria:_
  - [ ] One test per row of the table, on synthetic titles: a Teams lesson is kept; phone, all-day, `Kennenlernen`
        and a title without ` mit ` propose no lesson.
  - [ ] Matching: exact; the one student whose tokens contain the name's tokens; several candidates are ambiguous;
        an alias wins.
  - [ ] The module imports no session, no integration and reads no clock (architecture test).

**SF4-3 - Calendar import.**

- _Technique:_ `billing.calendar_event` with its revision; `import_calendar` in
  `app/services/billing/calendar.py`; the calendar routes.
- _Acceptance criteria:_
  - [ ] Importing a range twice changes no row.
  - [ ] A ticked or charged lesson is never changed or removed: a moved, shortened or vanished appointment sets
        `changed` (one test each).
  - [ ] A `planned` lesson nobody touched follows its appointment: moved, or removed.
  - [ ] A late cancellation becomes a `cancelled` lesson that is not billable (G3).
  - [ ] Lessons in the weeks of both clock changes land on the right Berlin day and time (G5).
  - [ ] The import answers with counts only.

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

- _Technique:_ `app/services/billing/texts.py` (the one home of German text), `money.py` (`amount_cents`,
  `quantity`), `builder.py` (`resolve_rate`, `position_description`, `payment_link`, `build_invoice` returning a
  frozen `PlannedInvoice`). A nested `app/services/billing/AGENTS.md` carries the
  [money rules](#money-rules) verbatim, with a `CLAUDE.md` that holds `@AGENTS.md`.
- _Tests that change:_ none - and no scenario or golden file.
- _Acceptance criteria:_
  - [ ] Every scenario of SF5-1 passes exactly.
  - [ ] `test_texts_match_the_spec`: every constant of `texts.py` equals its block in this spec.
  - [ ] Architecture tests: no `float` in `money.py` and `builder.py` (no float literal, no `float(`, no `/` on an
        amount); the builders import no session and no integration and read no clock.
  - [ ] For every duration from 15 to 600 minutes and every rate divisible by 4, `amount_cents` equals quantity
        times price.
  - [ ] Every scenario's link satisfies the `/zahlung` contract.
  - [ ] The PR names its mutation probes - the order of rate resolution, the first-name rule, the Berlin
        conversion - and the reviewing session ran them again.

**SF5-3 - Charges and preview.**

- _Technique:_ `Invoice` and `LessonCharge` with their revision; `route_lesson`; `GET /billable-lessons` and
  `POST /invoices/preview` in `app/services/billing/invoices.py`; the payment method read through the sevDesk
  client; `SEVDESK__PART_IDS`; `UpstreamError` (PD6); `LessonChargedError` on lesson edits.
- _Tests that change:_ `tests/api/test_error_taxonomy.py` and `tests/api/test_common_errors.py`, only where they
  pin the mapped categories, to add the fourth.
- _Acceptance criteria:_
  - [ ] A second active charge for one lesson violates `uq_lesson_charge_active`; a voided charge frees the lesson.
  - [ ] The preview changes no row. The same state gives the same fingerprint; a changed rate, duration, payer or
        payment method gives another.
  - [ ] Every blocker of the closed list is reported, several at once (one test per code).
  - [ ] Lessons of two payers are `mixed_payers`; sevDesk out of reach is `502 sevdesk_unavailable`.
  - [ ] Every open lesson appears in `GET /billable-lessons` exactly once.
  - [ ] A lesson with an active charge on an invoice cannot be edited (`lesson_charged`).

**SF6-1 - Create the draft.**

- _Technique:_ `POST /invoices` as in [Pricing, charges and invoices](#pricing-charges-and-invoices): fingerprint,
  pending row and charges, commit, one call, outcome in a second transaction, read-back; `GET /invoices...`.
- _Tests that change:_ none - and no scenario or golden file.
- _Acceptance criteria:_
  - [ ] The pending row and its charges are committed before the transport sees the request (a second session
        reads them inside the mocked call).
  - [ ] `saveInvoice` is sent exactly once per create: on success, on an error and on a lost connection.
  - [ ] A lost connection gives `unknown` and keeps the charges; an error answer gives `failed` and voids them.
  - [ ] Two creates for one lesson at the same time: one wins, the other is refused before any call.
  - [ ] An outdated fingerprint is `preview_outdated`: nothing stored, nothing sent.
  - [ ] The request equals each scenario's planned request (SF5-1).
  - [ ] The read-back sum equals the local total to the cent; a difference sets `drift` (S19).
  - [ ] In `dry_run` nothing is stored and nothing is sent (PD10).
  - [ ] Mutation probes: without the commit before the call, and with a retry, a named test fails.

**SF6-2 - Reconcile, refresh, cancel.**

- _Technique:_ the four invoice actions of the [route map](#route-map) and the transitions of
  [Invoice states](#invoice-states).
- _Acceptance criteria:_
  - [ ] Reconcile: reference found - `created`; not found - `failed`, charges voided; lookup failed - unchanged
        (S20).
  - [ ] Refresh never changes a charge - not on a `404`, a `401` or a `5xx` (one test each).
  - [ ] Refresh sets `drift` when positions or the sum differ, and names the difference (S16).
  - [ ] No route changes the charges or the snapshot of an issued invoice, except `mark-cancelled`.
  - [ ] `mark-cancelled` voids the charges and the lessons are open again; `discard` refuses an issued invoice.
  - [ ] The sevDesk client offers no call that sends, enshrines, cancels, books or deletes (architecture test).

**SF7 - Release and production.** _The maintainer's steps; agents write the runbook._

- _Technique:_ a section "Operating" in this spec: billing a lesson, what `unknown`, `failed` and `drift` mean and
  what to do, a new sevDesk token, a new certificate, the cut-over list. `compose.yml` passes the new settings, for
  the maintainer's review. The maintainer sets the production configuration, runs the doctor on the real account,
  ships the release and creates the first real draft himself.
- _Acceptance criteria:_
  - [ ] "Operating" exists and names every state a draft can be left in.
  - [ ] `.env.example` lists every setting of [Configuration](#configuration) without a real value.
  - [ ] The maintainer confirmed: the production database has moved, the configuration is set, the doctor is green
        on the real account, `/health` reports the released version.
  - [ ] The maintainer created the first real draft and checked it in sevDesk: texts, link, GiroCode, sum.

**SF-CLI - Command-line stage.** _After SF7, before the admin screens._

- _Technique:_ `just billing-drafts` (`scripts/billing_cli.py`), a client of the billing API with a person's token
  (PD11): it imports the calendar for a range, applies a correction file - ticks, cancellations made billable,
  corrected minutes, lessons added by hand - through the lesson routes, prints the preview of the open lessons and,
  with `--create`, creates the drafts: one per lesson, or one per group the file names.
- _Acceptance criteria:_
  - [ ] It uses only routes of the [route map](#route-map) and the token endpoint; it imports nothing from `app/`.
  - [ ] Without `--create` it creates nothing. With it, a create is sent once; `unknown` is printed with the hint to
        reconcile.
  - [ ] A correction row that matches no lesson stops the run before the first change.
  - [ ] Password and client secret come from a prompt or the environment, never from an argument, and are never
        printed.
  - [ ] A test runs it end to end against the app in-process with demo data and `SEVDESK__MODE=dry_run`.

**SFP-1a - Credit accounts and routing.** _After SF6-2. The slices before it hold no prepaid code._

- _Technique:_ `PrepaidAccount` and `PrepaidEntry`; `lesson_charge` gains `prepaid_account_id` and
  `ck_lesson_charge_target`; `route_lesson` answers `prepaid`; `settle_prepaid` at the end of every lesson write
  service, pinned by a `WRITES` registry (`tests/db/billing/test_billing_write_services.py`, pattern:
  `tests/db/crm/test_crm_write_services.py`); the account routes and `settle`; one revision.
- _Tests that change:_ the preview and `GET /billable-lessons` tests of SF5-3 gain the route `prepaid`.
- _Acceptance criteria:_
  - [ ] Every public write coroutine of `app/services/billing/lessons.py` has a scenario in `WRITES` and ends with
        `settle_prepaid`.
  - [ ] A tick on a prepaid lesson creates one charge at the child's frozen rate; two ticks at once create one.
  - [ ] A tick that races a close either lands before it or routes to the invoice (DB test).
  - [ ] Corrections follow [Credit accounts](#credit-accounts): minutes only, day or student, tick removed.
  - [ ] A prepaid lesson is never in an invoice preview (`prepaid_payer`), and no lesson has a charge on both
        paths.
  - [ ] A tick is never refused for lack of credit.
  - [ ] One open account per payer; a new one starts on or after the last close.

**SFP-1b - Statement, manual entries, close.**

- _Technique:_ the statement query (a running sum in SQL), the entry routes, `close`; `just billing-backfill`
  learns the prepaid columns (account, default top-up) and no opening balance.
- _Tests that change:_ none. The statement scenarios land in their own PR first.
- _Acceptance criteria:_
  - [ ] The balance equals a recomputation in Python over synthetic entries and charges; no column stores it.
  - [ ] The statement's order and sections match the golden statements, a negative carry-over included.
  - [ ] Voided rows appear only on request.
  - [ ] Close succeeds at balance 0 and is `prepaid_account_not_settled` otherwise.
  - [ ] After a close, later lessons and lessons found inside the closed window go to the invoice.
  - [ ] A lesson's charge cannot be voided through the entry routes (`prepaid_entry_not_voidable`).

**SFP-2a - Top-up invoices.**

- _Technique:_ `invoice.kind` and `invoice.prepaid_account_id`; the top-up preview and create on the path of SF5-3
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
  - [ ] Status 750 and 1000 show as a field; a top-up unpaid for more than 14 days is flagged.

### Nice-to-have (P1)

- **SF8 - No GiroCode on PayPal invoices.** Only if Q4 shows that the API can switch it off per invoice.
- **SFP-3 - "Record as sent".** Only if Q13 shows that the maintainer's way of sending leaves the status at 100:
  an action that posts a top-up's credit and still reads the amount from sevDesk.

### Future considerations (P2)

v1.1 and later, none of it designed here: the statement for families (text, PDF, mail, the portal with
`billing:read:own`); detecting payments and Storno; a low-balance forecast; reopening an account; refunding credit
and the Storno path when closing; hour packs; prepaid and postpaid under one payer; an overdraft limit; a mail text
from SkillForge; e-invoices for authorities; bookings made in the portal so that names always match; a tutor per
lesson.

## Decided (formerly open questions)

None yet. SP-CAL and SP-SD move each answered question here, with the fixture that shows it.

## Open questions

### For the spikes

| Id | Question | Decides | Answered by |
|---|---|---|---|
| Q1 | Which `invoiceNumber` does the API return for a draft, for a second draft, and after issue? In the UI every open draft shows the next free number. | How the PayPal link gets its number. | SP-SD (S8, S22) |
| Q2 | Which placeholders do today's templates use - for the salutation, the due date, the contact person, and in the PayPal link's target for number and amount? Do they resolve in a head or foot text set through the API, inside a link target too? | Whether texts carry placeholders or filled-in values; the head text. | SP-SD (S10, S15); the raw templates from the maintainer |
| Q3 | If SkillForge sets a draft's number from sevDesk's sequence (decision R), does sevDesk keep it when the draft is opened and issued? | Whether the fallback works at all. | SP-SD (S23) |
| Q4 | Do GiroCode and bank block render under an own foot text? Can the GiroCode be switched off per invoice through the API? | SF8. | SP-SD (S14) |
| Q5 | The part per subject: ids and fields (name, unit, price). Does a position created with `part` and no `name` show the part's name? | `SEVDESK__PART_IDS`; the request. | SP-SD (S9, S11); the real ids from the doctor |
| Q6 | The ids of the unit `Std` and of the contact person, and where they are read. | Configuration. | SP-SD (S6, S7) |
| Q7 | Which read yields the account's identity and company name? | The identity check of SF1-2. | SP-SD (S2) |
| Q8 | Does the trial run bookkeeping system 2.0 with the functions of "Buchhaltung Pro", like the real account? | Whether trial fixtures speak for the real account. | SP-SD (S1), the doctor on both accounts |
| Q9 | The exact name of the Outlook category for a late cancellation, and whether Graph delivers `categories` for the lesson calendar. | The cancelled row of [Calendar import](#calendar-import). | SP-CAL (G3) |
| Q10 | The real name and matching of the `customerInternalNote` filter; otherwise the list of a contact's drafts by creation time. | Reconcile. | SP-SD (S20) |
| Q11 | Does `DELETE` work on a draft although it is not documented? | Whether `discard` could ever clean up sevDesk; v1 does not rely on it. | SP-SD (S18) |
| Q12 | How does a cancelled invoice read: the original's status, the cancellation invoice's shape? | `mark-cancelled`; v1.1 detection. | SP-SD (S17) |
| Q13 | Does the maintainer's way of sending move an invoice to status 200? | Credit at issue; SFP-3. | SP-SD (S13) |
| Q14 | How are amounts typed in responses, and what do error bodies look like? | The client's parsing. | SP-SD (S19, S21) |
| Q15 | How does sevDesk read the time zone of `deliveryDate`? Which delivery date does a top-up carry today? | The request's dates; `delivery_from` on a top-up. | SP-SD (S24); a top-up invoice of today, read by the maintainer |
| Q16 | Which Graph id stays the same when an occurrence of a series moves? | `calendar_event.external_id`. | SP-CAL (G2) |

### For the maintainer

| Id | Point | Proposal |
|---|---|---|
| M1 | The foot text SkillForge sets: the first line is decided; the rest is **pending approval**. | Keep today's wording word for word (`FOOT_TRANSFER`, `FOOT_PAYPAL`). |
| M2 | The sentence about credit on a top-up, and where it stands: **pending approval**. | `TOPUP_CREDIT_SENTENCE`, before `Bei Rückfragen …`. |
| M3 | The head text: its raw template with the placeholder is missing. A top-up's head text is **pending approval**. | The maintainer copies the template; a top-up uses the same head text. |
| M4 | The description of a top-up position: **pending approval**. | Empty; `<Vorname>` when the top-up holds several children. |
| M5 | How SkillForge gets its Graph token with a certificate. Signing the request needs `cryptography`, a new runtime dependency; Microsoft's own library (`msal`) brings its own HTTP client, past the guarded transport. | The token request goes through the guarded transport, signed with `pyjwt[crypto]`; the dependency is approved with the freeze. |
| M6 | A remainder top-up whose amount is not a whole number of hours at the child's rate. | A position with a quantity in quarter hours on the subject's part; the wording as for any top-up. |

Proposals PD1 to PD12 above wait for the same review.

### Recorded, not resolved

These are observations, not legal advice, and no slice depends on them.

- **L1 - Terms and prepaid.** The terms and conditions on the website speak of billing after the lesson and of
  paying back amounts for lessons that did not take place. A top-up in advance, credit that stays when the
  maintainer cancels, and "no refund in v1" do not quite match that wording.
- **L2 - E-invoices for authorities.** Whether an authority that pays for a student (BuT) requires an e-invoice
  (XRechnung) is unclear. v1 bills those lessons by hand (decision V).

## Timeline / phasing

No slice waits for a merge: dependent PRs form a stack ([`AGENTS.md`](../../AGENTS.md)).

| Slice | Needs | Note |
|---|---|---|
| **SF0** | - | |
| **SF1-1**, **SF1-2**, **SF1-3** | SF0, each the one before | One stack. |
| **SP-CAL** | SF0 | The scripts can start at once; its fixture PR needs the scan of SF1-3. |
| **SP-SD** | SF1-3 | The maintainer creates the trial only when SF1-3 is merged, so its 14 days are not spent waiting. Freezes the spec. |
| **SF2-1**, **SF2-2**, **SF2-3** | SF0, each the one before | Rest on no open point; can run beside SF1. |
| **SF3-1**, **SF3-2** | SF2-2 | |
| **SF4-1** | SP-CAL, SF1-1, M5 | |
| **SF4-2** | SF2-1, Q9 | |
| **SF4-3** | SF4-1, SF4-2, SF2-2 | |
| **SF5-1** | the freeze | The check of PR bodies (SF-C, repository tooling) is in place before it. |
| **SF5-2**, **SF5-3** | SF5-1; SF5-3 also SF3-1 and SF1-2 | |
| **SF6-1**, **SF6-2** | SF5-3 | |
| **SF7** | SF6-2, SF4-3 | The release that carries the slices so far. |
| **SF-CLI** | SF7 | The maintainer can bill from here on; the admin screens follow. |
| **SFP-1a**, **SFP-1b**, **SFP-2a**, **SFP-2b** | SF6-2, each the one before | Their scenarios land in their own PRs first. |

- **Before the freeze** only SF1, SF2, SF3 and the scripts of SP-CAL are built. SF4 waits for SP-CAL; SF5, SF6
  and SFP-2 wait for the freeze.
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
| 1 | Money is `int` cents. No `float` near an amount; an amount from sevDesk goes through `Decimal` into cents once. | Architecture test (SF5-2); parsing tests (SF1-2). |
| 2 | The rate is frozen on each position at the preview. A later rate change touches no charge. | Fingerprint and charge tests (SF5-3, SF2-3). |
| 3 | Quarter-hour rule: cents per hour divisible by 4, minutes by 15. There is no rounding code. Needing to round means stop. | `ck_rate_quarter_hour_cents`, `ck_lesson_quarter_hour_minutes` (SF2-1); the exactness test (SF5-2). |
| 4 | Issued is immutable. An issued invoice's charges and snapshot change only through `mark-cancelled`. | Route tests (SF6-2). |
| 5 | One active charge per lesson, on whatever path. A correction voids and rebooks; it never deletes a charge. | `uq_lesson_charge_active` (SF5-3, SFP-1a). |
| 6 | No stored balance. A balance is a sum of active entries minus active charges. | Statement test (SFP-1b). |
| 7 | Builders are pure: no session, no network, no clock. | Architecture test (SF5-2, SF4-2). |
| 8 | A pending row is committed before the call. A POST is never retried. `unknown` is resolved only by a reconcile. | Create tests and their mutation probes (SF6-1). |
| 9 | Times are stored in UTC and billed in `Europe/Berlin`, daylight saving included. | Tests in the weeks of both clock changes (SF2-2, SF4-3, SF5-1). |
| 10 | After a create, the sum read back equals the local sum to the cent - or the invoice is flagged. | Read-back test (SF6-1). |
| 11 | Scenarios and golden fixtures change only in their own PR, never together with money logic. A recorded fixture is recorded again, never edited. | The PR check (SF-C); review. |
| 12 | The reviewer runs the mutation probes himself: the session that reviews a money PR breaks each named rule again and sees the named test fail. | The review's conclusion in the PR. |
| 13 | A sevDesk read error never changes money. | Refresh tests (SF6-2, SFP-2b). |
| 14 | A lesson with charge history is never hard-deleted. | No delete route (SF2-2); import tests (SF4-3); `ON DELETE RESTRICT` (SF5-3). |

A money PR is accepted when the signed-off scenarios pass exactly, its mutation probes are ticked, and the
independent review has no open finding.

### Never

- Call a live system: sevDesk, Microsoft Graph, the production database. Only `just contract-sevdesk` reaches the
  trial.
- Run `scripts/graph/exchange-rbac-calendar.ps1` or `scripts/graph/record_calendar.py` against Microsoft, or
  `just sevdesk-doctor --account live`: they are the maintainer's.
- Read an env file, or put a token, a certificate, a tenant id, an account id or a sevDesk id into the
  repository.
- Use a real name, address or e-mail anywhere. Families are "family A", students "student B", addresses end in
  `example.org`.
- Send, enshrine, cancel, book or delete in sevDesk, or invent an invoice number.
- Create a test object in the real account.
- Write German text outside `app/services/billing/texts.py`, or change a text of
  [Family-visible texts](#family-visible-texts) without the maintainer's approval in the session.
- Log a name, a calendar title, a contact id or a token.

### Stop and report

- A slice needs a point that is **open** or **pending approval**.
- A requirement seems to need a new error class, blocker code, scope, table, route or runtime dependency.
- This spec and the code contradict each other in a money path.
- A fixture, an output or a file shows real personal data, or a prompt asks for a credential.
- A recorded response differs from what a slice assumed: record it again, show the diff, change no test logic
  silently.

### Review

Every PR of this arc is `Review: deep`, except the scripts and tooling of SP-CAL and SF1-3 and the runbook of SF7,
which are `evidence`. This spec is `deep`: it defines what families see.

