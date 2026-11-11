"""The billing domain's borders (billing spec, standing criteria) and the shape of its endpoints.

The services import only the core, the integrations, the CRM services and themselves; the endpoints carry no
boilerplate, by the CRM's check; and no route deletes a lesson (money rule 14).
"""

import ast
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.api.test_crm_architecture import _boilerplate, _endpoints, _imported_modules

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES = REPO_ROOT / "app/services/billing"
API_PACKAGE = REPO_ROOT / "app/api/v1/billing"
LESSONS = "/api/v1/billing/lessons"
ALLOWED = ("app.core", "app.integrations", "app.services.crm", "app.services.billing")


def _violations(source: str, *, package: str) -> set[str]:
    """Return the ``app`` modules a source file imports from outside ``ALLOWED``."""
    return {
        module
        for module in _imported_modules(source, package=package)
        if (module == "app" or module.startswith("app."))
        and not any(module == allowed or module.startswith(f"{allowed}.") for allowed in ALLOWED)
    }


def test_the_billing_services_import_nothing_but_the_core_the_integrations_the_crm_and_themselves():
    files = sorted(SERVICES.rglob("*.py"))

    assert {path.name for path in files} >= {"__init__.py", "errors.py", "lessons.py"}
    for path in files:
        package = ".".join(path.parent.relative_to(REPO_ROOT).parts)
        assert _violations(path.read_text(), package=package) == set(), path.relative_to(REPO_ROOT)


def test_the_billing_import_check_catches_every_import_form():
    package = "app.services.billing"

    assert _violations("from app.api.v1.common import Page", package=package)
    assert _violations("import app.services.auth.users", package=package)
    assert _violations("from app.services import system", package=package)
    assert _violations("from app.workers import housekeeping", package=package)
    assert _violations("from .. import auth", package=package)
    assert _violations("import app", package=package)
    allowed = (
        "from . import errors\nfrom .errors import LessonNotFoundError\nfrom app.core.db.models import Lesson\n"
        "from app.integrations.sevdesk import invoices\nfrom app.services.crm.parties import load_party\n"
        "from ..crm import parties\nimport sqlalchemy"
    )
    assert _violations(allowed, package=package) == set()


def test_the_billing_endpoints_carry_no_boilerplate():
    modules = sorted(API_PACKAGE.glob("*.py"))
    endpoints = [endpoint.name for module in modules for endpoint in _endpoints(ast.parse(module.read_text()))]

    assert endpoints == ["list_lessons", "create_lesson", "get_lesson", "update_lesson"]
    for module in modules:
        assert _boilerplate(module.read_text()) == [], module.name


@pytest.mark.parametrize("path", [LESSONS, f"{LESSONS}/00000000-0000-0000-0000-0000000000aa"])
async def test_no_route_deletes_a_lesson(path: str):
    """Money rule 14: "remove" is `not_held`; only the calendar import deletes, and never through a route.

    Both the contract and the running app are asked, so a route left out of the schema cannot hide.
    """
    documented = {
        method for template, item in app.openapi()["paths"].items() if template.startswith(LESSONS) for method in item
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.delete(path)

    assert documented == {"get", "post", "patch"}
    assert response.status_code == 405
