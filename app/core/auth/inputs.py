"""Vocabulary shared by the auth services, the auth API and the operator commands.

The API and ``just bootstrap-admin`` import from here, never the reverse: this module stays free of
``app.api`` and of the services.
"""

import re
from typing import Annotated

from pydantic import (
    AfterValidator,
    EmailStr,
    PlainSerializer,
    PlainValidator,
    StringConstraints,
    WithJsonSchema,
    validate_email,
)

MAX_EMAIL_LENGTH = 254
"""The longest e-mail address there is (RFC 5321); the column is ``text``, the limit documents the rule."""


def normalize_email(email: str) -> str:
    """Return the one canonical form of a login e-mail address (user-authentication spec, decision D).

    Parsed the way ``LoginEmail`` parses it - a display name (``Anna <anna@example.org>``) is dropped, the
    domain is read as Unicode (``xn--bcher-kva.de`` is ``bücher.de``), the text is NFC-normalized - then
    lowercased. Every spelling of one address yields the same value, the one that is stored and looked up;
    the form ``ck_user_account_email_lowercase`` accepts. Raises ``ValueError`` for what is no address.
    """
    _, address = validate_email(email)
    return address.lower()


LoginEmail = Annotated[EmailStr, StringConstraints(max_length=MAX_EMAIL_LENGTH), AfterValidator(normalize_email)]
"""A login e-mail address, parsed into its canonical form: for the API schemas and ``just bootstrap-admin`` alike.

A plain assignment rather than a PEP 695 alias: the API inlines the constraints at its field, where an
alias would become a schema of its own.
"""

MAX_BIGINT = 2**63 - 1
"""The largest value of the ``BIGINT`` column a Discord user ID is stored in."""

_DECIMAL = re.compile(r"[0-9]{1,19}")


def _parse_discord_user_id(value: object) -> int:
    """Accept a decimal string from the wire, or an ``int`` from Python code, within ``0 .. MAX_BIGINT``."""
    if isinstance(value, bool):
        raise ValueError("a Discord user ID is a decimal string")
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        value = int(value)
    if isinstance(value, int) and 0 <= value <= MAX_BIGINT:
        return value
    raise ValueError("a Discord user ID is a decimal string of 0 to 2^63 - 1")


DiscordUserId = Annotated[
    int,
    PlainValidator(_parse_discord_user_id),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "pattern": "^[0-9]{1,19}$"}),
]
"""A Discord user's snowflake: an ``int`` in Python, a decimal string on the wire (bot-decoupling spec, decision I).

Snowflakes exceed 2^53, so JavaScript - the portal, release-please's rewrite of ``openapi.json`` - would round a
JSON number; Discord's own API sends strings for the same reason. A plain assignment, like ``LoginEmail``.
"""
