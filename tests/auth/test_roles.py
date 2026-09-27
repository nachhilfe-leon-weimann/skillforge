import pytest

from app.core.auth import Scope
from app.core.auth.roles import BASE_USER_SCOPES, ROLE_SCOPES, STORED_ROLES, Role, scopes_for
from app.core.auth.scopes import CLIENT_ONLY_SCOPES
from app.core.db.models import UserAccountRoleName


def test_stored_roles_is_exactly_admin():
    assert STORED_ROLES == {Role.ADMIN}


def test_base_user_scopes_is_account_self_and_crm_read_own():
    assert BASE_USER_SCOPES == {Scope.ACCOUNT_SELF, Scope.CRM_READ_OWN}


def test_role_scopes_has_an_entry_for_every_role():
    """``scopes_for`` indexes ``ROLE_SCOPES`` without a default: a ``Role`` added without an entry
    would be a bare ``KeyError`` at token issuance."""
    assert set(ROLE_SCOPES) == set(Role)


@pytest.mark.parametrize("role", [Role.STUDENT, Role.TUTOR, Role.GUARDIAN])
def test_derived_roles_carry_no_scopes_of_their_own(role: Role):
    assert ROLE_SCOPES[role] == frozenset()


def test_admin_role_scopes_match_the_spec_and_exclude_bot_write():
    assert ROLE_SCOPES[Role.ADMIN] == {
        Scope.CRM_READ,
        Scope.CRM_WRITE,
        Scope.AUTH_USERS_MANAGE,
        Scope.AUTH_CLIENTS_MANAGE,
        Scope.AUTH_DISCORD_LINKS_READ,
        Scope.BOT_READ,
    }


def test_scopes_for_no_roles_is_the_base_user_scopes():
    assert scopes_for([]) == BASE_USER_SCOPES


def test_scopes_for_unites_the_base_with_every_roles_scopes():
    assert scopes_for([Role.TUTOR, Role.ADMIN]) == BASE_USER_SCOPES | ROLE_SCOPES[Role.ADMIN]


def test_no_role_carries_client_only_scopes():
    """Client-only scopes never reach a person's token: they are neither in the scopes every user
    holds nor in any role's scopes."""
    assert not CLIENT_ONLY_SCOPES & scopes_for(Role)


def test_reading_discord_links_is_grantable_in_both_modes():
    assert Scope.AUTH_DISCORD_LINKS_READ not in CLIENT_ONLY_SCOPES


def test_stored_roles_are_the_values_of_the_stored_role_enum():
    """``STORED_ROLES`` (the mapping) and ``UserAccountRoleName`` (the table's enum) state one fact twice."""
    assert {role.value for role in STORED_ROLES} == {role.value for role in UserAccountRoleName}
