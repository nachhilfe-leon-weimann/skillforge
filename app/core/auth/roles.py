from collections.abc import Iterable
from enum import StrEnum

from .scopes import Scope


class Role(StrEnum):
    """A view a user account holds; an account can hold several.

    ``student``, ``tutor`` and ``guardian`` follow from the CRM, ``admin`` is assigned to the account.
    """

    STUDENT = "student"
    TUTOR = "tutor"
    GUARDIAN = "guardian"
    ADMIN = "admin"


STORED_ROLES: frozenset[Role] = frozenset({Role.ADMIN})
"""Roles that are rows in ``auth.user_account_role`` rather than derived from the CRM."""

BASE_USER_SCOPES: frozenset[Scope] = frozenset({Scope.ACCOUNT_SELF, Scope.CRM_READ_OWN})
"""Scopes every user account holds, regardless of its roles."""

ROLE_SCOPES: dict[Role, frozenset[Scope]] = {
    Role.STUDENT: frozenset(),
    Role.TUTOR: frozenset(),
    Role.GUARDIAN: frozenset(),
    Role.ADMIN: frozenset({
        Scope.CRM_READ,
        Scope.CRM_WRITE,
        Scope.AUTH_USERS_MANAGE,
        Scope.AUTH_CLIENTS_MANAGE,
        Scope.AUTH_DISCORD_LINKS_READ,
        Scope.BOT_READ,
    }),
}
"""Scopes a role adds on top of ``BASE_USER_SCOPES``; the derived roles add none yet.

SkillForge authorizes by scope only and never branches on a role.
"""


def scopes_for(roles: Iterable[Role]) -> frozenset[Scope]:
    """Return the scopes a user account with ``roles`` holds: ``BASE_USER_SCOPES`` plus every role's own."""
    return BASE_USER_SCOPES.union(*(ROLE_SCOPES[role] for role in roles))
