import base64
import binascii
import re
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from skillcore.config import CoreSettings

from app.integrations.config import EmptyIsDefault, IntegrationMode

_MAILBOX = re.compile(r"[^\s/?#@%]+@[^\s/?#@%]+")
"""An address that goes into a URL path unchanged: no whitespace, no path, query or fragment delimiter."""


def _mailbox(value: str) -> str:
    if not _MAILBOX.fullmatch(value):
        raise ValueError("GRAPH__MAILBOX must be one e-mail address")
    return value


type Mailbox = Annotated[str, AfterValidator(_mailbox)]


def _pem(variable: str, label: str) -> AfterValidator:
    """A PEM text base64-encoded into one line, as the secret settings hold it; the value is never repeated."""
    marker = re.compile(rf"-----BEGIN [A-Z ]*{label}-----")

    def validate(value: SecretStr) -> SecretStr:
        try:
            text = base64.b64decode(value.get_secret_value(), validate=True).decode("ascii")
        except binascii.Error, UnicodeDecodeError:
            raise ValueError(f"{variable} must be a PEM text, base64-encoded into one line") from None
        if not marker.search(text):
            raise ValueError(f"{variable} must hold a PEM {label.lower()}")
        return value

    return AfterValidator(validate)


class GraphSettings(CoreSettings):
    """Microsoft Graph settings (``GRAPH__*``). All optional, so the app starts without them; a calendar read
    without the app's credential, the mailbox or the calendar name is refused before anything is sent.

    There is no ``GRAPH__ACCOUNT``: Graph has no trial, so the client treats its tenant as a live account, which
    no test run - the contract run included - may reach.
    """

    mode: Annotated[IntegrationMode, EmptyIsDefault] = IntegrationMode.DRY_RUN
    """As for sevDesk. The client has no write - its token request is declared a read - so both values behave
    alike today."""
    tenant_id: Annotated[UUID | None, EmptyIsDefault] = None
    """The Entra tenant of the app ``skillforge``."""
    client_id: Annotated[UUID | None, EmptyIsDefault] = None
    """The application (client) id of the app ``skillforge``."""
    certificate_pem: Annotated[SecretStr, _pem("GRAPH__CERTIFICATE_PEM", "CERTIFICATE")] | None = None
    """This location's certificate, registered with the app: its PEM text, base64-encoded into one line."""
    private_key_pem: Annotated[SecretStr, _pem("GRAPH__PRIVATE_KEY_PEM", "PRIVATE KEY")] | None = None
    """The certificate's unencrypted RSA key: its PEM text, base64-encoded into one line."""
    mailbox: Annotated[Mailbox | None, EmptyIsDefault] = Field(default=None, repr=False)
    """The address of the mailbox that holds the lesson calendar."""
    calendar_name: Annotated[str | None, EmptyIsDefault] = Field(default=None, repr=False)
    """The name of the lesson calendar in that mailbox."""

    # A malformed secret is still a secret: no validation error repeats an input.
    model_config = SettingsConfigDict(env_prefix="GRAPH__", hide_input_in_errors=True)
