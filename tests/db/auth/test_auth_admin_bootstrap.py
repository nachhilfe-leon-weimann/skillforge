"""`just bootstrap-admin`, the break-glass command (decision P), against the test database."""

import re
from collections import Counter
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cli import bootstrap
from app.core.auth.secrets import digest
from app.core.db.models import (
    GrantMode,
    Party,
    UserAccount,
    UserAccountRoleName,
    UserAccountStatus,
    UserActionTokenPurpose,
)
from app.services.auth.action_tokens import issue_action_token, redeem_action_token
from app.services.auth.audit import AuditEventType, Operator
from app.services.auth.bootstrap import bootstrap_admin_account, bootstrap_application_client
from app.services.auth.users import create_user_account, update_user_account
from app.services.crm import persons

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("command_session")]

REPORT = re.compile(
    r"user_id=(?P<user_id>\S+)\n(?P<purpose>invitation|password_reset)_token=(?P<token>sf_ua_\S+)\nexpires_at=\S+\n"
)


async def _run(party: Party, capsys, email: str = "admin@example.org") -> re.Match[str]:
    """Run the command and return its parsed report."""
    await bootstrap.bootstrap_admin(party_id=party.id, email=email)
    report = REPORT.fullmatch(capsys.readouterr().out)
    assert report, "the command prints user_id, the token and its expiry"
    return report


async def _hashes(live_tokens, user_id: UUID) -> set[str]:
    return {token.token_hash for token in await live_tokens(user_id)}


async def _account(session: AsyncSession, party: Party) -> UserAccount:
    statement = (
        select(UserAccount)
        .where(UserAccount.party_id == party.id)
        .options(selectinload(UserAccount.roles))
        .execution_options(populate_existing=True)
    )
    account = await session.scalar(statement)
    assert account is not None
    return account


async def test_the_first_run_creates_an_active_admin_and_prints_its_invitation(
    session: AsyncSession, make_person, audit_events, capsys, live_tokens
):
    party = await make_person()

    report = await _run(party, capsys, email="Admin@Example.org")

    account = await _account(session, party)
    assert report["user_id"] == str(account.id)
    assert report["purpose"] == "invitation"
    assert (account.email, account.status) == ("admin@example.org", UserAccountStatus.ACTIVE)
    assert [held.role for held in account.roles] == [UserAccountRoleName.ADMIN]
    assert await _hashes(live_tokens, account.id) == {digest(report["token"])}
    assert await audit_events(account.id) == {
        AuditEventType.USER_ACCOUNT_CREATED: 1,
        AuditEventType.INVITATION_ISSUED: 1,
    }


async def test_a_rerun_that_changes_nothing_records_only_the_issued_token(
    session: AsyncSession, make_person, audit_events, capsys, live_tokens
):
    party = await make_person()
    first = await _run(party, capsys)
    before = await audit_events(UUID(first["user_id"]))

    second = await _run(party, capsys)

    assert second["user_id"] == first["user_id"]
    assert second["token"] != first["token"]
    assert await _hashes(live_tokens, UUID(first["user_id"])) == {digest(second["token"])}
    assert await audit_events(UUID(first["user_id"])) - before == Counter({AuditEventType.INVITATION_ISSUED: 1})


async def test_a_rerun_re_promotes_re_enables_and_replaces_the_email(
    session: AsyncSession, make_person, audit_events, capsys, live_tokens
):
    party = await make_person()
    first = await _run(party, capsys)
    user_id = UUID(first["user_id"])
    await update_user_account(session, user_id, status=UserAccountStatus.DISABLED, actor=Operator.CLI)
    account = await _account(session, party)
    account.roles.clear()
    await session.flush()
    before = await audit_events(user_id)

    second = await _run(party, capsys, email="new.admin@example.org")

    account = await _account(session, party)
    assert (account.id, account.email, account.status) == (user_id, "new.admin@example.org", UserAccountStatus.ACTIVE)
    assert [held.role for held in account.roles] == [UserAccountRoleName.ADMIN]
    assert await _hashes(live_tokens, user_id) == {digest(second["token"])}
    assert await audit_events(user_id) - before == Counter({
        AuditEventType.USER_ROLE_ADDED: 1,
        AuditEventType.USER_ACCOUNT_ENABLED: 1,
        AuditEventType.USER_ACCOUNT_UPDATED: 1,
        AuditEventType.INVITATION_ISSUED: 1,
    })


