"""compose's worker runs a module that exists: a rename of the worker cannot leave the service pointing nowhere."""

import importlib.util
import json
import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yml"


def _worker_block() -> str:
    """The lines of the `worker` service, up to the next top-level service."""
    after = COMPOSE.read_text().split("\n  worker:\n", 1)[1]
    return re.split(r"\n  [a-z]", after, maxsplit=1)[0]


def test_the_worker_runs_the_housekeeping_module():
    line = next(line for line in _worker_block().splitlines() if line.strip().startswith("command:"))
    command = json.loads(re.sub(r"^\s*command:\s*", "", line))

    assert command == ["python", "-m", "app.workers.housekeeping"]
    assert importlib.util.find_spec(command[-1]) is not None


def test_the_worker_disables_the_image_healthcheck_it_never_serves():
    assert re.search(r"healthcheck:\s*\n\s+disable: true", _worker_block())
