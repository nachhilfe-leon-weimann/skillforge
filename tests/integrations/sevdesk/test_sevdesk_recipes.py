"""The sevDesk recipes and the contract run's connect, without sevDesk: `uv` is a stand-in that records its call.

Every run points ``SKILLFORGE_SEVDESK_TRIAL_ENV`` at a temporary synthetic file, so no test reads the real one.
"""

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.integrations.errors import IdentityCheckError
from tests.contract.sevdesk_trial import connect_trial
from tests.integrations.sevdesk.fake_sevdesk import FakeSevdesk, objects

ROOT = Path(__file__).resolve().parents[3]
FAKE_UV = '#!/bin/sh\necho "SKILLFORGE_CONTRACT_RUN=${SKILLFORGE_CONTRACT_RUN:-} uv $*" >> "$UV_CALLS"\n'


class Recipes:
    def __init__(self, tmp_path: Path) -> None:
        self.env_file = tmp_path / "sevdesk-trial.env"
        self.calls = tmp_path / "uv-calls"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "uv").write_text(FAKE_UV)
        (bin_dir / "uv").chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["just", "--justfile", str(ROOT / "justfile"), *arguments], capture_output=True, text=True
        )

    def uv_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def recipes(tmp_path: Path, monkeypatch) -> Recipes:
    if shutil.which("just") is None:
        pytest.skip("needs just")
    recipes = Recipes(tmp_path)
    for name in [name for name in os.environ if name.upper().startswith("SEVDESK__")] + ["CI"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", recipes.path)
    monkeypatch.setenv("UV_CALLS", str(recipes.calls))
    monkeypatch.setenv("SKILLFORGE_SEVDESK_TRIAL_ENV", str(recipes.env_file))
    return recipes


@pytest.fixture
def trial_env_file(recipes: Recipes) -> Path:
    recipes.env_file.write_text("SEVDESK__ACCOUNT=trial\n")
    return recipes.env_file


TRIAL_RECIPES = {"contract-sevdesk": ("contract-sevdesk",), "sevdesk-doctor trial": ("sevdesk-doctor", "trial")}
ALL_RECIPES = {**TRIAL_RECIPES, "sevdesk-doctor live": ("sevdesk-doctor", "live")}


@pytest.mark.parametrize("recipe", ALL_RECIPES.values(), ids=ALL_RECIPES.keys())
def test_no_recipe_runs_in_ci(recipe: tuple[str, ...], recipes: Recipes, trial_env_file, monkeypatch):
    monkeypatch.setenv("CI", "true")

    result = recipes.run(*recipe)

    assert result.returncode == 1 and "never runs in CI" in result.stderr
    assert recipes.uv_calls() == []


@pytest.mark.parametrize("variable", ["SEVDESK__TOKEN", "sevdesk__account", "Sevdesk__Mode"])
@pytest.mark.parametrize("recipe", TRIAL_RECIPES.values(), ids=TRIAL_RECIPES.keys())
def test_a_trial_recipe_refuses_sevdesk_variables_of_the_shell_in_any_case(
    recipe, variable: str, recipes: Recipes, trial_env_file, monkeypatch
):
    monkeypatch.setenv(variable, "synthetic-shell-value")

    result = recipes.run(*recipe)

    assert result.returncode == 1 and "unset the SEVDESK__ variables" in result.stderr
    assert "synthetic-shell-value" not in result.stdout + result.stderr
    assert recipes.uv_calls() == []


def test_the_shell_variable_check_holds_with_many_variables(recipes: Recipes, trial_env_file, monkeypatch):
    """With more names than a pipe holds, a check that stops reading early would break its pipe and pass."""
    monkeypatch.setenv("SEVDESK__TOKEN", "synthetic-shell-value")
    for number in range(4000):
        monkeypatch.setenv(f"ZZ_SYNTHETIC_PADDING_VARIABLE_{number:05d}", "")

    result = recipes.run("contract-sevdesk")

    assert result.returncode == 1 and "unset the SEVDESK__ variables" in result.stderr
    assert recipes.uv_calls() == []


@pytest.mark.parametrize("recipe", TRIAL_RECIPES.values(), ids=TRIAL_RECIPES.keys())
def test_a_trial_recipe_refuses_an_env_file_path_with_whitespace(recipe, recipes: Recipes, tmp_path, monkeypatch):
    spaced = tmp_path / "trial settings" / "sevdesk-trial.env"
    spaced.parent.mkdir()
    spaced.write_text("SEVDESK__ACCOUNT=trial\n")
    monkeypatch.setenv("SKILLFORGE_SEVDESK_TRIAL_ENV", str(spaced))

    result = recipes.run(*recipe)

    assert result.returncode == 1 and "must not contain whitespace" in result.stderr
    assert recipes.uv_calls() == []


@pytest.mark.parametrize("inside", ["sevdesk-trial.env", "tests/sevdesk-trial.env", "tests/../sevdesk-trial.env"])
@pytest.mark.parametrize("recipe", TRIAL_RECIPES.values(), ids=TRIAL_RECIPES.keys())
def test_a_trial_recipe_refuses_an_env_file_inside_the_repository(recipe, inside: str, recipes: Recipes, monkeypatch):
    # The file need not exist: the recipe refuses the path before it looks for the file.
    monkeypatch.setenv("SKILLFORGE_SEVDESK_TRIAL_ENV", str(ROOT / inside))

    result = recipes.run(*recipe)

    assert result.returncode == 1 and "must live outside the repository" in result.stderr
    assert recipes.uv_calls() == []


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_a_trial_recipe_refuses_a_file_symlink_into_the_repository(
    relative: bool, recipes: Recipes, tmp_path, monkeypatch
):
    (tmp_path / "repository").symlink_to(ROOT, target_is_directory=True)
    (tmp_path / "config").mkdir()
    link = tmp_path / "config" / "sevdesk-trial.env"
    # A relative target counts from the link's directory, never from where the recipe runs.
    link.symlink_to("../repository/tests/sevdesk-trial.env" if relative else ROOT / "tests" / "sevdesk-trial.env")
    monkeypatch.setenv("SKILLFORGE_SEVDESK_TRIAL_ENV", str(link))

    result = recipes.run("contract-sevdesk")

    assert result.returncode == 1 and "must live outside the repository" in result.stderr
    assert recipes.uv_calls() == []


def test_a_trial_recipe_refuses_an_env_file_in_any_git_work_tree(recipes: Recipes, tmp_path, monkeypatch):
    other = tmp_path / "another-repository"
    (other / "config").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    env_file = other / "config" / "sevdesk-trial.env"
    env_file.write_text("SEVDESK__ACCOUNT=trial\n")
    monkeypatch.setenv("SKILLFORGE_SEVDESK_TRIAL_ENV", str(env_file))

    result = recipes.run("contract-sevdesk")

    assert result.returncode == 1 and "must live outside any git work tree" in result.stderr
    assert recipes.uv_calls() == []


def test_git_ignores_a_trial_env_file_anywhere_in_the_repository():
    paths = ["sevdesk-trial.env", "tests/sevdesk-trial.env", "scripts/my-trial.env"]
    result = subprocess.run(["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True)

    assert result.stdout.split() == paths


@pytest.mark.parametrize("linked", ["env file", "justfile"])
def test_a_trial_recipe_sees_through_a_symlink_to_the_repository(linked: str, recipes: Recipes, tmp_path, monkeypatch):
    link = tmp_path / "linked-repository"
    link.symlink_to(ROOT, target_is_directory=True)
    env_file = (link if linked == "env file" else ROOT) / "sevdesk-trial.env"
    justfile = (link if linked == "justfile" else ROOT) / "justfile"
    monkeypatch.setenv("SKILLFORGE_SEVDESK_TRIAL_ENV", str(env_file))

    result = subprocess.run(["just", "--justfile", str(justfile), "contract-sevdesk"], capture_output=True, text=True)

    assert result.returncode == 1 and "must live outside the repository" in result.stderr
    assert recipes.uv_calls() == []


def test_the_contract_run_without_a_trial_env_file_runs_nothing_and_passes(recipes: Recipes):
    result = recipes.run("contract-sevdesk")

    assert result.returncode == 0 and "no trial configured" in result.stdout
    assert recipes.uv_calls() == []


def test_the_doctor_without_a_trial_env_file_fails(recipes: Recipes):
    result = recipes.run("sevdesk-doctor", "trial")

    assert result.returncode == 1 and "no trial configured" in result.stderr
    assert recipes.uv_calls() == []


def test_the_contract_run_loads_the_trial_env_file_and_runs_the_contract_tests_only(recipes: Recipes, trial_env_file):
    result = recipes.run("contract-sevdesk")

    assert result.returncode == 0, result.stderr
    assert recipes.uv_calls() == [f"SKILLFORGE_CONTRACT_RUN=1 uv run --env-file {trial_env_file} pytest -m contract"]


def test_the_doctor_loads_the_trial_env_file_for_the_trial_only(recipes: Recipes, trial_env_file, monkeypatch):
    assert recipes.run("sevdesk-doctor", "trial", "--discover").returncode == 0
    monkeypatch.setenv("SEVDESK__ACCOUNT", "live")
    assert recipes.run("sevdesk-doctor", "live").returncode == 0

    assert recipes.uv_calls() == [
        f"SKILLFORGE_CONTRACT_RUN= uv run --env-file {trial_env_file} python -m app.cli.sevdesk_doctor --account trial"
        " --discover",
        "SKILLFORGE_CONTRACT_RUN= uv run python -m app.cli.sevdesk_doctor --account live",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ("prod",),
        ("trial", "--account", "live"),
        ("trial", "--account=live"),
        ("live", "--account", "trial"),
        ("trial", "--discover", "--discover"),
        ("trial", "extra"),
        ("trial", "live"),
    ],
)
def test_the_doctor_takes_trial_or_live_and_at_most_discover(arguments: tuple[str, ...], recipes, trial_env_file):
    result = recipes.run("sevdesk-doctor", *arguments)

    assert result.returncode == 1 and "trial or live" in result.stderr
    assert recipes.uv_calls() == []


@pytest.mark.parametrize(
    ("recipe", "selection"),
    [("test", "not contract"), ("test-coverage", "not contract"), ("test-without-db", "not db and not contract")],
)
def test_the_test_recipes_deselect_the_contract_tests(recipe: str, selection: str, recipes: Recipes):
    """``check`` runs ``test-without-db``, ``check-all`` runs ``test``, CI runs ``test-coverage``."""
    assert f'pytest -m "{selection}"' in recipes.run("--show", recipe).stdout


# --- the contract run's connect ---


async def test_an_expired_trial_skips_the_contract_run(settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/SevClient", httpx.Response(401, json={"error": {"message": "Authentication required"}}))

    with pytest.raises(pytest.skip.Exception, match="trial expired"):
        async with connect_trial(settings, transport=fake_sevdesk.transport):
            pass


@pytest.mark.parametrize(
    "answer",
    [objects([{"id": "4712", "name": "Example TRIAL"}]), httpx.Response(500), httpx.Response(403)],
    ids=["another account", "a server error", "a forbidden token"],
)
async def test_any_other_failed_check_fails_the_contract_run(answer, settings, fake_sevdesk: FakeSevdesk):
    fake_sevdesk.on("GET", "/SevClient", answer)

    # A skip is no exception but an outcome: catch it too, or a wrong skip would pass as a skipped test.
    with pytest.raises((IdentityCheckError, pytest.skip.Exception)) as raised:
        async with connect_trial(settings, transport=fake_sevdesk.transport):
            pass

    assert isinstance(raised.value, IdentityCheckError)
