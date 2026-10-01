"""ADR 0009: the CRM depends on no other domain - it imports the core, the shared API vocabulary and itself."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CRM_PACKAGES = ("app/services/crm", "app/api/v1/crm")
ALLOWED = ("app.core", "app.api.v1.common", "app.services.crm", "app.api.v1.crm")


def _imported_modules(source: str, *, package: str) -> set[str]:
    """Return the absolute module names a source file imports, resolving relative imports."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package.split(".")
            prefix = ".".join(base[: len(base) - (node.level - 1)]) if node.level else ""
            module = ".".join(part for part in (prefix, node.module or "") if part)
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names)
    return modules


def _violations(source: str, *, package: str) -> set[str]:
    """Return the ``app`` modules a source file imports from outside ``ALLOWED``."""
    return {
        module
        for module in _imported_modules(source, package=package)
        if (module == "app" or module.startswith("app."))
        and not any(module == allowed or module.startswith(f"{allowed}.") for allowed in ALLOWED)
    }


def test_the_crm_imports_nothing_but_the_core_and_itself():
    files = [path for package in CRM_PACKAGES for path in (REPO_ROOT / package).rglob("*.py")]

    assert {path.parent.relative_to(REPO_ROOT).as_posix() for path in files} >= set(CRM_PACKAGES)
    for path in files:
        package = ".".join(path.parent.relative_to(REPO_ROOT).parts)
        assert _violations(path.read_text(), package=package) == set(), path.relative_to(REPO_ROOT)


def test_the_import_check_catches_every_import_form():
    package = "app.services.crm"

    assert _violations("import app.services.auth.users", package=package)
    assert _violations("from app.services.auth import users", package=package)
    assert _violations("from app.services import system", package=package)
    assert _violations("from app.api.v1.auth.schemas import DiscordLink", package=package)
    assert _violations("from app.workers import housekeeping", package=package)
    assert _violations("from .. import auth", package=package)
    assert _violations("from ..system.health_service import check_health", package=package)
    assert _violations("import app", package=package)
    assert _violations("from . import subjects\nfrom app.core.errors import NotFoundError", package=package) == set()
    assert _violations("from app.api.v1.common import DBSession\nimport sqlalchemy", package=package) == set()


# --- standing criteria of the CRM API spec: what an endpoint module must not contain ---

API_PACKAGE = REPO_ROOT / "app/api/v1/crm"
ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete"}


def _endpoints(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in ROUTE_DECORATORS
            for decorator in node.decorator_list
        )
    ]


# Where the type of an endpoint parameter may come from: the CRM's own vocabulary and the shared one.
ALIAS_SOURCES = {".params", ".schemas", "app.api.v1.common"}


def _boilerplate(source: str) -> list[str]:
    tree = ast.parse(source)
    found = [f"try/except at line {node.lineno}" for node in ast.walk(tree) if isinstance(node, (ast.Try, ast.TryStar))]
    mentions = {
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "HTTPException")
        or (isinstance(node, ast.Attribute) and node.attr == "HTTPException")
        or (isinstance(node, ast.alias) and node.name == "HTTPException")
    }
    found += [f"HTTPException at line {line}" for line in sorted(mentions)]
    imported_from = {
        alias.asname or alias.name: "." * node.level + (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    for endpoint in _endpoints(tree):
        # A default is where an inline Path(), Query() or Depends() would hide.
        if endpoint.args.defaults or any(default is not None for default in endpoint.args.kw_defaults):
            found.append(f"{endpoint.name}: a parameter has a default")
        if endpoint.args.vararg or endpoint.args.kwarg:
            found.append(f"{endpoint.name}: takes *args or **kwargs")
        for argument in [*endpoint.args.posonlyargs, *endpoint.args.args, *endpoint.args.kwonlyargs]:
            if argument.arg == "_":
                found.append(f"{endpoint.name}: parameter named _")
            annotation = argument.annotation
            # Positive check: a bare name that the module imports from the vocabulary. Anything
            # spelled out in the signature - Annotated[...], typing.Annotated[...], uuid.UUID - is not.
            is_alias = isinstance(annotation, ast.Name) and (
                imported_from.get(annotation.id) in ALIAS_SOURCES
                or (annotation.id == "Response" and imported_from.get("Response") == "fastapi")
            )
            if not is_alias:
                found.append(f"{endpoint.name}: parameter {argument.arg} is not typed by an alias")
    return found


def test_the_crm_endpoints_carry_no_boilerplate():
    modules = sorted(API_PACKAGE.glob("*.py"))
    endpoints = [endpoint.name for module in modules for endpoint in _endpoints(ast.parse(module.read_text()))]

    assert len(endpoints) == 21, endpoints
    for module in modules:
        assert _boilerplate(module.read_text()) == [], module.name


def test_the_boilerplate_check_catches_each_kind():
    offending = """
import typing as t
import uuid
import fastapi
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from .params import PartyId
router = APIRouter()

@router.get("/{party_id}")
async def get_party(party_id: Annotated[int, Path()], _: object, session):
    try:
        return 1
    except ValueError:
        raise HTTPException(404)

@router.get("/other/{party_id}")
async def other(party_id: uuid.UUID = Path(), limit: int = Query(50), extra: t.Annotated[int, Query()] = 1):
    raise fastapi.HTTPException(404)

@router.get("/fine/{party_id}")
async def fine(party_id: PartyId):
    return 1
"""

    assert _boilerplate(offending) == [
        "try/except at line 12",
        "HTTPException at line 6",
        "HTTPException at line 15",
        "HTTPException at line 19",
        "get_party: parameter party_id is not typed by an alias",
        "get_party: parameter named _",
        "get_party: parameter _ is not typed by an alias",
        "get_party: parameter session is not typed by an alias",
        "other: a parameter has a default",
        "other: parameter party_id is not typed by an alias",
        "other: parameter limit is not typed by an alias",
        "other: parameter extra is not typed by an alias",
    ]
    assert _boilerplate("async def helper(x: int): ...") == []


def test_the_alias_check_rejects_a_session_that_bypasses_dbsession():
    """Decision N lives in the ``DBSession`` alias: a hand-rolled ``Depends(get_db_session)`` would lose it."""
    bypass = """
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db.dependencies import get_db_session
router = APIRouter()

@router.post("")
async def create(session: AsyncSession = Depends(get_db_session)):
    return 1
"""

    assert _boilerplate(bypass) == [
        "create: a parameter has a default",
        "create: parameter session is not typed by an alias",
    ]
