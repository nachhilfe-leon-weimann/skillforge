import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from .roles import Role


class PrincipalType(StrEnum):
    """Whom an access token speaks for - the value of its ``principal_type`` claim."""

    APPLICATION = "application"
    USER = "user"


class AuthMethod(StrEnum):
    """How a person's token was obtained - a value of its ``amr`` claim (RFC 8176).

    ``pwd``: the person logged in with their password. ``discord``: a client holding ``auth:users:exchange``
    vouched for the person by their linked Discord user - no proof by the person themselves.
    """

    PASSWORD = "pwd"
    DISCORD = "discord"


@dataclass(frozen=True)
class PasswordLogin:
    """The person logged in with their password; ``session_id`` is the session that login opened (``sid``)."""

    session_id: uuid.UUID


@dataclass(frozen=True)
class DiscordLogin:
    """A client vouched for the person by their linked Discord user: no password, no session, and only
    ``VOUCHED_SCOPES`` (bot-decoupling spec, decisions P to S)."""


type Login = PasswordLogin | DiscordLogin
"""How a person's token was obtained: the typed form of its ``amr`` and ``sid`` claims. An action that needs a
password matches ``PasswordLogin``."""


@dataclass(frozen=True, kw_only=True)
class Principal(ABC):
    """Who a validated access token speaks for (ADR 0008).

    Never one of its own: a token speaks for an ``ApplicationPrincipal`` or a ``UserPrincipal``.
    Code that needs one of the two narrows with ``isinstance`` or ``match`` - the type then
    guarantees what the principal carries, instead of an optional field that has to be checked.
    """

    principal_type: ClassVar[PrincipalType]

    principal_id: uuid.UUID
    client_id: str
    """The client the token was issued to; for a person, the client that logged them in (``azp``)."""
    scopes: frozenset[str]

    @property
    @abstractmethod
    def subject(self) -> str:
        """The token's ``sub`` claim, derived from the principal and checked against it on validation."""


@dataclass(frozen=True, kw_only=True)
class ApplicationPrincipal(Principal):
    """An application client acting for itself (``client_credentials``); ``principal_id`` is the client row."""

    principal_type = PrincipalType.APPLICATION

    @property
    def subject(self) -> str:
        return f"app:{self.client_id}"


@dataclass(frozen=True, kw_only=True)
class UserPrincipal(Principal):
    """A person, acting through the client that logged them in; ``principal_id`` is their user account."""

    principal_type = PrincipalType.USER

    party_id: uuid.UUID
    roles: frozenset[Role]
    """Which views to offer. Informational only: SkillForge authorizes by scope and never branches on a role."""
    login: Login
    """How the token was obtained: a password login with its session, or a Discord user a client vouched for."""

    @property
    def subject(self) -> str:
        return f"user:{self.principal_id}"
