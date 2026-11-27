"""Read-only check of the sevDesk configuration: `just sevdesk-doctor trial|live [--discover | --invoice-number N ...]`.

It reads the settings from the process environment only (``SEVDESK__*``; no ``.env``): the recipe loads the trial
env file for ``trial``, the maintainer's own terminal provides them for ``live``. It always runs in ``dry_run``, so
nothing it could call is sent as a write, and never in CI. It prints ids and the hour unit's name - never a
person's data, nor the company name: ``--discover`` sends the three reads of spike row S2 (``GET /SevClient``,
``GET /SevUser``, ``GET /ContactCustomFieldSetting``) and prints the ``sevClient`` ids they report and whether
the company name carries the trial marker, so the maintainer can fill ``SEVDESK__EXPECTED_ACCOUNT_ID`` on the
first run. It is the one path that reads before the account is confirmed.

``--invoice-number`` (repeatable) answers Q19 of the billing spec: after the identity check it reads ``GET /Invoice``
with the documented ``invoiceNumber`` filter, per number, and prints how many invoices came back and, for each,
whether its number matches exactly, its type and its status - never a name, a contact, a customer number or an
amount. A number passes when every listed invoice carries it exactly. A number nothing lists is neither a pass nor a
failure: an ``info`` line, left out of the exit code, says that only a number no invoice carries should show it -
for an existing number it means the filter is wrong.
"""

import argparse
import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.logging import LoggingSettings, LogLevel, configure_logging
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import IdentityCheckError, IntegrationError
from app.integrations.sevdesk.client import SevdeskClient
from app.integrations.sevdesk.config import SevdeskSettings
from app.integrations.sevdesk.models import Invoice, SevUser, Unity
from app.integrations.transport import InnerTransport
from app.services.billing.payment_requests import INVOICE_NUMBER_PATTERN

EXPECTED_BOOKKEEPING_VERSION = "2.0"


class Outcome(StrEnum):
    """A check's result; only ``FAIL`` fails the run, ``INFO`` reports without passing or failing."""

    OK = "ok"
    FAIL = "FAIL"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Check:
    label: str
    outcome: Outcome
    detail: str

    @classmethod
    def of(cls, label: str, ok: bool, detail: str) -> Check:
        return cls(label, Outcome.OK if ok else Outcome.FAIL, detail)

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL

    def __str__(self) -> str:
        return f"{self.outcome:<4} {self.label}: {self.detail}"


async def configured_ids(client: SevdeskClient, settings: SevdeskSettings) -> list[Check]:
    """Whether the bookkeeping system is 2.0 and every configured id exists in the account."""
    version = await client.bookkeeping_version()
    return [
        Check.of("bookkeeping system", version == EXPECTED_BOOKKEEPING_VERSION, version),
        await _exists("contact person (SevUser)", "CONTACT_PERSON_ID", settings.contact_person_id, client.get_user),
        await _exists("hour unit (Unity)", "HOUR_UNIT_ID", settings.hour_unit_id, client.get_unity),
    ]


async def discover(settings: SevdeskSettings, *, transport: InnerTransport | None = None) -> list[Check]:
    """The ``sevClient`` ids the account reports - without the identity check, which needs one of them."""
    try:
        found = await SevdeskClient.discover(settings, transport=transport)
    except IntegrationError as exc:
        return [Check.of("discover", False, str(exc))]
    return [
        Check.of(
            "GET /SevClient", True, f"id={found.account_id} TRIAL marker: {'yes' if found.trial_marker else 'no'}"
        ),
        Check.of("GET /SevUser", True, f"sevClient ids {_ids(found.user_account_ids)}"),
        Check.of("GET /ContactCustomFieldSetting", True, f"sevClient ids {_ids(found.field_setting_account_ids)}"),
    ]


async def invoice_numbers(client: SevdeskClient, numbers: Sequence[str]) -> list[Check]:
    """What ``GET /Invoice`` lists under each number: the count and, per invoice, the exact match, type and status."""
    return [await _invoice_number(client, number) for number in numbers]


