"""The guards that keep a test run off real systems.

Under pytest nothing reaches the network and ``live`` mode is refused - unless the contract run is
active (``CONTRACT_RUN_ENV=1``) against a ``trial`` account. A process counts as under pytest when pytest is
imported or set ``PYTEST_VERSION``, which a subprocess of a test inherits.
"""

import os
import sys

from .config import IntegrationAccount
from .errors import IntegrationRefusedError

CONTRACT_RUN_ENV = "SKILLFORGE_CONTRACT_RUN"


def running_under_pytest() -> bool:
    """Whether this process runs tests or was started by one; no SkillForge process outside tests does either."""
    return "pytest" in sys.modules or "PYTEST_VERSION" in os.environ


def contract_run_active() -> bool:
    """Whether this process is a contract run."""
    return os.environ.get(CONTRACT_RUN_ENV) == "1"


def ensure_live_allowed(account: IntegrationAccount) -> None:
    """Refuse ``live`` mode under pytest, except in the contract run against a ``trial`` account."""
    _ensure_contract_run_on_trial(account, "live mode")


def ensure_network_allowed(account: IntegrationAccount) -> None:
    """Refuse real network under pytest, except in the contract run against a ``trial`` account."""
    _ensure_contract_run_on_trial(account, "real network")


def _ensure_contract_run_on_trial(account: IntegrationAccount, what: str) -> None:
    if not running_under_pytest():
        return
    if not contract_run_active():
        raise IntegrationRefusedError(f"{what} is refused under pytest without {CONTRACT_RUN_ENV}=1")
    if account is not IntegrationAccount.TRIAL:
        raise IntegrationRefusedError(f"{what} under pytest is allowed for a trial account only")
