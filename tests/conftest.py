import os
import socket

import pytest

from app.integrations.guards import CONTRACT_RUN_ENV, contract_run_active
from tests.network_guard import stays_local

os.environ.setdefault("AUTH__SECRET_KEY", "test-signing-secret-with-at-least-32-bytes")
os.environ.setdefault("DB__URL", "postgresql+asyncpg://skillforge:skillforge@localhost:5432/skillforge")

SKIP_CONTRACT = pytest.mark.skip(reason=f"contract tests run only in a contract run ({CONTRACT_RUN_ENV}=1)")


def pytest_configure(config):
    # The contract run reaches a real account: it runs the contract tests and nothing else, never by accident.
    if contract_run_active() and config.getoption("markexpr") != "contract":
        raise pytest.UsageError(f"{CONTRACT_RUN_ENV}=1 is set: it is for `pytest -m contract` only; unset it")


def pytest_collection_modifyitems(config, items):
    contract_run = contract_run_active()
    for item in items:
        if "session" in item.fixturenames:
            if not item.get_closest_marker("db"):
                raise pytest.UsageError(f"{item.name} uses DB but is missing @pytest.mark.db")
        if item.get_closest_marker("contract") and not contract_run:
            item.add_marker(SKIP_CONTRACT)


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Outside the contract run no test opens a connection to the internet, whatever the code under test does."""
    if contract_run_active():
        return
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded(original):
        def wrapper(sock: socket.socket, address):
            if not stays_local(address):
                raise ConnectionRefusedError(f"tests may not reach the internet: {address!r}")
            return original(sock, address)

        return wrapper

    monkeypatch.setattr(socket.socket, "connect", guarded(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(connect_ex))
