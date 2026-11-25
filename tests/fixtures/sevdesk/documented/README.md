# sevDesk fixtures derived from the documentation (not recorded)

Nothing here was recorded: these files are derived from sevDesk's API documentation and kept apart from every
recorded run, as the [fixture manifest](../../../../docs/specs/billing.md#fixture-manifest) asks for SF-PAY. Each
file says so in its `source` key. They use the recorder's record format (`request`, `response`) and pass
`tests/test_fixture_scan.py`. Whether the real account answers as documented, the maintainer's read-only
`just sevdesk-doctor live --invoice-number ...` shows before the release that carries SF-PAY (Q19); a mismatch
replaces these files, it never patches them.

| File | Shows |
|---|---|
| `invoice-by-number.json` | `GET /Invoice` filtered by `invoiceNumber`, as documented, with the paging parameters the client sends (`limit`, `offset`, `countAll`), for a number no invoice carries: `total` `"0"` and no object. |

How to read them:

- The envelope - `total` as a string beside `objects` - is the one the recorded `GET /Invoice` listing shows
  (`run202611112124164167/deleted-read-187.json`). A test that needs a number with invoices fills `objects` with
  invoice objects of the recorded run and `total` with their count; the invoices themselves are never written
  here.
- The request's `invoiceNumber` is the parameter name the documentation gives; its value is synthetic.
