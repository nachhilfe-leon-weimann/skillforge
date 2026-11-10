"""The guards that keep a test run off real systems.

Under pytest nothing reaches the network and ``live`` mode is refused - unless the contract run is
active (``CONTRACT_RUN_ENV=1``), and then ``live`` only for a ``trial`` account.
"""

import os
import sys

from .config import IntegrationAccount
from .errors import IntegrationRefusedError

CONTRACT_RUN_ENV = "SKILLFORGE_CONTRACT_RUN"


def running_under_pytest() -> bool:
    """Whether this process runs tests: pytest is imported, which no SkillForge process outside tests does."""
    return "pytest" in sys.modules


def contract_run_active() -> bool:
    """Whether this process is a contract run against a trial account."""
    return os.environ.get(CONTRACT_RUN_ENV) == "1"


def ensure_live_allowed(account: IntegrationAccount) -> None:
    """Refuse ``live`` mode under pytest, except in the contract run against a ``trial`` account."""
    if not running_under_pytest():
        return
    if not contract_run_active():
        raise IntegrationRefusedError(f"live mode is refused under pytest without {CONTRACT_RUN_ENV}=1")
    if account is not IntegrationAccount.TRIAL:
        raise IntegrationRefusedError("live mode under pytest is allowed for a trial account only")


def ensure_network_allowed() -> None:
    """Refuse real network under pytest, except in the contract run."""
    if running_under_pytest() and not contract_run_active():
        raise IntegrationRefusedError("real network is refused under pytest; use httpx.MockTransport")
