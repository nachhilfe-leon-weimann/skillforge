"""Guards the error taxonomy across all domains: every service error is mapped and uniquely coded."""

import importlib
import pkgutil

import app
from app.api.v1.common import status_for
from app.core import errors as taxonomy
from app.core.errors import ConflictError, DomainError, NotFoundError


class UnmappedProbeError(DomainError):
    pass


class FirstProbeError(NotFoundError):
    code = "probe_clash"


class SecondProbeError(ConflictError):
    code = "probe_clash"


def test_every_service_error_is_mapped_and_has_a_unique_code():
    error_modules = _import_error_modules()
    service_errors = [error for error in _subclasses(DomainError) if error.__module__.startswith("app.")]

    assert {"app.services.auth.errors", "app.services.crm.errors"} <= set(error_modules)
    assert _violations(service_errors) == []


def test_violations_reports_an_unmapped_error():
    assert _violations([UnmappedProbeError]) == ["UnmappedProbeError is not mapped to a status code"]


def test_violations_reports_a_code_used_twice():
    assert _violations([FirstProbeError, SecondProbeError]) == [
        "code 'probe_clash' is used by FirstProbeError and SecondProbeError"
    ]


def _import_error_modules() -> list[str]:
    names = [
        module.name
        for module in pkgutil.walk_packages(app.__path__, prefix="app.")
        if module.name.rsplit(".", 1)[-1] == "errors"
    ]
    for name in names:
        importlib.import_module(name)
    return names


def _subclasses(error_type: type[DomainError]) -> list[type[DomainError]]:
    found: list[type[DomainError]] = []
    for subclass in error_type.__subclasses__():
        found.append(subclass)
        found.extend(_subclasses(subclass))
    return found


def _violations(error_types: list[type[DomainError]]) -> list[str]:
    """Check concrete errors only: the categories in ``app/core/errors.py`` are the mapping itself."""
    concrete = [error for error in dict.fromkeys(error_types) if error.__module__ != taxonomy.__name__]
    violations: list[str] = []
    owners: dict[str, str] = {}
    for error in concrete:
        try:
            status_for(error)
        except LookupError:
            violations.append(f"{error.__name__} is not mapped to a status code")

        if error.code in owners:
            violations.append(f"code {error.code!r} is used by {owners[error.code]} and {error.__name__}")
        owners.setdefault(error.code, error.__name__)

    return violations
