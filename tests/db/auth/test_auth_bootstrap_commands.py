"""`just bootstrap-client` against the test database."""

import re
from collections import Counter

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import bootstrap
from app.core.auth import AuthSettings
from app.core.db.models import (
    ApplicationClient,
    ApplicationClientSecret,
    ApplicationClientStatus,
    AuthAuditLog,
    GrantMode,
)
from app.services.auth import issue_client_token
from app.services.auth.audit import AuditEventType

NEW_SECRET = re.compile(r"client_secret=(?P<plaintext>sf_live_\S+)")
RETAINED_SECRET = "client_secret=<existing usable secret retained>"

pytestmark = pytest.mark.usefixtures("command_session")

OPERATOR_APPLICATION = frozenset({"auth:users:login", "crm:write"})
OPERATOR_DELEGATED = frozenset({"account:self", "crm:read", "crm:write"})


@pytest.mark.db
async def test_bootstrap_client_grants_in_both_modes_and_prints_a_new_secret_once(
    session: AsyncSession, auth_settings, grants, capsys
):
    await _bootstrap_operator()
    first = capsys.readouterr().out.splitlines()
    await _bootstrap_operator()
    second = capsys.readouterr().out.splitlines()

    report = [
        "client_id=operator",
        "application_scopes=auth:users:login crm:write",
        "delegated_scopes=account:self crm:read crm:write",
    ]
    assert first[:3] == second[:3] == report
    assert second[3:] == [RETAINED_SECRET]
    [secret_line] = first[3:]

    clients = (await session.execute(select(ApplicationClient))).scalars().all()
    assert [(client.client_id, client.name, client.status) for client in clients] == [
        ("operator", "operator", ApplicationClientStatus.ACTIVE)
    ]
    assert len((await session.execute(select(ApplicationClientSecret))).scalars().all()) == 1
    assert await grants() == {
        *((scope, GrantMode.APPLICATION) for scope in OPERATOR_APPLICATION),
        *((scope, GrantMode.DELEGATED) for scope in OPERATOR_DELEGATED),
    }
    # The rerun changed nothing, so it recorded nothing.
    assert await _event_types(session) == Counter([
        "application_client.created",
        "scope_grant.added",
        "scope_grant.added",
        "client_secret.created",
        "scope_grant.added",
        "scope_grant.added",
        "scope_grant.added",
    ])
    # The printed secret is the client's; its own token draws on the application grants only.
    assert await _token_scope(session, auth_settings, "operator", secret_line) == "auth:users:login crm:write"


@pytest.mark.db
async def test_bootstrap_client_refuses_a_client_only_scope_as_delegated_and_changes_nothing(
    session: AsyncSession, capsys
):
    with pytest.raises(SystemExit) as exit_code:
        await bootstrap.bootstrap_client(
            "portal", application=frozenset({"auth:users:login"}), delegated=frozenset({"auth:users:login"})
        )

    assert exit_code.value.code == "invalid_scope: --delegated: Client-only scopes cannot be granted in delegated mode"
    assert capsys.readouterr().out == ""
    assert (await session.execute(select(ApplicationClient))).scalars().all() == []
    assert await _event_types(session) == Counter()


@pytest.mark.db
async def test_a_refused_rerun_leaves_the_client_as_it_was(grants):
    await _bootstrap_operator()
    before = await grants()

    with pytest.raises(SystemExit) as exit_code:
        await bootstrap.bootstrap_client(
            "operator", application=frozenset({"bot:write"}), delegated=frozenset({"auth:users:login"})
        )

    assert exit_code.value.code == "invalid_scope: --delegated: Client-only scopes cannot be granted in delegated mode"
    assert await grants() == before


@pytest.mark.db
async def test_bootstrap_client_refuses_an_unknown_scope(session: AsyncSession):
    with pytest.raises(SystemExit) as exit_code:
        await bootstrap.bootstrap_client("portal", application=frozenset({"nope:scope"}), delegated=frozenset())

    assert exit_code.value.code == "invalid_scope: --application: Requested scopes are not known or active"
    assert (await session.execute(select(ApplicationClient))).scalars().all() == []


@pytest.mark.db
async def test_bootstrap_client_names_the_flag_of_an_unknown_delegated_scope(session: AsyncSession):
    with pytest.raises(SystemExit) as exit_code:
        await bootstrap.bootstrap_client(
            "portal", application=frozenset({"crm:read"}), delegated=frozenset({"acount:self"})
        )

    assert exit_code.value.code == "invalid_scope: --delegated: Requested scopes are not known or active"
    assert (await session.execute(select(ApplicationClient))).scalars().all() == []


@pytest.mark.db
async def test_bootstrap_client_keeps_the_name_and_description_of_a_client_created_through_the_api(
    session: AsyncSession, client: AsyncClient
):
    created = await client.post(
        "/clients", json={"client_id": "operator", "name": "Operator Console", "description": "Back office"}
    )

    await _bootstrap_operator()
    await _bootstrap_operator()

    assert created.status_code == 201
    assert await _client_row(session, "operator") == ("Operator Console", "Back office", ApplicationClientStatus.ACTIVE)
    assert await _client_updates(session) == Counter()


@pytest.mark.db
async def test_bootstrap_client_re_enables_a_disabled_client_and_records_it_once(
    session: AsyncSession, client: AsyncClient
):
    await client.post("/clients", json={"client_id": "operator", "name": "Operator Console"})
    disabled = await client.patch("/clients/operator", json={"status": "disabled"})

    await _bootstrap_operator()
    await _bootstrap_operator()

    assert disabled.json()["status"] == "disabled"
    assert await _client_row(session, "operator") == ("Operator Console", None, ApplicationClientStatus.ACTIVE)
    assert await _client_updates(session) == Counter(["Updated application client operator."])


async def _bootstrap_operator() -> None:
    await bootstrap.bootstrap_client("operator", application=OPERATOR_APPLICATION, delegated=OPERATOR_DELEGATED)


async def _token_scope(session: AsyncSession, settings: AuthSettings, client_id: str, secret_line: str) -> str:
    """The scope of a `client_credentials` token obtained with the secret a command printed."""
    match = NEW_SECRET.fullmatch(secret_line)
    assert match
    token = await issue_client_token(session, settings, client_id=client_id, client_secret=match["plaintext"])
    return token.scope


async def _client_row(session: AsyncSession, client_id: str) -> tuple[str, str | None, ApplicationClientStatus]:
    statement = select(ApplicationClient.name, ApplicationClient.description, ApplicationClient.status).where(
        ApplicationClient.client_id == client_id
    )
    return (await session.execute(statement)).tuples().one()


async def _client_updates(session: AsyncSession) -> Counter[str | None]:
    """Details of the `application_client.updated` entries; the log has no order within a transaction."""
    statement = select(AuthAuditLog.detail).where(AuthAuditLog.event_type == AuditEventType.APPLICATION_CLIENT_UPDATED)
    return Counter((await session.execute(statement)).scalars())


async def _event_types(session: AsyncSession) -> Counter[str]:
    """Event types of the audit entries; the log has no order within a transaction."""
    return Counter((await session.execute(select(AuthAuditLog.event_type))).scalars())
