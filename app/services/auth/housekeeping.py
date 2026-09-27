"""Housekeeping of the auth tables (#159, bot-decoupling spec P0-3): deleting login sessions and one-time tokens
long past their expiry.

A row goes by its ``expires_at`` alone - a revoked session, a used or invalidated token too - once it lies more
than ``RETENTION_AFTER_EXPIRY`` in the past. Until then a replayed refresh token is still denied as one of an ended
session, naming the account. Nothing is audited per row: the history of a session or a token is in
``auth_audit_log``, which holds no foreign key to either table and outlives both.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.models import UserActionToken, UserSession

RETENTION_AFTER_EXPIRY = timedelta(days=30)


async def delete_expired_sessions(session: AsyncSession, *, limit: int, now: datetime | None = None) -> int:
    """Delete up to ``limit`` sessions that expired more than ``RETENTION_AFTER_EXPIRY`` ago, oldest first."""
    return await _delete_expired(session, UserSession, limit=limit, now=now)


async def delete_expired_action_tokens(session: AsyncSession, *, limit: int, now: datetime | None = None) -> int:
    """Delete up to ``limit`` one-time tokens that expired more than ``RETENTION_AFTER_EXPIRY`` ago, oldest first."""
    return await _delete_expired(session, UserActionToken, limit=limit, now=now)


async def _delete_expired(
    session: AsyncSession, model: type[UserSession | UserActionToken], *, limit: int, now: datetime | None
) -> int:
    """Rows another transaction holds (a refresh, a token issue, a cascading party delete) are skipped, never
    waited on: they go in a later batch. Housekeeping never waits on a request, so it cannot deadlock with one; a
    request may wait for one short batch."""
    cutoff = (now or datetime.now(UTC)) - RETENTION_AFTER_EXPIRY
    doomed = (
        select(model.id)
        .where(model.expires_at < cutoff)
        .order_by(model.expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(delete(model).where(model.id.in_(doomed)))
    # ``execute`` is typed as ``Result``; a DELETE always yields a ``CursorResult`` with ``rowcount``.
    return cast(CursorResult, result).rowcount
