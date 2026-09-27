import pytest

from app.core.db.models import (
    DiscordUser,
    MemberRole,
    PermissionGroup,
)
from app.services.bot import (
    GroupMembershipNotFoundError,
    PermissionGroupNotFoundError,
    PrincipalNotFoundError,
    add_user_to_group,
    get_principal_view,
    remove_user_from_group,
    upsert_discord_user,
)

# Snowflake-sized ids (> 2**32) so the tests fail if discord_id is not a BIGINT.
DISCORD_ID = 123456789012345678


# --- upsert user ------------------------------------------------------------


@pytest.mark.db
async def test_upsert_discord_user_creates_then_updates(session):
    created = await upsert_discord_user(session, discord_id=DISCORD_ID, role=MemberRole.STUDENT, nick_name="Stud")
    assert created.role is MemberRole.STUDENT
    assert created.active is True

    updated = await upsert_discord_user(
        session, discord_id=DISCORD_ID, role=MemberRole.TUTOR, nick_name="Now Tutor", active=False
    )
    assert updated.discord_id == DISCORD_ID
    assert updated.role is MemberRole.TUTOR
    assert updated.nick_name == "Now Tutor"
    assert updated.active is False

    fetched = await session.get(DiscordUser, DISCORD_ID)
    assert fetched is not None
    assert fetched.role is MemberRole.TUTOR
    assert fetched.nick_name == "Now Tutor"
    assert fetched.active is False


# --- group membership -------------------------------------------------------


@pytest.mark.db
async def test_add_user_to_group_creates_and_is_idempotent(session):
    await upsert_discord_user(session, discord_id=DISCORD_ID, role=MemberRole.TUTOR, nick_name="Tutor")
    await _add_group(session, "support")

    first = await add_user_to_group(session, discord_id=DISCORD_ID, group_key="support")
    assert (first.discord_id, first.group_key) == (DISCORD_ID, "support")
    # Idempotent: a second add does not raise and does not duplicate.
    await add_user_to_group(session, discord_id=DISCORD_ID, group_key="support")

    view = await get_principal_view(session, DISCORD_ID)
    assert view.group_keys == ["support"]


@pytest.mark.db
async def test_add_user_to_group_unknown_user_raises(session):
    await _add_group(session, "support")

    with pytest.raises(PrincipalNotFoundError):
        await add_user_to_group(session, discord_id=DISCORD_ID, group_key="support")


@pytest.mark.db
async def test_add_user_to_group_unknown_group_raises(session):
    await upsert_discord_user(session, discord_id=DISCORD_ID, role=MemberRole.TUTOR, nick_name="Tutor")

    with pytest.raises(PermissionGroupNotFoundError):
        await add_user_to_group(session, discord_id=DISCORD_ID, group_key="nope")


@pytest.mark.db
async def test_remove_user_from_group_removes_then_unknown_raises(session):
    await upsert_discord_user(session, discord_id=DISCORD_ID, role=MemberRole.TUTOR, nick_name="Tutor")
    await _add_group(session, "support")
    await add_user_to_group(session, discord_id=DISCORD_ID, group_key="support")

    await remove_user_from_group(session, discord_id=DISCORD_ID, group_key="support")

    view = await get_principal_view(session, DISCORD_ID)
    assert view.group_keys == []

    with pytest.raises(GroupMembershipNotFoundError):
        await remove_user_from_group(session, discord_id=DISCORD_ID, group_key="support")


# --- helpers ----------------------------------------------------------------


async def _add_group(session, key: str) -> None:
    session.add(PermissionGroup(key=key, name=key.title()))
    await session.flush()
