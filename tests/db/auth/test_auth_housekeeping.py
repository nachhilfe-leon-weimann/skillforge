"""Housekeeping of the auth tables (bot-decoupling P0-3, #159): what goes, what stays, and that it never waits."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Database
from app.core.db.models import (
    ApplicationClient,
    ApplicationClientStatus,
    AuthAuditLog,
    UserAccount,
    UserActionToken,
    UserActionTokenPurpose,
    UserSession,
)
from app.services.auth.audit import Operator
from app.services.auth.housekeeping import (
    RETENTION_AFTER_EXPIRY,
    delete_expired_action_tokens,
    delete_expired_sessions,
)
from app.services.auth.users import create_user_account
from app.services.crm import parties, persons

pytestmark = pytest.mark.db

# Far in the past, so that only the rows a test creates can match - even rows other tests committed.
NOW = datetime(2001, 1, 1, tzinfo=UTC)
CUTOFF = NOW - RETENTION_AFTER_EXPIRY


def _session_row(account_id: uuid.UUID, client_id: uuid.UUID, expires_at: datetime, **fields: object) -> UserSession:
    return UserSession(
        user_account_id=account_id,
        application_client_id=client_id,
        scope="account:self",
        refresh_token_hash=f"test-hash-{uuid.uuid4().hex}",
        expires_at=expires_at,
        **fields,
    )


def _token_row(
    account_id: uuid.UUID, purpose: UserActionTokenPurpose, expires_at: datetime, **fields: object
) -> UserActionToken:
    return UserActionToken(
        user_account_id=account_id,
        purpose=purpose,
        token_hash=f"test-hash-{uuid.uuid4().hex}",
        expires_at=expires_at,
        issued_by="cli",
        **fields,
    )


@pytest.fixture
async def account(session: AsyncSession, make_person) -> UserAccount:
    person = await make_person()
    return (await create_user_account(session, party_id=person.id, actor=Operator.CLI)).account


async def _remaining(session: AsyncSession, model: type[UserSession] | type[UserActionToken]) -> set[datetime]:
    return set(await session.scalars(select(model.expires_at).where(model.expires_at < NOW + timedelta(days=1))))


async def test_a_session_goes_one_microsecond_past_the_window_and_stays_on_the_boundary(
    session: AsyncSession, account: UserAccount, application_client: ApplicationClient
):
    past = CUTOFF - timedelta(microseconds=1)
    kept = {CUTOFF, CUTOFF + timedelta(seconds=1), NOW + timedelta(days=1) - timedelta(seconds=1)}
    session.add_all(_session_row(account.id, application_client.id, when) for when in {past, *kept})
    await session.flush()

    deleted = await delete_expired_sessions(session, limit=10, now=NOW)

    assert deleted == 1
    assert await _remaining(session, UserSession) == kept


@pytest.mark.parametrize(
    "state", [{}, {"revoked_at": datetime(1999, 1, 1, tzinfo=UTC)}, {"rotated_at": datetime(1999, 1, 1, tzinfo=UTC)}]
)
async def test_a_session_goes_past_the_window_whatever_its_state(
    session: AsyncSession, account: UserAccount, application_client: ApplicationClient, state: dict[str, datetime]
):
    inside, outside = CUTOFF + timedelta(days=1), CUTOFF - timedelta(days=1)
    session.add_all([
        _session_row(account.id, application_client.id, inside, **state),
        _session_row(account.id, application_client.id, outside, **state),
    ])
    await session.flush()

    assert await delete_expired_sessions(session, limit=10, now=NOW) == 1
    assert await _remaining(session, UserSession) == {inside}


@pytest.mark.parametrize("purpose", list(UserActionTokenPurpose))
@pytest.mark.parametrize(
    "state", [{}, {"used_at": datetime(1999, 1, 1, tzinfo=UTC)}, {"invalidated_at": datetime(1999, 1, 1, tzinfo=UTC)}]
)
async def test_an_action_token_goes_past_the_window_whatever_its_state(
    session: AsyncSession, account: UserAccount, purpose: UserActionTokenPurpose, state: dict[str, datetime]
):
    inside, outside = CUTOFF + timedelta(days=1), CUTOFF - timedelta(days=1)
    session.add_all([
        _token_row(account.id, purpose, inside, **state),
        _token_row(account.id, purpose, outside, **state),
    ])
    await session.flush()

    assert await delete_expired_action_tokens(session, limit=10, now=NOW) == 1
    assert await _remaining(session, UserActionToken) == {inside}


async def test_a_call_deletes_at_most_limit_rows_oldest_first(
    session: AsyncSession, account: UserAccount, application_client: ApplicationClient
):
    doomed = [CUTOFF - timedelta(days=days) for days in (3, 2, 1)]
    session.add_all(_session_row(account.id, application_client.id, when) for when in doomed)
    await session.flush()

    assert await delete_expired_sessions(session, limit=2, now=NOW) == 2
    assert await _remaining(session, UserSession) == {doomed[2]}
    assert await delete_expired_sessions(session, limit=2, now=NOW) == 1
    assert await delete_expired_sessions(session, limit=2, now=NOW) == 0


async def test_housekeeping_writes_no_audit_entry_and_leaves_the_account_alone(
    session: AsyncSession, account: UserAccount, application_client: ApplicationClient
):
    session.add(_session_row(account.id, application_client.id, CUTOFF - timedelta(days=1)))
    await session.flush()
    audit_before = await session.scalar(select(func.count()).select_from(AuthAuditLog))

    await delete_expired_sessions(session, limit=10, now=NOW)

    assert await session.scalar(select(func.count()).select_from(AuthAuditLog)) == audit_before
    assert await session.get(UserAccount, account.id) is not None
    assert await session.get(ApplicationClient, application_client.id) is not None


@pytest.fixture
async def committed(db: Database) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A committed account and client for the two-transaction test; removed afterwards with their audit rows."""
    async with db.session() as setup:
        party_id = (await persons.create_person(setup, firstname="Skip", lastname="Locked")).id
        account_id = (await create_user_account(setup, party_id=party_id, actor=Operator.CLI)).account.id
        client = ApplicationClient(
            client_id=f"portal-{uuid.uuid4().hex[:8]}", name="Portal", status=ApplicationClientStatus.ACTIVE
        )
        setup.add(client)
        await setup.flush()
        client_id = client.id
    try:
        yield account_id, client_id
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(AuthAuditLog).where(AuthAuditLog.principal_id == str(account_id)))
            await parties.delete_party(cleanup, party_id)
            await cleanup.execute(delete(ApplicationClient).where(ApplicationClient.id == client_id))


async def test_a_row_another_transaction_holds_is_skipped_not_waited_on(
    db: Database, committed: tuple[uuid.UUID, uuid.UUID]
):
    account_id, client_id = committed
    async with db.session() as setup:
        rows = [_session_row(account_id, client_id, CUTOFF - timedelta(days=days)) for days in (2, 1)]
        setup.add_all(rows)
        await setup.flush()
        held_id = rows[0].id

    holder: AsyncSession = db.session_factory()
    try:
        await holder.execute(select(UserSession).where(UserSession.id == held_id).with_for_update())
        async with db.session() as cleaner:
            deleted = await asyncio.wait_for(delete_expired_sessions(cleaner, limit=10, now=NOW), timeout=5)
        assert deleted == 1
        await holder.commit()
    finally:
        await holder.close()

    async with db.session() as cleaner:
        assert await delete_expired_sessions(cleaner, limit=10, now=NOW) == 1
