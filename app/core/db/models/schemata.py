from pathlib import Path

MODELS_DIR = Path(__file__).parent


def get_schemata(path: str | Path = MODELS_DIR) -> frozenset[str]:
    """Return the model packages under ``path``: one database schema each, created by ``migrations/env.py``.

    Only a folder with an ``__init__.py`` counts, so the ``__pycache__`` a deleted package leaves behind in a
    checkout never brings its schema back; a name starting with an underscore never counts.
    """
    return frozenset(
        entry.name
        for entry in Path(path).iterdir()
        if entry.is_dir() and not entry.name.startswith("_") and (entry / "__init__.py").is_file()
    )
