"""The app's token: a client-credentials grant with a certificate, signed here and sent through the guarded transport.

Microsoft's own library (``msal``) would bring its own HTTP client, past the guarded transport (M5 of
``docs/specs/billing.md``), so the client assertion is a JWT signed with ``pyjwt[crypto]``: RS256 with this
location's key, the certificate's SHA-1 thumbprint as ``x5t``. Certificate, key, assertion and token appear in no
log line, error message or ``repr``.
"""

import base64
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from pydantic import BaseModel, ConfigDict, PositiveInt, SecretStr

from app.integrations.config import IntegrationAccount, IntegrationMode
from app.integrations.errors import IntegrationRefusedError, UnexpectedResponseError
from app.integrations.transport import InnerTransport, IntegrationHttp, Reply

LOGIN_HOST = "login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_LIFETIME_SECONDS = 300
REFRESH_MARGIN_SECONDS = 300
"""A cached token is used until this long before it expires."""
TOKEN_PATH_TEMPLATE = "/{tenant}/oauth2/v2.0/token"
_HEADER_VALUE = re.compile(r"[\x21-\x7e]+")
"""Printable ASCII without whitespace: what a ``Bearer`` header can carry unchanged."""


@dataclass(frozen=True, slots=True)
class AppCredential:
    """The app's identity in one tenant, with this location's certificate; shows no secret in its ``repr``."""

    tenant_id: str = field(repr=False)
    client_id: str
    thumbprint: str
    """The certificate's SHA-1 thumbprint, base64url: the ``x5t`` header Microsoft documents."""
    key: RSAPrivateKey = field(repr=False)

    @property
    def token_url(self) -> str:
        return f"https://{LOGIN_HOST}/{self.tenant_id}/oauth2/v2.0/token"


def load_credential(
    *, tenant_id: str, client_id: str, certificate_pem: SecretStr, private_key_pem: SecretStr
) -> AppCredential:
    """Read certificate and key from their base64-encoded PEM settings; a key that does not belong to the
    certificate, or is no RSA key, is refused. No message repeats or chains what failed to parse."""
    try:
        certificate = x509.load_pem_x509_certificate(base64.b64decode(certificate_pem.get_secret_value()))
    except ValueError:
        raise IntegrationRefusedError("GRAPH__CERTIFICATE_PEM holds no readable certificate") from None
    try:
        key = serialization.load_pem_private_key(base64.b64decode(private_key_pem.get_secret_value()), password=None)
    except ValueError, TypeError:
        raise IntegrationRefusedError("GRAPH__PRIVATE_KEY_PEM holds no readable unencrypted key") from None
    if not isinstance(key, RSAPrivateKey):
        raise IntegrationRefusedError("GRAPH__PRIVATE_KEY_PEM must hold an RSA key")
    if certificate.public_key() != key.public_key():
        raise IntegrationRefusedError("GRAPH__PRIVATE_KEY_PEM does not belong to GRAPH__CERTIFICATE_PEM")
    thumbprint = base64.urlsafe_b64encode(certificate.fingerprint(hashes.SHA1())).decode().rstrip("=")
    return AppCredential(tenant_id=tenant_id, client_id=client_id, thumbprint=thumbprint, key=key)


def client_assertion(credential: AppCredential, *, now: float) -> str:
    """The signed JWT that proves the app's identity to the token endpoint, valid for a few minutes."""
    issued = int(now)
    claims = {
        "aud": credential.token_url,
        "iss": credential.client_id,
        "sub": credential.client_id,
        "jti": str(uuid.uuid4()),
        "iat": issued,
        "nbf": issued,
        "exp": issued + ASSERTION_LIFETIME_SECONDS,
    }
    return jwt.encode(claims, credential.key, algorithm="RS256", headers={"x5t": credential.thumbprint})


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    token_type: Literal["Bearer"]
    expires_in: PositiveInt
    access_token: SecretStr


@dataclass(slots=True)
class _CachedToken:
    value: SecretStr
    expires_at: float


class TokenCache:
    """App tokens in memory, per tenant, app and certificate, until shortly before they expire."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tokens: dict[tuple[str, str, str], _CachedToken] = {}

    def __repr__(self) -> str:
        return f"TokenCache(tokens={len(self._tokens)})"

    def get(self, credential: AppCredential) -> SecretStr | None:
        cached = self._tokens.get(_key(credential))
        if cached is None or self._clock() >= cached.expires_at:
            return None
        return cached.value

    def put(self, credential: AppCredential, value: SecretStr, expires_in: int) -> None:
        self._tokens[_key(credential)] = _CachedToken(value, self._clock() + expires_in - REFRESH_MARGIN_SECONDS)


def _key(credential: AppCredential) -> tuple[str, str, str]:
    return credential.tenant_id, credential.client_id, credential.thumbprint


TOKENS = TokenCache()
"""The process's tokens."""


async def app_token(
    credential: AppCredential,
    *,
    mode: IntegrationMode,
    tokens: TokenCache,
    transport: InnerTransport | None = None,
    now: Callable[[], float] = time.time,
) -> SecretStr:
    """The app's access token for Graph: from the cache, or from the token endpoint - a POST that changes nothing,
    so it is sent as a read in every mode."""
    if (cached := tokens.get(credential)) is not None:
        return cached
    form = {
        "client_id": credential.client_id,
        "scope": SCOPE,
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": client_assertion(credential, now=now()),
    }
    login = IntegrationHttp(
        name="graph",
        base_url=f"https://{LOGIN_HOST}/{credential.tenant_id}/oauth2/v2.0",
        allowed_hosts=frozenset({LOGIN_HOST}),
        headers={"Accept": "application/json"},
        mode=mode,
        account=IntegrationAccount.LIVE,
        error_message=login_error,
        transport=transport,
    )
    async with login:
        reply = await login.read_form(credential.token_url, form=form, template=TOKEN_PATH_TEMPLATE)
    token = _token_response(reply)
    tokens.put(credential, token.access_token, token.expires_in)
    return token.access_token


def _token_response(reply: Reply) -> _TokenResponse:
    where = f"graph POST {TOKEN_PATH_TEMPLATE}"
    try:
        token = _TokenResponse.model_validate(reply.json())
    except ValueError, RecursionError:  # pydantic's ValidationError is a ValueError, and its text holds the input
        raise UnexpectedResponseError(f"{where}: the response is no token") from None
    if not _HEADER_VALUE.fullmatch(token.access_token.get_secret_value()):
        raise UnexpectedResponseError(f"{where}: the token cannot be sent in a header")
    return token


def login_error(reply: Reply) -> str | None:
    """The token endpoint's error code and its ``AADSTS`` numbers - never its description."""
    try:
        body = reply.json()
    except ValueError:
        return None
    match body:
        case {"error": str(error), "error_codes": [*codes]}:
            return f"{error} ({', '.join(f'AADSTS{code}' for code in codes)})"
        case {"error": str(error)}:
            return error
    return None
