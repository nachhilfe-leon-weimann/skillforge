"""The job queue knows no domain (calendar-categories spec, "Layers" and the standing criteria): the modules of
``app/services/jobs/`` import nothing of ``app`` but ``app.core`` and themselves."""

from pathlib import Path

import pytest

from tests.api.test_crm_architecture import _imported_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
JOBS = REPO_ROOT / "app/services/jobs"
PACKAGE = "app.services.jobs"
ALLOWED = ("app.core", PACKAGE)


def _violations(source: str, *, package: str = PACKAGE) -> set[str]:
    """Return the ``app`` modules a source file imports from outside ``ALLOWED``."""
    return {
        module
        for module in _imported_modules(source, package=package)
        if (module == "app" or module.startswith("app."))
        and not any(module == allowed or module.startswith(f"{allowed}.") for allowed in ALLOWED)
    }


def test_the_job_services_import_nothing_but_the_core_and_themselves():
    files = sorted(JOBS.rglob("*.py"))

    assert {path.name for path in files} >= {"__init__.py", "queue.py"}
    for path in files:
        package = ".".join(path.parent.relative_to(REPO_ROOT).parts)
        assert _violations(path.read_text(), package=package) == set(), path.relative_to(REPO_ROOT)


@pytest.mark.parametrize(
    "source",
    [
        "from app.services.calendar import jobs",
        "import app.services.billing.lessons",
        "from app.services import system",
        "from app.integrations.graph import client",
        "from app.api.v1.common import Page",
        "from app.workers import housekeeping",
        "from .. import crm",
        "import app",
    ],
)
def test_the_import_check_catches_every_import_form(source: str):
    assert _violations(source)


def test_the_import_check_lets_the_core_and_the_package_pass():
    allowed = (
        "from app.core.db.models import Job\nfrom . import queue\nfrom .queue import claim\n"
        "import sqlalchemy\nfrom datetime import timedelta"
    )

    assert _violations(allowed) == set()
