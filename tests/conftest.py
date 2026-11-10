import os

import pytest

from app.integrations.guards import CONTRACT_RUN_ENV, contract_run_active

os.environ.setdefault("AUTH__SECRET_KEY", "test-signing-secret-with-at-least-32-bytes")
os.environ.setdefault("DB__URL", "postgresql+asyncpg://skillforge:skillforge@localhost:5432/skillforge")

SKIP_CONTRACT = pytest.mark.skip(reason=f"contract tests run only in a contract run ({CONTRACT_RUN_ENV}=1)")
SKIP_OUTSIDE_CONTRACT = pytest.mark.skip(reason="the contract run runs contract tests only")


def pytest_collection_modifyitems(config, items):
    contract_run = contract_run_active()
    for item in items:
        if "session" in item.fixturenames:
            if not item.get_closest_marker("db"):
                raise pytest.UsageError(f"{item.name} uses DB but is missing @pytest.mark.db")
        # Contract tests reach the sevDesk trial account; every other run skips them, and they run alone.
        if (item.get_closest_marker("contract") is not None) != contract_run:
            item.add_marker(SKIP_OUTSIDE_CONTRACT if contract_run else SKIP_CONTRACT)
