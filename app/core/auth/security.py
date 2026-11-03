from fastapi.openapi.models import OAuthFlowClientCredentials, OAuthFlowPassword, OAuthFlows
from fastapi.requests import HTTPConnection
from fastapi.security import OAuth2
from fastapi.security.utils import get_authorization_scheme_param

from .scopes import CLIENT_ONLY_SCOPES, Scope

TOKEN_URL = "/api/v1/auth/token"


class OAuth2Bearer(OAuth2):
    async def __call__(self, request: HTTPConnection) -> str | None:
        authorization = request.headers.get("Authorization")
        scheme, param = get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "bearer":
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None

        return param


oauth2_scheme = OAuth2Bearer(
    flows=OAuthFlows(
        clientCredentials=OAuthFlowClientCredentials(
            tokenUrl=TOKEN_URL,
            scopes={scope.value: scope.description for scope in Scope},
        ),
        # A person's login through a client: Swagger UI's "Authorize" dialog asks for the client's credentials
        # too. A client-only scope is never part of a person's token, so the flow does not offer one.
        password=OAuthFlowPassword(
            tokenUrl=TOKEN_URL,
            refreshUrl=TOKEN_URL,
            scopes={scope.value: scope.description for scope in Scope if scope not in CLIENT_ONLY_SCOPES},
        ),
    ),
    # Named explicitly: FastAPI would otherwise derive the key in ``components.securitySchemes``
    # from the class name, and a later rename of the class would silently change the contract.
    scheme_name="OAuth2",
    auto_error=False,
)
