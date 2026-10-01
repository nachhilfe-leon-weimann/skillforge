"""The four account tables: metadata, the constraints of the "Accounts" section of the user
authentication spec, and what the unchanged ``delete_party`` does to them (ADR 0008)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from asyncpg.exceptions import CheckViolationError, PostgresError, UniqueViolationError
from sqlalchemy import Table, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import (
    ApplicationClient,
    AuthAuditLog,
    Party,
    PartyType,
    Person,
    UserAccount,
    UserAccountRole,
    UserAccountRoleName,
    UserAccountStatus,
    UserActionToken,
    UserActionTokenPurpose,
    UserSession,
)
from app.services.crm.parties import delete_party

pytestmark = pytest.mark.db


async def _assert_constraint_violation(
    session: AsyncSession, error_type: type[PostgresError], constraint_name: str, *objects: object
) -> None:
    """Assert the flush fails with *this* constraint, not merely with some ``IntegrityError`` -
    a generic catch would still pass if the intended constraint were dropped and a different one
    fired instead. SQLAlchemy's asyncpg dialect wraps the driver error; the raw
    ``asyncpg.exceptions.PostgresError`` (with its ``constraint_name``) is its ``__cause__``.
    """
    with pytest.raises(IntegrityError) as raised:
        async with session.begin_nested():
            session.add_all(objects)
            await session.flush()

    orig = raised.value.orig
    assert orig is not None
    cause = orig.__cause__
    assert isinstance(cause, error_type)
    assert getattr(cause, "constraint_name", None) == constraint_name


async def _person_party(session: AsyncSession, *, firstname: str = "Max") -> Party:
    party = Party(type=PartyType.PERSON, person=Person(firstname=firstname, lastname="Mustermann"))
    session.add(party)
    await session.flush()
    return party


async def test_auth_user_model_metadata():
    assert UserAccount.__table__.schema == "auth"
    assert UserAccountRole.__table__.schema == "auth"
    assert UserSession.__table__.schema == "auth"
    assert UserActionToken.__table__.schema == "auth"

    assert getattr(UserAccount.__table__.c.status.type, "enums", None) == ["active", "disabled"]
    assert UserAccount.__table__.c.status.default.arg is UserAccountStatus.ACTIVE
    assert UserAccount.__table__.c.status.server_default.arg.text == "'active'"
    assert UserAccount.__table__.c.party_id.unique is True
    assert UserAccount.__table__.c.email.unique is True
    assert UserAccount.__table__.c.email.nullable is True

    user_account_role = cast(Table, UserAccountRole.__table__)
    assert [column.name for column in user_account_role.primary_key.columns] == ["user_account_id", "role"]
    assert getattr(UserAccountRole.__table__.c.role.type, "enums", None) == ["admin"]

    assert UserSession.__table__.c.refresh_token_hash.unique is True
    assert UserSession.__table__.c.previous_refresh_token_hash.unique is True

    assert UserActionToken.__table__.c.token_hash.unique is True
    assert getattr(UserActionToken.__table__.c.purpose.type, "enums", None) == [
        "invitation",
        "password_reset",
        "discord_link",
    ]

    for model in (UserAccount, UserAccountRole, UserSession, UserActionToken):
        assert {key.ondelete for key in model.__table__.foreign_keys} == {"CASCADE"}

    assert "ix_user_session_expires_at" in {index.name for index in cast(Table, UserSession.__table__).indexes}
    assert "ix_user_action_token_expires_at" in {index.name for index in cast(Table, UserActionToken.__table__).indexes}


async def test_uppercase_email_violates_the_lowercase_check_constraint(session: AsyncSession):
    party = await _person_party(session)

    await _assert_constraint_violation(
        session,
        CheckViolationError,
        "ck_user_account_email_lowercase",
        UserAccount(party_id=party.id, email="Anna@Example.org"),
    )


async def test_a_second_account_for_the_same_party_violates_the_unique_constraint(session: AsyncSession):
    party = await _person_party(session)
    session.add(UserAccount(party_id=party.id, email="first@example.org"))
    await session.flush()

    await _assert_constraint_violation(
        session,
        UniqueViolationError,
        "user_account_party_id_key",
        UserAccount(party_id=party.id, email="second@example.org"),
    )


async def test_two_accounts_with_the_same_email_violate_the_unique_constraint(session: AsyncSession):
    first = await _person_party(session, firstname="Anna")
    second = await _person_party(session, firstname="Erika")
    session.add(UserAccount(party_id=first.id, email="anna@example.org"))
    await session.flush()

    await _assert_constraint_violation(
        session,
        UniqueViolationError,
        "user_account_email_key",
        UserAccount(party_id=second.id, email="anna@example.org"),
    )


async def test_two_accounts_without_an_email_do_not_conflict(session: AsyncSession):
    first = await _person_party(session, firstname="Anna")
    second = await _person_party(session, firstname="Erika")

    session.add_all([UserAccount(party_id=first.id), UserAccount(party_id=second.id)])
    await session.flush()

    assert await session.scalar(select(func.count()).select_from(UserAccount).where(UserAccount.email.is_(None))) == 2


async def test_a_new_account_defaults_to_active(session: AsyncSession):
    party = await _person_party(session)
    account = UserAccount(party_id=party.id)
    session.add(account)
    await session.flush()

    stored = await session.scalar(select(UserAccount.status).where(UserAccount.id == account.id))
    assert stored is UserAccountStatus.ACTIVE
    assert account.failed_login_count == 0


async def test_deleting_a_party_removes_its_account_roles_sessions_and_action_tokens_but_not_the_audit_log(
    session: AsyncSession,
):
    party = await _person_party(session)
    client = ApplicationClient(client_id="portal", name="Portal")
    session.add(client)
    await session.flush()

    account = UserAccount(party_id=party.id, email="admin@example.org")
    session.add(account)
    await session.flush()

    expires_at = datetime.now(UTC) + timedelta(days=1)
    session.add_all([
        UserAccountRole(user_account_id=account.id, role=UserAccountRoleName.ADMIN),
        UserSession(
            user_account_id=account.id,
            application_client_id=client.id,
            scope="account:self",
            refresh_token_hash="refresh-hash",
            expires_at=expires_at,
        ),
        UserActionToken(
            user_account_id=account.id,
            purpose=UserActionTokenPurpose.INVITATION,
            token_hash="action-hash",
            expires_at=expires_at,
            issued_by="cli",
        ),
    ])
    audit_log = AuthAuditLog(
        principal_type="user",
        principal_id=str(account.id),
        event_type="user_account.created",
        success=True,
    )
    session.add(audit_log)
    await session.flush()

    await delete_party(session, party.id)

    assert await session.scalar(select(func.count()).select_from(UserAccount)) == 0
    assert await session.scalar(select(func.count()).select_from(UserAccountRole)) == 0
    assert await session.scalar(select(func.count()).select_from(UserSession)) == 0
    assert await session.scalar(select(func.count()).select_from(UserActionToken)) == 0
    assert await session.scalar(select(func.count()).select_from(ApplicationClient)) == 1

    # Columns, not the entity: ``delete_party`` expires nothing, so the entity would come from the
    # identity map instead of the database.
    surviving = await session.execute(
        select(AuthAuditLog.principal_type, AuthAuditLog.principal_id).where(AuthAuditLog.id == audit_log.id)
    )
    assert surviving.one() == ("user", str(account.id))


async def test_deleting_a_party_leaves_the_accounts_of_other_parties_alone(session: AsyncSession):
    kept_party = await _person_party(session, firstname="Erika")
    kept_account = UserAccount(party_id=kept_party.id, email="kept@example.org")
    session.add(kept_account)
    doomed_party = await _person_party(session, firstname="Max")
    session.add(UserAccount(party_id=doomed_party.id))
    await session.flush()

    await delete_party(session, doomed_party.id)

    assert list(await session.scalars(select(UserAccount.id))) == [kept_account.id]


async def test_user_account_role_model_relationship(session: AsyncSession):
    party = await _person_party(session)
    account = UserAccount(party_id=party.id)
    role = UserAccountRole(user_account=account, role=UserAccountRoleName.ADMIN)
    session.add_all([account, role])
    await session.flush()

    assert isinstance(role.user_account_id, uuid.UUID)
    assert role.user_account_id == account.id
    # Assigned through the relationship above, so this reads the identity map, not a lazy load
    # (async SQLAlchemy cannot lazy-load; see PARTY_GRAPH in app/services/crm/parties.py).
    assert account.roles == [role]
