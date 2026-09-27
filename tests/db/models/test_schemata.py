"""`get_schemata`: the schemas `migrations/env.py` creates are the model packages (bot-decoupling, decision L)."""

from pathlib import Path

from app.core.db.models.schemata import get_schemata


def _package(root: Path, name: str) -> None:
    (root / name).mkdir()
    (root / name / "__init__.py").touch()


def test_every_model_package_is_a_schema(tmp_path: Path):
    _package(tmp_path, "primary")
    _package(tmp_path, "secondary")

    assert get_schemata(tmp_path) == {"primary", "secondary"}


def test_a_folder_without_an_init_is_no_schema(tmp_path: Path):
    """A deleted package leaves its `__pycache__` behind in a checkout; it must not bring its schema back."""
    _package(tmp_path, "core")
    (tmp_path / "bot" / "__pycache__").mkdir(parents=True)
    (tmp_path / "bot" / "__pycache__" / "job.cpython-314.pyc").touch()

    assert get_schemata(tmp_path) == {"core"}


def test_a_private_folder_and_a_module_are_no_schema(tmp_path: Path):
    _package(tmp_path, "core")
    _package(tmp_path, "_private")
    (tmp_path / "shared.py").touch()

    assert get_schemata(tmp_path) == {"core"}
