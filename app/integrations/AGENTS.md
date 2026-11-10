# app/integrations

The rules for the only package that calls other systems; the repository's [`AGENTS.md`](../../AGENTS.md) applies
too. The layer is described in [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md#integrations).

- **One way out.** Every request goes through `IntegrationHttp` in `transport.py`, the only module in `app/` that
  imports `httpx` (`tests/integrations/test_integrations_architecture.py`). A client brings its base URL and host
  allowlist and never builds its own HTTP client.
- **`dry_run` is the default.** In `dry_run` reads go out and writes do not: `write` returns the `PlannedRequest`.
  Under pytest real network and `live` are refused, except in the contract run (`SKILLFORGE_CONTRACT_RUN=1`, set
  only by `just contract-sevdesk`) - and there `live` only for a `trial` account. Tests use `httpx.MockTransport`
  and synthetic data.
- **Never retry a write.** A write that may have reached the remote system is an `OutcomeUnknownError`; the calling
  service records it for a later reconcile. Calls run synchronously from services - no job queue.
- **Errors stay internal.** Integration errors (`errors.py`) are no `DomainError`; the calling service decides what
  an API client sees. No message, log line or `repr` carries a credential, a header, a query, a body or an id:
  a path is named by its template (`path_template`).
- **Imports.** This package imports only `app.core` and itself; `app/core/` imports nothing from it but the
  settings `app/core/config.py` nests (`tests/integrations/test_integrations_architecture.py`).
- **Ids are configuration.** Account-specific ids (contact person, units, custom fields) come from settings, never
  from literals in code.
- **Live calls are the maintainer's.** `just sevdesk-doctor` and `just contract-sevdesk` (`@pytest.mark.contract`,
  skipped in every other run) reach sevDesk; agents write and mock-test them, never run them.
