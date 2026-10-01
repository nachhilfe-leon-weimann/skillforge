from .config import AuthSettings
from .dependencies import get_current_principal, require_access, require_application, require_scopes
from .principal import (
    ApplicationPrincipal,
    AuthMethod,
    DiscordLogin,
    Login,
    PasswordLogin,
    Principal,
    PrincipalType,
    UserPrincipal,
)
from .reach import Access, ReachBasis
from .scopes import Scope
from .tokens import (
    CreatedAccessToken,
    TokenValidationError,
    create_access_token,
    create_application_access_token,
    validate_access_token,
)

__all__ = [
    "Access",
    "ApplicationPrincipal",
    "AuthMethod",
    "AuthSettings",
    "CreatedAccessToken",
    "DiscordLogin",
    "Login",
    "PasswordLogin",
    "Principal",
    "PrincipalType",
    "ReachBasis",
    "Scope",
    "TokenValidationError",
    "UserPrincipal",
    "create_access_token",
    "create_application_access_token",
    "get_current_principal",
    "require_access",
    "require_application",
    "require_scopes",
    "validate_access_token",
]