async def test_the_email_change_invalidates_unused_tokens_of_every_purpose(
    session: AsyncSession, make_person, auth_settings, capsys, password, live_tokens
):
    party = await make_person()
    first = await _run(party, capsys)
    await redeem_action_token(session, plaintext=first["token"], new_password=password, actor=Operator.CLI)
    user_id = UUID(first["user_id"])
    earlier_reset = await issue_action_token(
        session, auth_settings, user_id=user_id, purpose=UserActionTokenPurpose.PASSWORD_RESET, actor=Operator.CLI
    )

    second = await _run(party, capsys, email="new.admin@example.org")

    assert second["purpose"] == "password_reset"
    assert await _hashes(live_tokens, user_id) == {digest(second["token"])}
    assert digest(earlier_reset.plaintext) not in await _hashes(live_tokens, user_id)


async def test_once_the_account_has_a_password_it_prints_a_reset_that_replaces_earlier_ones(
    session: AsyncSession, make_person, audit_events, capsys, password, live_tokens
):
    party = await make_person()
    first = await _run(party, capsys)
    await redeem_action_token(session, plaintext=first["token"], new_password=password, actor=Operator.CLI)
    second = await _run(party, capsys)

    third = await _run(party, capsys)

    assert (second["purpose"], third["purpose"]) == ("password_reset", "password_reset")
    assert await _hashes(live_tokens, UUID(first["user_id"])) == {digest(third["token"])}
    assert (await audit_events(UUID(first["user_id"])))[AuditEventType.PASSWORD_RESET_ISSUED] == 2
    await redeem_action_token(session, plaintext=third["token"], new_password="a new password", actor=Operator.CLI)


async def test_an_email_another_account_holds_is_refused_and_changes_nothing(
    session: AsyncSession, make_person, audit_events, capsys
):
    party = await make_person()
    first = await _run(party, capsys)
    await create_user_account(
        session, party_id=(await make_person("Ben")).id, email="ben@example.org", actor=Operator.CLI
    )
    account = await _account(session, party)
    account.roles.clear()
    await session.flush()
    before = await audit_events(UUID(first["user_id"]))

    with pytest.raises(SystemExit) as exit_code:
        await bootstrap.bootstrap_admin(party_id=party.id, email="BEN@example.org")

    assert str(exit_code.value.code).startswith("user_email_already_in_use: ")
    assert "ben@example.org" not in str(exit_code.value.code)
    assert capsys.readouterr().out == ""
    account = await _account(session, party)
    assert (account.email, account.roles) == ("admin@example.org", [])
    assert await audit_events(UUID(first["user_id"])) == before


async def test_an_unknown_party_or_a_company_is_refused(make_company, session: AsyncSession, capsys):
    with pytest.raises(SystemExit) as unknown:
        await bootstrap.bootstrap_admin(party_id=uuid4(), email="admin@example.org")
    with pytest.raises(SystemExit) as company:
        await bootstrap.bootstrap_admin(party_id=(await make_company()).id, email="admin@example.org")

    assert str(unknown.value.code).startswith("unknown_account_party: ")
    assert str(company.value.code).startswith("account_party_not_a_person: ")
    assert capsys.readouterr().out == ""
    assert list(await session.scalars(select(UserAccount))) == []


async def test_bootstrap_admin_needs_nothing_but_an_existing_person_party(
    session: AsyncSession, auth_settings, live_tokens
):
    """ "Operating without a portal", steps 1 to 3, on an empty database through the service entry points."""
    await bootstrap_application_client(
        session, client_id="operator", scopes=["auth:users:login", "crm:write"], mode=GrantMode.APPLICATION
    )
    await bootstrap_application_client(
        session,
        client_id="operator",
        scopes=[
            "account:self",
            "crm:read",
            "crm:write",
            "auth:users:manage",
            "auth:clients:manage",
            "auth:discord-links:read",
        ],
        mode=GrantMode.DELEGATED,
    )
    party = await persons.create_person(session, firstname="Ada", lastname="Admin")

    result = await bootstrap_admin_account(session, auth_settings, party_id=party.id, email="ada@example.org")

    assert result.created_account
    assert result.account.party_id == party.id
    assert result.account.status is UserAccountStatus.ACTIVE
    assert [held.role for held in result.account.roles] == [UserAccountRoleName.ADMIN]
    assert result.issued.token.purpose is UserActionTokenPurpose.INVITATION
    assert result.issued.token.issued_by == Operator.CLI
    assert await _hashes(live_tokens, result.account.id) == {digest(result.issued.plaintext)}
