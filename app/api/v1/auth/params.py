"""Parameter aliases the auth routes share."""

from typing import Annotated

from fastapi import Path

from app.core.auth import ApplicationPrincipal, Principal, Scope
from app.core.auth.dependencies import require_application_scopes, require_scopes
from app.core.auth.inputs import DiscordUserId

LoginClient = Annotated[ApplicationPrincipal, require_application_scopes(Scope.AUTH_USERS_LOGIN)]
"""A client acting on a person's behalf: `auth:users:login` in `application` mode; a person's token is refused
whatever it carries."""

ManageUsers = Annotated[Principal, require_scopes(Scope.AUTH_USERS_MANAGE)]
"""The caller of an admin route (`auth:users:manage`); its principal is the actor of the audit entry."""

DiscordUserIdPath = Annotated[DiscordUserId, Path(description="The Discord user's snowflake, as a decimal string.")]
