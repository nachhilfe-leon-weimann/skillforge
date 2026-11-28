"""compose's workers run modules that exist: a rename of a worker cannot leave its service pointing nowhere."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yml"

# Service -> the module its command runs.
WORKERS = {"worker": "app.workers.housekeeping", "jobs": "app.workers.jobs"}


def _service_block(name: str) -> str:
    """The lines of a service, up to the next top-level service."""
    after = COMPOSE.read_text().split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z]", after, maxsplit=1)[0]


@pytest.mark.parametrize(("service", "module"), WORKERS.items())
def test_the_worker_runs_its_module(service: str, module: str):
    line = next(line for line in _service_block(service).splitlines() if line.strip().startswith("command:"))
    command = json.loads(re.sub(r"^\s*command:\s*", "", line))

    assert command == ["python", "-m", module]
    assert importlib.util.find_spec(command[-1]) is not None


@pytest.mark.parametrize("service", WORKERS)
def test_the_worker_disables_the_image_healthcheck_it_never_serves(service: str):
    assert re.search(r"healthcheck:\s*\n\s+disable: true", _service_block(service))