async def _invoice_number(client: SevdeskClient, number: str) -> Check:
    label = f"invoice number {number}"
    try:
        listed = await client.invoices_by_number(number)
    except IntegrationError as exc:
        return Check.of(label, False, str(exc))
    count = f"{len(listed)} listed"
    if not listed:
        return Check(label, Outcome.INFO, f"{count} - expected only for a number no invoice carries")
    found = "; ".join(_listed(invoice, number) for invoice in listed)
    return Check.of(label, all(invoice.invoice_number == number for invoice in listed), f"{count} - {found}")


def _listed(invoice: Invoice, number: str) -> str:
    match = "yes" if invoice.invoice_number == number else "no"
    return f"exact match {match}, type {invoice.invoice_type or 'none'}, status {invoice.status}"


async def doctor(
    settings: SevdeskSettings, *, numbers: Sequence[str] = (), transport: InnerTransport | None = None
) -> list[Check]:
    """The identity check, then the configured ids - or, given invoice numbers, their listings instead."""
    try:
        async with SevdeskClient.connect(settings, transport=transport) as client:
            identity = Check.of("account", True, f"{settings.expected_account_id} ({settings.account.value})")
            checks = await invoice_numbers(client, numbers) if numbers else await configured_ids(client, settings)
            return [identity, *checks]
    except IdentityCheckError as exc:
        return [Check.of("account", False, str(exc))]
    except IntegrationError as exc:
        return [Check.of("sevDesk", False, str(exc))]


async def run(
    account: IntegrationAccount,
    *,
    discover_accounts: bool,
    invoice_numbers: Sequence[str] = (),
    settings: SevdeskSettings | None = None,
    transport: InnerTransport | None = None,
    out: Callable[[str], None] = print,
) -> int:
    settings = settings or SevdeskSettings(_env_file=None)
    if settings.account is not account:
        out(f"SEVDESK__ACCOUNT is {settings.account.value}, not {account.value}: refusing")
        return 1
    settings = settings.model_copy(update={"mode": IntegrationMode.DRY_RUN})
    if discover_accounts:
        checks = await discover(settings, transport=transport)
    else:
        checks = await doctor(settings, numbers=invoice_numbers, transport=transport)
    for check in checks:
        out(str(check))
    return 1 if any(check.failed for check in checks) else 0


async def _exists[T: SevUser | Unity](
    label: str, variable: str, object_id: int | None, find: Callable[[int], Awaitable[T | None]]
) -> Check:
    if object_id is None:
        return Check.of(label, False, f"SEVDESK__{variable} is not set")
    match await find(object_id):
        case None:
            return Check.of(label, False, f"{object_id} does not exist")
        case Unity(name=name):
            return Check.of(label, True, f"{object_id} ({name})")
    return Check.of(label, True, str(object_id))


def _ids(ids: tuple[int, ...]) -> str:
    return ", ".join(map(str, ids)) or "none"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.sevdesk_doctor", description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--account", type=IntegrationAccount, choices=list(IntegrationAccount), required=True, help="trial or live"
    )
    reads = parser.add_mutually_exclusive_group()
    reads.add_argument(
        "--discover", action="store_true", help="Print the sevClient ids the token's account reports, then stop."
    )
    reads.add_argument(
        "--invoice-number",
        dest="invoice_numbers",
        action="append",
        default=[],
        type=_invoice_number_argument,
        metavar="NUMBER",
        help="After the identity check, list the invoices GET /Invoice finds with this invoiceNumber filter: count, "
        "exact match, type and status. Repeatable.",
    )
    return parser


def _invoice_number_argument(value: str) -> str:
    if not re.fullmatch(INVOICE_NUMBER_PATTERN, value):
        raise argparse.ArgumentTypeError(f"an invoice number matches {INVOICE_NUMBER_PATTERN}")
    return value


def main() -> None:
    if os.environ.get("CI"):
        raise SystemExit("sevdesk-doctor never runs in CI")
    arguments = build_parser().parse_args()
    configure_logging(LoggingSettings(level=LogLevel.WARNING))
    raise SystemExit(
        asyncio.run(
            run(arguments.account, discover_accounts=arguments.discover, invoice_numbers=arguments.invoice_numbers)
        )
    )


if __name__ == "__main__":
    main()
