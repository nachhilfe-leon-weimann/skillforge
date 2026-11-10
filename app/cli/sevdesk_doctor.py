"""Read-only check of the sevDesk configuration: `just sevdesk-doctor --account trial|live [--discover]`.

It reads the settings from the process environment only (``SEVDESK__*``; no ``.env``): the recipe loads the trial
env file for ``--account trial``, the maintainer's own terminal provides it for ``--account live``. It always
runs in ``dry_run``, so nothing it could call is sent as a write. It prints ids and the names of configuration
objects (units, custom fields) - never a person's data; only ``--discover`` prints the account's company name,
so the maintainer can fill ``SEVDESK__EXPECTED_ACCOUNT_ID`` on the first run.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.core.logging import LoggingSettings, LogLevel, configure_logging
from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import IdentityCheckError, IntegrationError
from app.integrations.sevdesk.client import SevdeskClient, sevdesk_http
from app.integrations.sevdesk.config import SevdeskSettings
from app.integrations.sevdesk.models import CustomFieldSetting, SevUser, Unity
from app.integrations.transport import InnerTransport

EXPECTED_BOOKKEEPING_VERSION = "2.0"


@dataclass(frozen=True, slots=True)
class Check:
    label: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"{'ok  ' if self.ok else 'FAIL'} {self.label}: {self.detail}"


async def configured_ids(client: SevdeskClient, settings: SevdeskSettings) -> list[Check]:
    """Whether the bookkeeping system is 2.0 and every configured id exists in the account."""
    version = await client.bookkeeping_version()
    return [
        Check("bookkeeping system", version == EXPECTED_BOOKKEEPING_VERSION, version),
        await _exists("contact person (SevUser)", "CONTACT_PERSON_ID", settings.contact_person_id, client.get_user),
        await _exists("hour unit (Unity)", "HOUR_UNIT_ID", settings.hour_unit_id, client.get_unity),
        await _exists(
            "payment method (ContactCustomFieldSetting)",
            "PAYMENT_METHOD_FIELD_SETTING_ID",
            settings.payment_method_field_setting_id,
            client.get_custom_field_setting,
        ),
    ]


async def discover(settings: SevdeskSettings, *, transport: InnerTransport | None = None) -> list[str]:
    """The account id as each candidate endpoint reports it - without the identity check, which needs it."""
    lines: list[str] = []
    async with sevdesk_http(settings, transport=transport) as http:
        client = SevdeskClient(http, settings)
        for endpoint, read in (
            ("GET /SevClient", _from_accounts),
            ("GET /SevUser", _from_users),
            ("GET /ContactCustomFieldSetting", _from_field_settings),
        ):
            try:
                found = await read(client)
            except IntegrationError as exc:
                found = [f"unavailable ({exc})"]
            lines.extend(f"{endpoint}: {item}" for item in found or ["nothing found"])
    return lines


async def doctor(settings: SevdeskSettings, *, transport: InnerTransport | None = None) -> list[Check]:
    try:
        async with SevdeskClient.connect(settings, transport=transport) as client:
            identity = Check("account", True, f"{settings.expected_account_id} ({settings.account.value})")
            return [identity, *await configured_ids(client, settings)]
    except IdentityCheckError as exc:
        return [Check("account", False, str(exc))]
    except IntegrationError as exc:
        return [Check("sevDesk", False, str(exc))]


async def run(
    account: IntegrationAccount,
    *,
    discover_accounts: bool,
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
        for line in await discover(settings, transport=transport):
            out(line)
        return 0
    checks = await doctor(settings, transport=transport)
    for check in checks:
        out(str(check))
    return 0 if all(check.ok for check in checks) else 1


async def _exists[T: SevUser | Unity | CustomFieldSetting](
    label: str, variable: str, object_id: int | None, find: Callable[[int], Awaitable[T | None]]
) -> Check:
    if object_id is None:
        return Check(label, False, f"SEVDESK__{variable} is not set")
    match await find(object_id):
        case None:
            return Check(label, False, f"{object_id} does not exist")
        case Unity(name=name) | CustomFieldSetting(name=name):
            return Check(label, True, f"{object_id} ({name})")
    return Check(label, True, str(object_id))


async def _from_accounts(client: SevdeskClient) -> list[str]:
    account = await client.account()
    return [f"id={account.id} company={account.name!r}"]


async def _from_users(client: SevdeskClient) -> list[str]:
    return _account_refs(await client.users())


async def _from_field_settings(client: SevdeskClient) -> list[str]:
    return _account_refs(await client.custom_field_settings())


def _account_refs(items: Sequence[SevUser | CustomFieldSetting]) -> list[str]:
    ids = sorted({item.sev_client.id for item in items if item.sev_client is not None})
    return [f"sevClient id={account_id}" for account_id in ids]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.sevdesk_doctor", description=__doc__)
    parser.add_argument(
        "--account", type=IntegrationAccount, choices=list(IntegrationAccount), required=True, help="trial or live"
    )
    parser.add_argument(
        "--discover", action="store_true", help="Print the account id each candidate endpoint reports, then stop."
    )
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    configure_logging(LoggingSettings(level=LogLevel.WARNING))
    raise SystemExit(asyncio.run(run(arguments.account, discover_accounts=arguments.discover)))


if __name__ == "__main__":
    main()
