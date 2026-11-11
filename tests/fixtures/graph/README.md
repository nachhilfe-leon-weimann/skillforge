# Graph fixtures (synthetic)

G1 to G7 of the [fixture manifest](../../../docs/specs/billing.md#fixture-manifest). Unlike the sevDesk fixtures,
nothing here was recorded: nothing read from the lesson calendar enters the repository. An agent built these files
in the shape of the maintainer's redacted SP-CAL summary (2026-11, 125 events over 70 days); names, ids, dates and
the tenant are invented. They use the recorder's record format (`request`, `response`) and pass
`tests/test_fixture_scan.py`. A changed shape is rebuilt from a new summary, not patched.

What the summary showed:

- Graph delivers `categories` (50 events had one); the late-cancellation category is `Abgesagt` (Q9).
  `isCancelled` was never true, no event was all-day, and `showAs` was always `busy` - G4's all-day entry is there
  because the import must handle one.
- Teams lessons have `isOnlineMeeting: true`, `onlineMeetingProvider: "teamsForBusiness"` and a location named
  after Teams; a "Teams" category only colours them. Two events had a phone number as location; the maintainer
  files phone appointments under a "Telefon" category.
- Durations were 15, 30, 45, 60 and 120 minutes.

How to read them:

- Each file is a scenario of its own; their windows overlap. Do not chain them as successive reads of one
  calendar, or lessons seem to vanish - only G2's two files are one range read twice.
- Times are UTC with seven fractional digits. The record format keeps no `Prefer` header: a test that needs the
  time zone the import asks for checks it on the outgoing request.
- Some entries were not in the run and are there because the import must handle them: G4's all-day entry (its
  shape follows the docs; key on `isAllDay`), G1's title in lower case, G3's event with two categories.
- `originalStart` is missing on G1's single instances, as documented, and `null` on the others: treat both alike.
- G4's phone number has eight digits only to stay under the fixture scan's rule; it sets no length rule.
- G7's request body is the form the token endpoint takes, recorded as a mapping; its token and assertion are
  synthetic strings a log test can search for.
- `owner` on G1's calendars was not in the run: it was added for the identity check of SF4-1 in Graph's documented
  `emailAddress` shape, with a synthetic name and address.

| File | Fixture | Shows |
|---|---|---|
| `g1-calendars.json` | G1 | The mailbox's calendars with `id`, `name` and `owner`: the identity read, which finds the lesson calendar by name and compares its owner with the mailbox. The recorder's `find_calendar_id` read `id,name` only; `owner` follows Graph's documented `emailAddress` shape (`name`, `address`). |
| `g1-calendar-view-page-1.json`, `g1-calendar-view-page-2.json` | G1 | A `calendarView` read with `$select` (the recorder's `EVENT_FIELDS`) in two pages: the first carries `@odata.nextLink`, the second none. |
| `g2-series-first-read.json`, `g2-series-second-read.json` | G2 | The same range read twice; in between the occurrence of 2026-12-01 moved to 2026-12-03 and became an `exception`. `id`, `iCalUId` and `seriesMasterId` stay; `originalStart` keeps the planned time (Q16). |
| `g3-late-cancellation.json` | G3 | A single lesson and an occurrence with the category `Abgesagt`; the occurrence became an `exception` by it. |
| `g4-special-entries.json` | G4 | A Teams lesson, a phone appointment, an all-day entry, a `Kennenlernen` entry, a title without ` mit `. |
| `g5-spring-clock-change.json`, `g5-autumn-clock-change.json` | G5 | A weekly lesson across both clock changes of 2026 (same Berlin time, other UTC hour) and lessons at 00:30 and 23:30 Berlin time on the day of the change. |
| `g6-mailbox-outside-scope.json` | G6 | `403` for a mailbox outside the restriction. The run saw the status; the code and message are Graph's documented error shape. |
| `g7-token-response.json`, `g7-token-error.json` | G7 | The token endpoint's answer for the client-credentials grant with a certificate, and a refused assertion (`AADSTS700027`). The recorder reads tokens through MSAL and records neither; both follow the documented shape. |
