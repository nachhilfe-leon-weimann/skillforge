"""Import boundaries of the auth domain.

The auth core (``app/core/auth``: what validates a request) depends on no service and no API module - a
standing criterion of the user-authentication spec. The auth services (``app/services/auth``) depend on no
API module and on no other domain's services, as the CRM does (ADR 0009).
"""

from tests.api.test_crm_architecture import REPO_ROOT, _imported_modules

AUTH_CORE = "app/core/auth"
AUTH_SERVICES = "app/services/auth"
FORBIDDEN_FOR_THE_CORE = ("app.services", "app.api")
FORBIDDEN_FOR_THE_SERVICES = ("app.api", "app.services.crm", "app.services.system")


def _violations(source: str, *, package: str, forbidden: tuple[str, ...]) -> set[str]:
    return {
        module
        for module in _imported_modules(source, package=package)
        if any(module == name or module.startswith(f"{name}.") for name in forbidden)
    }


def _assert_no_violations(directory: str, forbidden: tuple[str, ...]) -> None:
    files = list((REPO_ROOT / directory).rglob("*.py"))

    assert files
    for path in files:
        package = ".".join(path.parent.relative_to(REPO_ROOT).parts)
        assert _violations(path.read_text(), package=package, forbidden=forbidden) == set(), path.relative_to(REPO_ROOT)


def test_the_auth_core_imports_neither_services_nor_the_api():
    _assert_no_violations(AUTH_CORE, FORBIDDEN_FOR_THE_CORE)


def test_the_auth_services_import_neither_the_api_nor_another_domain():
    _assert_no_violations(AUTH_SERVICES, FORBIDDEN_FOR_THE_SERVICES)


def test_the_check_catches_absolute_and_relative_imports():
    core = {"package": "app.core.auth", "forbidden": FORBIDDEN_FOR_THE_CORE}
    services = {"package": "app.services.auth", "forbidden": FORBIDDEN_FOR_THE_SERVICES}

    assert _violations("from app.services.auth import tokens", **core)
    assert _violations("import app.api.v1.common", **core)
    assert _violations("from ...api.v1 import common", **core)
    assert _violations("from .scopes import Scope\nfrom app.core.db.models import UserAccount", **core) == set()

    assert _violations("from app.services.crm import parties", **services)
    assert _violations("from ..system import heartbeat_service", **services)
    assert _violations("from ...api.v1 import common", **services)
    assert _violations("from .tokens import issue_client_token\nfrom app.core.auth import Scope", **services) == set()
