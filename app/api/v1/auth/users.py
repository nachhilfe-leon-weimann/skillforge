"""The admin surface of user accounts: every route is guarded by `auth:users:manage`."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, status
from pydantic import Field

from app.api.v1.common import DBSession, Page, PageParams, error_responses
from app.core.auth import Scope
from app.core.auth.dependencies import AuthConfig, require_scopes
from app.core.auth.inputs import LoginEmail
from app.core.db.models import UserAccountRoleName, UserAccountStatus, UserActionTokenPurpose
from app.services.auth import action_tokens as action_tokens_service
from app.services.auth import sessions as sessions_service
from app.services.auth import users as users_service
from app.services.auth.errors import (
    AccountPartyNotAPersonError,
    UnknownAccountPartyError,
    UserAccountAlreadyExistsError,
    UserAccountNotFoundError,
    UserAccountStateError,
    UserEmailAlreadyInUseError,
    UserRoleNotFoundError,
)

from .params import ManageUsers
from .schemas import (
    ActionTokenResponse,
    UserAccountCreateRequest,
    UserAccountDetail,
    UserAccountListItem,
    UserAccountUpdateRequest,
)

router = APIRouter(prefix="/users")

UserId = Annotated[UUID, Path(description="ID of the user account.", examples=["3f2b8c1e-5a4d-4e6f-8a9b-0c1d2e3f4a5b"])]
StoredRole = Annotated[
    UserAccountRoleName,
    Path(description="A stored role; the other roles follow from the CRM.", examples=[UserAccountRoleName.ADMIN]),
]


class UserAccountListParams(PageParams):
    """Filters of `GET /users`. A combination nothing matches is an empty page."""

    status: UserAccountStatus | None = Field(None, description="Only accounts in this status.")
    party_id: UUID | None = Field(None, description="Only the account of this party; a party has at most one.")
    email: LoginEmail | None = Field(
        None, description="Only the account with this login e-mail address, compared in its canonical form."
    )


type UserAccountListQuery = Annotated[UserAccountListParams, Query()]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        UnknownAccountPartyError,
        AccountPartyNotAPersonError,
        UserAccountAlreadyExistsError,
        UserEmailAlreadyInUseError,
    ),
)
async def create_user(
    request: UserAccountCreateRequest, session: DBSession, principal: ManageUsers
) -> UserAccountDetail:
    """Create an `active` account for a person party.

    The response carries no token: set up the password login with `POST /users/{user_id}/invitation`.
    """
    view = await users_service.create_user_account(session, **request.model_dump(), actor=principal)
    return UserAccountDetail.from_view(view)


@router.get("", dependencies=[require_scopes(Scope.AUTH_USERS_MANAGE)])
async def list_users(params: UserAccountListQuery, session: DBSession) -> Page[UserAccountListItem]:
    """List user accounts, oldest first."""
    accounts, total = await users_service.list_user_accounts(session, **params.model_dump())
    return Page.of([UserAccountListItem.from_view(view) for view in accounts], total=total, params=params)


@router.get(
    "/{user_id}",
    dependencies=[require_scopes(Scope.AUTH_USERS_MANAGE)],
    responses=error_responses(UserAccountNotFoundError),
)
async def get_user(user_id: UserId, session: DBSession) -> UserAccountDetail:
    """Read a single user account with its stored and derived roles."""
    return UserAccountDetail.from_view(await users_service.load_user_account(session, user_id))


@router.patch(
    "/{user_id}",
    responses=error_responses(UserAccountNotFoundError, UserEmailAlreadyInUseError, UserAccountStateError),
)
async def update_user(
    user_id: UserId, request: UserAccountUpdateRequest, session: DBSession, principal: ManageUsers
) -> UserAccountDetail:
    """Change a user account; only the fields that are sent change.

    Disabling revokes every session right away; the access tokens already handed out live out their short lifetime.
    """
    view = await users_service.update_user_account(session, user_id, **request.model_dump(), actor=principal)
    return UserAccountDetail.from_view(view)


@router.put("/{user_id}/roles/{role}", responses=error_responses(UserAccountNotFoundError))
async def add_user_role(
    user_id: UserId, role: StoredRole, session: DBSession, principal: ManageUsers
) -> UserAccountDetail:
    """Give the account a stored role. Idempotent: a role it already holds changes nothing."""
    view = await users_service.add_user_role(session, user_id, role=role, actor=principal)
    return UserAccountDetail.from_view(view)


@router.delete(
    "/{user_id}/roles/{role}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(UserAccountNotFoundError, UserRoleNotFoundError),
)
async def remove_user_role(user_id: UserId, role: StoredRole, session: DBSession, principal: ManageUsers) -> None:
    """Take a stored role away."""
    await users_service.remove_user_role(session, user_id, role=role, actor=principal)


@router.post(
    "/{user_id}/invitation",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(UserAccountNotFoundError, UserAccountStateError),
)
async def issue_invitation(
    user_id: UserId, session: DBSession, settings: AuthConfig, principal: ManageUsers
) -> ActionTokenResponse:
    """Issue an invitation that sets up the password login; the person redeems it at `POST /password/redeem`.

    Needs an e-mail address and no password yet. Earlier unused invitations stop working.
    """
    issued = await action_tokens_service.issue_action_token(
        session, settings, user_id=user_id, purpose=UserActionTokenPurpose.INVITATION, actor=principal
    )
    return ActionTokenResponse.from_issued(issued)


@router.post(
    "/{user_id}/password-reset",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(UserAccountNotFoundError, UserAccountStateError),
)
async def issue_password_reset(
    user_id: UserId, session: DBSession, settings: AuthConfig, principal: ManageUsers
) -> ActionTokenResponse:
    """Issue a password reset, redeemed at `POST /password/redeem`.

    Needs a password. Earlier unused resets stop working; nothing else changes until the token is redeemed.
    """
    issued = await action_tokens_service.issue_action_token(
        session, settings, user_id=user_id, purpose=UserActionTokenPurpose.PASSWORD_RESET, actor=principal
    )
    return ActionTokenResponse.from_issued(issued)


@router.delete(
    "/{user_id}/sessions",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(UserAccountNotFoundError),
)
async def revoke_user_sessions(user_id: UserId, session: DBSession, principal: ManageUsers) -> None:
    """Revoke every live session of the account, so none of its refresh tokens works any more."""
    await sessions_service.revoke_user_sessions(session, user_id, actor=principal)
