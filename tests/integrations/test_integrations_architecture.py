"""Outbound calls leave only through the guarded transport: no module in ``app/`` but it reaches for the network."""

import ast
import socket
from pathlib import Path

import pytest

from tests.network_guard import stays_local

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSPORT = "app/integrations/transport.py"
NETWORK_MODULES = (
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "socket",
    "ssl",
    "asyncio.open_connection",
    "importlib",
)
NETWORK_CALLS = frozenset({"open_connection", "create_connection", "sock_connect", "__import__"})
INTEGRATIONS_ALLOWED = ("app.core", "app.integrations")
CORE_TO_INTEGRATIONS: dict[str, tuple[str, ...]] = {
    "app/core/config.py": ("app.integrations.sevdesk.config", "app.integrations.graph.config")
}
"""The one direction the layering is crossed: a module of ``app/core`` that reads an integration's settings."""


def _imports(tree: ast.AST, *, package: str) -> set[str]:
    """The absolute module names a module imports, relative imports resolved, each ``from`` name included."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package.split(".")
            prefix = ".".join(base[: len(base) - (node.level - 1)]) if node.level else ""
            module = ".".join(part for part in (prefix, node.module or "") if part)
            modules.update({module, *(f"{module}.{alias.name}" for alias in node.names)})
    return modules


def _network_use(source: str, *, package: str) -> set[str]:
    """The network modules a source file imports - relative imports resolved - and the network calls it makes."""
    tree = ast.parse(source)
    found = {
        m for m in _imports(tree, package=package) if any(m == n or m.startswith(f"{n}.") for n in NETWORK_MODULES)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in NETWORK_CALLS:
            found.add(f"call .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in NETWORK_CALLS:
            found.add(f"call {node.id}")
    return found


def _app_imports_outside(source: str, *, package: str, allowed: tuple[str, ...]) -> set[str]:
    """The ``app`` modules a source file imports from outside ``allowed``."""
    return {
        module
        for module in _imports(ast.parse(source), package=package)
        if (module == "app" or module.startswith("app."))
        and not any(module == prefix or module.startswith(f"{prefix}.") for prefix in allowed)
    }


def _package(path: Path) -> str:
    return ".".join(path.parent.relative_to(REPO_ROOT).parts)


def test_only_the_transport_reaches_for_the_network():
    offenders = {}
    for path in (REPO_ROOT / "app").rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        if relative.as_posix() != TRANSPORT and (
            used := _network_use(path.read_text(), package=".".join(relative.parent.parts))
        ):
            offenders[relative.as_posix()] = used

    assert offenders == {}
    assert "httpx" in _network_use((REPO_ROOT / TRANSPORT).read_text(), package="app.integrations")


def test_the_integrations_import_nothing_but_the_core_and_themselves():
    files = sorted((REPO_ROOT / "app/integrations").rglob("*.py"))

    assert files
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): found
        for path in files
        if (found := _app_imports_outside(path.read_text(), package=_package(path), allowed=INTEGRATIONS_ALLOWED))
    }
    assert offenders == {}


def test_the_core_imports_no_integration_but_the_named_settings():
    offenders = {}
    for path in (REPO_ROOT / "app/core").rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        allowed = ("app.core", *CORE_TO_INTEGRATIONS.get(relative, ()))
        found = {
            module
            for module in _app_imports_outside(path.read_text(), package=_package(path), allowed=allowed)
            if module == "app.integrations" or module.startswith("app.integrations.")
        }
        if found:
            offenders[relative] = found
    assert offenders == {}


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("import app.services.billing", True),
        ("from app.services.crm import parties", True),
        ("from app.api.v1.common import Page", True),
        ("from ... import services", True),
        ("from ...workers import housekeeping", True),
        ("import app", True),
        ("from app.core.logging import get_logger", False),
        ("from ..errors import IntegrationError", False),
        ("from . import models\nimport httpx\nfrom pydantic import BaseModel", False),
    ],
)
def test_the_import_check_catches_every_form_of_an_outside_import(source: str, found: bool):
    assert bool(_app_imports_outside(source, package="app.integrations.sevdesk", allowed=INTEGRATIONS_ALLOWED)) is found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import httpx", {"httpx"}),
        ("import httpx2", {"httpx2"}),
        ("from httpcore2 import AsyncConnectionPool", {"httpcore2", "httpcore2.AsyncConnectionPool"}),
        ("from httpx import AsyncClient", {"httpx", "httpx.AsyncClient"}),
        ("import urllib.request", {"urllib.request"}),
        ("from urllib import request", {"urllib.request"}),
        ("from http import client", {"http.client"}),
        ("import socket", {"socket"}),
        ("import ssl", {"ssl"}),
        ("from asyncio import open_connection", {"asyncio.open_connection"}),
        ("import asyncio\nasyncio.open_connection('h', 1)", {"call .open_connection"}),
        ("import importlib\nimportlib.import_module('httpx')", {"importlib"}),
        ("__import__('httpx')", {"call __import__"}),
        ("from ..integrations import transport", set()),
        ("from ...integrations.transport import IntegrationHttp", set()),
        ("from urllib.parse import quote\nimport httpx_like\nfrom . import transport", set()),
    ],
)
def test_the_check_catches_every_form(source: str, expected: set[str]):
    assert _network_use(source, package="app.services.billing") == expected


@pytest.mark.parametrize(
    ("address", "local"),
    [
        ("/var/run/docker.sock", True),
        (("localhost", 5432), True),
        (("127.0.0.1", 5432), True),
        (("::1", 5432, 0, 0), True),
        (("192.168.1.10", 5432), True),
        (("172.17.0.2", 5432), True),
        (("::ffff:127.0.0.1", 5432, 0, 0), True),
        (("169.254.169.254", 80), False),
        (("fe80::1%en0", 80, 0, 1), False),
        (("100.64.0.1", 443), False),
        (("100.127.255.254", 443), False),
        (("93.184.215.14", 443), False),
        (("2606:4700::1111", 443, 0, 0), False),
        (("my.sevdesk.de", 443), False),
    ],
)
def test_the_tests_socket_guard_lets_only_local_addresses_through(address, local: bool):
    assert stays_local(address) is local


def test_the_tests_socket_guard_is_installed():
    assert socket.socket.connect.__name__ == "wrapper"
    assert socket.socket.connect_ex.__name__ == "wrapper"
