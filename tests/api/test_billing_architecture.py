"""The billing domain's borders (billing spec, standing criteria) and the shape of its endpoints.

The services import only the core, the integrations, the CRM services and themselves; the endpoints carry no
boilerplate, by the CRM's check; and no route deletes a lesson (money rule 14).
"""

import ast
import re
from pathlib import Path
from typing import TypeIs

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.db.models import SevdeskContact
from app.main import app
from tests.api.test_crm_architecture import _boilerplate, _endpoints, _imported_modules

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES = REPO_ROOT / "app/services/billing"
API_PACKAGE = REPO_ROOT / "app/api/v1/billing"
LESSONS = "/api/v1/billing/lessons"
ALLOWED = ("app.core", "app.integrations", "app.services.crm", "app.services.billing")
# The one writer of ext.sevdesk_contact (billing spec, SF3-1), and the models that define the table and relate to it.
SEVDESK_CONTACT_WRITER = REPO_ROOT / "app/services/billing/payers.py"
MODELS = REPO_ROOT / "app/core/db/models"


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

    assert endpoints == [
        "list_lessons",
        "create_lesson",
        "get_lesson",
        "update_lesson",
        "get_student_payer",
        "resolve_lesson_payer",
        "put_sevdesk_contact",
        "delete_sevdesk_contact",
        "put_manual_billing",
        "delete_manual_billing",
        "list_rates",
        "put_rate",
        "delete_rate",
    ]
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


# The table named in SQL text: qualified by its schema, quoted or not, or right after a keyword that writes or reads.
SEVDESK_CONTACT_IN_TEXT = re.compile(
    r'\bext"?\s*\.\s*"?sevdesk_contact\b|\b(?:from|into|update|join|table)\s+"?sevdesk_contact\b', re.IGNORECASE
)
TABLE_CONSTRUCTORS = {"table", "Table", "TableClause"}


def _sevdesk_contact_uses(source: str) -> list[str]:
    """Return what in a source file could write ``ext.sevdesk_contact``.

    Flagged: the model under any name it is imported as, unless it is a type annotation or the owner of a column
    used as an operand (``SevdeskContact.party_id == ...``, the CRM's delete guard) - so a construction, ``insert``,
    ``update``, ``delete``, ``select`` (whose rows could be changed), ``__table__`` and a column's ``.table`` are
    flagged; the party's relationship ``.sevdesk_contact``; the table built by name (``table("sevdesk_contact")``);
    and the table named in SQL text (``SEVDESK_CONTACT_IN_TEXT``), docstrings aside.

    A static check cannot see: a name built at run time (string formatting, ``getattr``), the table unqualified in
    SQL text outside the keywords above (a ``search_path`` that includes ``ext``), the table reached by iterating the
    metadata, or a write through an instance some other path loaded without naming the model.
    """
    tree = ast.parse(source)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    imported = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    names = {"SevdeskContact"} | {name for name, original in imported.items() if original == "SevdeskContact"}
    constructors = TABLE_CONSTRUCTORS | {name for name, original in imported.items() if original in TABLE_CONSTRUCTORS}
    columns = set(SevdeskContact.__table__.columns.keys())

    def is_model(node: ast.AST) -> TypeIs[ast.Name | ast.Attribute]:
        return (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr == "SevdeskContact"
        )

    def is_operand(column: ast.Attribute) -> bool:
        """A column read as it is - not the receiver of another attribute such as ``.table``."""
        parent = parents.get(id(column))
        return not (isinstance(parent, ast.Attribute) and parent.value is column)

    annotations = [
        node.annotation for node in ast.walk(tree) if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation
    ] + [
        node.returns
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns
    ]
    reads = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in columns and is_model(node.value) and is_operand(node)
    } | {id(node) for annotation in annotations for node in ast.walk(annotation)}
    found = [
        f"SevdeskContact at line {node.lineno}" for node in ast.walk(tree) if is_model(node) and id(node) not in reads
    ]
    found += [
        f"Party.sevdesk_contact at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "sevdesk_contact"
    ]
    found += [
        f"the table built by name at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in constructors)
            or (isinstance(node.func, ast.Attribute) and node.func.attr in TABLE_CONSTRUCTORS)
        )
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "sevdesk_contact"
    ]
    # A string that is a statement of its own - a docstring - does nothing.
    docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    found += [
        f"the table in text at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and SEVDESK_CONTACT_IN_TEXT.search(node.value)
        and id(node) not in docstrings
    ]
    return found


def test_only_the_payer_service_writes_sevdesk_contacts():
    """``app/services/billing/payers.py`` is the one writer of ``ext.sevdesk_contact`` (billing spec, SF3-1)."""
    files = [path for path in sorted((REPO_ROOT / "app").rglob("*.py")) if MODELS not in path.parents]

    assert SEVDESK_CONTACT_WRITER in files
    assert _sevdesk_contact_uses(SEVDESK_CONTACT_WRITER.read_text()), "the check sees the writer's writes"
    for path in files:
        if path != SEVDESK_CONTACT_WRITER:
            assert _sevdesk_contact_uses(path.read_text()) == [], path.relative_to(REPO_ROOT)


@pytest.mark.parametrize(
    "source",
    [
        "session.add(SevdeskContact(sevdesk_id='1', party_id=party_id))",
        "insert(SevdeskContact).values(sevdesk_id='1', party_id=party_id)",
        "update(SevdeskContact).where(SevdeskContact.party_id == party_id).values(sevdesk_id='2')",
        "delete(SevdeskContact).where(SevdeskContact.party_id == party_id)",
        "contact = await session.scalar(select(SevdeskContact))\ncontact.sevdesk_id = '2'",
        "from app.core.db.models import SevdeskContact as Contact\nsession.add(Contact(sevdesk_id='1'))",
        "from app.core.db import models\nsession.add(models.SevdeskContact(sevdesk_id='1'))",
        "delete(SevdeskContact.__table__)",
        "party.sevdesk_contact = None",
        "await session.execute(text('DELETE FROM ext.sevdesk_contact'))",
        "update(SevdeskContact.party_id.table).values(sevdesk_id='2')",
        'await session.execute(text(\'DELETE FROM "ext"."sevdesk_contact" WHERE party_id = :p\'))',
        "await session.execute(text('UPDATE sevdesk_contact SET sevdesk_id = :s'))",
        "from sqlalchemy import table as sa_table\ndelete(sa_table('sevdesk_contact', schema='ext'))",
        "delete(sqlalchemy.table('sevdesk_contact', schema='ext'))",
        "metadata.tables['ext.sevdesk_contact'].delete()",
    ],
)
def test_the_sevdesk_contact_check_catches_every_write_form(source: str):
    assert _sevdesk_contact_uses(source)


def test_the_sevdesk_contact_check_lets_a_column_read_pass():
    read = (
        "from app.core.db.models import SevdeskContact\n"
        "exists().where(SevdeskContact.party_id == party_id)\n"
        "ExternalLink('sevdesk_contact', SevdeskContact.party_id, true())\n"
        "def from_model(contact: SevdeskContact) -> SevdeskContact:\n"
        '    """The one writer of ``ext.sevdesk_contact``."""'
    )

    assert _sevdesk_contact_uses(read) == []
