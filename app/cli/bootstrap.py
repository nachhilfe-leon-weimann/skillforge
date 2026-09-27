"""Operator commands that seed the auth state: `just bootstrap-client` and `just bootstrap-admin`.

They print a client secret or a one-time token they created to stdout and nowhere else: the operator
reads it from the terminal and passes it on. It must never go through the logger.
"""

import argparse
import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.inputs import LoginEmail
from app.core.auth.scopes import format_scopes, parse_scopes
from app.core.config import get_settings
from app.core.db import Database
from app.core.db.models import GrantMode
from app.core.errors import DomainError
from app.services.auth import InvalidClientScopeError
from app.services.auth.bootstrap import bootstrap_admin_account, bootstrap_application_client
from app.services.auth.results import BootstrappedApplicationClient, CreatedClientSecret

_LOGIN_EMAIL: TypeAdapter[str] = TypeAdapter(LoginEmail)


async def bootstrap_client(client_id: str, *, application: frozenset[str], delegated: frozenset[str]) -> None:
    """Ensure an active client, its grants in both modes and a usable secret.

    A new client is named after its ID. Run again, or on a client created through the API, it keeps
    the client's name and description, its secret and every grant it holds, and re-enables a
    disabled client. An unknown scope, or a client-only one in ``delegated``, is ``invalid_scope``
    naming the refused flag, and changes nothing.
    """
    grants = {GrantMode.APPLICATION: application, GrantMode.DELEGATED: delegated}
    results: dict[GrantMode, BootstrappedApplicationClient] = {}
    try:
        async with _session() as session:
            for mode, scopes in grants.items():
                results[mode] = await bootstrap_application_client(
                    session, client_id=client_id, scopes=scopes, mode=mode
                )
    except InvalidClientScopeError as exc:
        refused = next(mode for mode in grants if mode not in results)
        raise SystemExit(f"invalid_scope: --{refused}: {exc}") from None

    print(f"client_id={client_id}")
    for mode, result in results.items():
        print(f"{mode}_scopes={format_scopes(result.granted_scopes)}")
    # The first call creates a missing secret, so the second finds a usable one.
    _print_secret(results[GrantMode.APPLICATION].created_secret)


async def bootstrap_admin(*, party_id: uuid.UUID, email: str) -> None:
    """Ensure an enabled admin account for the person party and print a token to get in with.

    The break-glass command: run again it keeps the account, re-enables and re-promotes it, takes
    ``email``, and prints an invitation while the account has no password, a password reset once it
    has one. An unknown party, a company or an e-mail address another account uses changes nothing.
    """
    try:
        async with _session() as session:
            result = await bootstrap_admin_account(session, get_settings().auth, party_id=party_id, email=email)
    except DomainError as exc:
        raise SystemExit(f"{exc.code}: {exc}") from None

    print(f"user_id={result.account.id}")
    print(f"{result.issued.token.purpose}_token={result.issued.plaintext}")
    print(f"expires_at={result.issued.token.expires_at.isoformat()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.bootstrap", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    client = commands.add_parser("client", help="Seed an application client with grants in both modes.")
    client.add_argument("client_id", type=_client_id, help="Client ID of the application client.")
    client.add_argument(
        "--application",
        type=parse_scopes,
        required=True,
        help="Space-separated scopes the client may use for itself.",
    )
    client.add_argument(
        "--delegated",
        type=parse_scopes,
        required=True,
        help="Space-separated scopes the client may use at most for a person (the ceiling).",
    )
    admin = commands.add_parser(
        "admin", help="Ensure an enabled admin account for a person party and print an invitation or a reset token."
    )
    admin.add_argument("--party-id", type=uuid.UUID, required=True, help="ID of the person party of the account.")
    admin.add_argument("--email", type=_login_email, required=True, help="Login e-mail address of the account.")
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    match arguments.command:
        case "client":
            asyncio.run(
                bootstrap_client(arguments.client_id, application=arguments.application, delegated=arguments.delegated)
            )
        case "admin":
            asyncio.run(bootstrap_admin(party_id=arguments.party_id, email=arguments.email))


def _client_id(value: str) -> str:
    """The client ID as given on the command line, stripped; an empty one is refused."""
    if not (client_id := value.strip()):
        raise argparse.ArgumentTypeError("must not be empty")

    return client_id


def _login_email(value: str) -> str:
    """The e-mail address validated by the API's rule: a typo on the first admin could otherwise only be
    corrected through the API - which needs an admin who can log in."""
    try:
        return _LOGIN_EMAIL.validate_python(value)
    except ValidationError:
        raise argparse.ArgumentTypeError("not a valid e-mail address") from None


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    """A session on the configured database whose transaction commits when the block succeeds."""
    db = Database.from_url(str(get_settings().db.url))
    try:
        async with db.session() as session:
            yield session
    finally:
        await db.dispose()


def _print_secret(created_secret: CreatedClientSecret | None) -> None:
    if created_secret is None:
        print("client_secret=<existing usable secret retained>")
    else:
        print(f"client_secret={created_secret.plaintext}")


if __name__ == "__main__":
    main()
