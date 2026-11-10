"""Outbound calls leave only through the guarded transport: no module in ``app/`` but it imports an HTTP client."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSPORT = "app/integrations/transport.py"
HTTP_CLIENTS = ("httpx", "httpcore", "requests", "aiohttp", "urllib3", "urllib.request", "http.client")


def _http_imports(source: str) -> set[str]:
    """Return the HTTP client modules a source file imports."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {
        module
        for module in modules
        if any(module == client or module.startswith(f"{client}.") for client in HTTP_CLIENTS)
    }


def test_only_the_transport_imports_an_http_client():
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): imports
        for path in (REPO_ROOT / "app").rglob("*.py")
        if path.relative_to(REPO_ROOT).as_posix() != TRANSPORT and (imports := _http_imports(path.read_text()))
    }

    assert offenders == {}
    assert "httpx" in _http_imports((REPO_ROOT / TRANSPORT).read_text())


def test_the_import_check_catches_every_import_form():
    assert _http_imports("import httpx") == {"httpx"}
    assert _http_imports("from httpx import AsyncClient") == {"httpx", "httpx.AsyncClient"}
    assert _http_imports("import urllib.request") == {"urllib.request"}
    assert _http_imports("from urllib import request") == {"urllib.request"}
    assert _http_imports("from http import client") == {"http.client"}
    assert _http_imports("import requests.adapters") == {"requests.adapters"}
    assert _http_imports("from urllib.parse import quote\nimport httpx_like\nfrom . import transport") == set()
