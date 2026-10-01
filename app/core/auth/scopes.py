from collections.abc import Iterable, Set
from enum import StrEnum
from typing import Self


class Scope(StrEnum):
    """Defines the scopes for authentication.

    Each member is declared as ``(value, description)``. The value is the wire format; the
    description feeds the seeded ``PermissionScope`` rows and the OpenAPI security scheme, so a
    scope without a description cannot be declared.
    """

    description: str

    def __new__(cls, value: str, description: str) -> Self:
        member = str.__new__(cls, value)
        member._value_ = value
        member.description = description
        return member

    AUTH_CLIENTS_MANAGE = "auth:clients:manage", "Manage application clients."
    AUTH_USERS_MANAGE = "auth:users:manage", "Create, disable and reset user accounts; assign stored roles."
    AUTH_USERS_LOGIN = (
        "auth:users:login",
        "Log people in on their behalf (password / refresh_token grants, redeem, revoke). Client-only: never part "
        "of a person's token.",
    )
    AUTH_USERS_EXCHANGE = (
        "auth:users:exchange",
        "Obtain a person's token for the Discord user they linked, and redeem Discord link codes; the client's secret "
        "speaks for every linked person up to its delegated ceiling. Client-only: never part of a person's token.",
    )
    AUTH_DISCORD_LINKS_READ = (
        "auth:discord-links:read",
        "Read Discord links - which Discord account belongs to which person party - including unlinked ones.",
    )
    CRM_READ = "crm:read", "Read parties, relations and subjects."
    CRM_READ_OWN = "crm:read:own", "Read parties within the caller's reach."
    CRM_WRITE = "crm:write", "Create, change and delete parties, relations and subjects."
    ACCOUNT_SELF = "account:self", "Manage the caller's own account."


CLIENT_ONLY_SCOPES: frozenset[Scope] = frozenset({Scope.AUTH_USERS_LOGIN, Scope.AUTH_USERS_EXCHANGE})
"""Scopes that only make sense for a client: grantable in ``application`` mode only, in no role's scopes."""

VOUCHED_SCOPES: frozenset[Scope] = frozenset({Scope.CRM_READ, Scope.CRM_READ_OWN, Scope.CRM_WRITE})
"""The only scopes a token obtained without a password carries: a client vouched for the person (bot-decoupling spec,
decision R).

Every other scope - ``account:self``, every ``auth:*`` and every scope added later - needs a password login until it
is added here on purpose. The exchange takes this set as a ceiling, and a token with ``amr: ["discord"]`` that
carries another scope is invalid.
"""

OWN_VARIANT: dict[Scope, Scope] = {
    Scope.CRM_READ: Scope.CRM_READ_OWN,
}
"""Maps an unqualified scope to its reach-qualified ``:own`` form (ADR 0008).

``require_scopes`` never demands a value of this map; add an entry only together with the routes
that honor it.
"""

BASE_OF: dict[Scope, Scope] = {own: base for base, own in OWN_VARIANT.items()}
"""Maps a reach-qualified scope back to its unqualified form - the inverse of ``OWN_VARIANT``."""


def parse_scopes(scopes: str | Iterable[str] | None) -> frozenset[str]:
    """Return ``scopes`` as a set - the one place a scope string is split.

    An OAuth2 scope string (RFC 6749, section 3.3) is split at whitespace; any other iterable is
    taken value by value, stripped, with empty values dropped; ``None`` is no scope.
    """
    if scopes is None:
        return frozenset()
    if isinstance(scopes, str):
        return frozenset(scopes.split())

    return frozenset(value for scope in scopes if (value := scope.strip()))


def format_scopes(scopes: Iterable[str]) -> str:
    """Return the OAuth2 scope string of ``scopes``: sorted, deduplicated, space-separated; inverts ``parse_scopes``."""
    return " ".join(sorted(set(scopes)))


def expand(scopes: Set[str]) -> frozenset[str]:
    """Return ``scopes`` plus the ``:own`` variant of every unqualified scope among them.

    What a set of scopes *permits*: every check expands first, so a token carrying the unqualified
    scope satisfies a requirement of its ``:own`` variant.
    """
    return frozenset(scopes) | _implied_own_variants(scopes)


def canonical(scopes: Set[str]) -> frozenset[str]:
    """Return ``scopes`` without every ``:own`` variant whose unqualified scope is among them.

    What a token carries: holding both forms is redundant. The inverse of ``expand`` on canonical
    sets.
    """
    return frozenset(scopes) - _implied_own_variants(scopes)


def _implied_own_variants(scopes: Set[str]) -> frozenset[Scope]:
    return frozenset(own for base, own in OWN_VARIANT.items() if base in scopes)
