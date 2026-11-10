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


def _network_use(source: str, *, package: str) -> set[str]:
    """The network modules a source file imports - relative imports resolved - and the network calls it makes."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        modules: set[str] = set()
        if isinstance(node, ast.Import):
            modules = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = package.split(".")
            prefix = ".".join(base[: len(base) - (node.level - 1)]) if node.level else ""
            module = ".".join(part for part in (prefix, node.module or "") if part)
            modules = {module, *(f"{module}.{alias.name}" for alias in node.names)}
        elif isinstance(node, ast.Attribute) and node.attr in NETWORK_CALLS:
            found.add(f"call .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in NETWORK_CALLS:
            found.add(f"call {node.id}")
        found.update(m for m in modules if any(m == n or m.startswith(f"{n}.") for n in NETWORK_MODULES))
    return found


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
