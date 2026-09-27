"""The one security scheme of the contract: its key, and what every operation demands under it.

P0-5 of `docs/specs/user-authentication.md` renamed the scheme key from the class name FastAPI derived
(`OAuth2ClientCredentialsBearer`) to `OAuth2` - deliberately and once, while no consumer is live.
`SCOPES_AT_THE_RENAME` and `PUBLIC_AT_THE_RENAME` pin what the operations demanded before: the rename moved the
key and nothing else. A later slice that changes an operation's requirement on purpose drops it from the pins
(P0-7 did so for the two party reads), so together they name every operation at the rename but those.
"""

from typing import Any

import pytest

from app.main import app

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
SCHEME_NAME = "OAuth2"

# Every operation that existed at the rename, with the scopes of its one security requirement.
# A later slice that deliberately changes an operation's requirement removes that operation here in
# the same PR, with a comment naming the slice. Operations added after the rename are not pinned.
# P0-7 (own data) removed `GET /api/v1/crm/parties` and `GET /api/v1/crm/parties/{party_id}`: they accept
# `crm:read` or `crm:read:own` as two alternative requirements.
# P0-2 of docs/specs/bot-decoupling.md removed the two bot link routes: Discord links moved to
# /api/v1/auth/discord-links.
SCOPES_AT_THE_RENAME: dict[tuple[str, str], list[str]] = {
    ("GET", "/api/v1/auth/clients"): ["auth:clients:manage"],
    ("POST", "/api/v1/auth/clients"): ["auth:clients:manage"],
    ("GET", "/api/v1/auth/clients/{client_id}"): ["auth:clients:manage"],
    ("PATCH", "/api/v1/auth/clients/{client_id}"): ["auth:clients:manage"],
    ("POST", "/api/v1/auth/clients/{client_id}/scopes"): ["auth:clients:manage"],
    ("DELETE", "/api/v1/auth/clients/{client_id}/scopes/{mode}/{scope_key}"): ["auth:clients:manage"],
    ("POST", "/api/v1/auth/clients/{client_id}/secrets"): ["auth:clients:manage"],
    ("DELETE", "/api/v1/auth/clients/{client_id}/secrets/{secret_id}"): ["auth:clients:manage"],
    ("GET", "/api/v1/auth/me"): [],
    ("POST", "/api/v1/bot/authz/check"): ["bot:read"],
    ("PUT", "/api/v1/bot/command-envs"): ["bot:write"],
    ("DELETE", "/api/v1/bot/command-envs/{guild_id}/{channel_id}/{kind}"): ["bot:write"],
    ("GET", "/api/v1/bot/jobs"): ["bot:read"],
    ("POST", "/api/v1/bot/jobs/claim"): ["bot:write"],
    ("GET", "/api/v1/bot/jobs/summary"): ["bot:read"],
    ("GET", "/api/v1/bot/jobs/{job_id}"): ["bot:read"],
    ("POST", "/api/v1/bot/jobs/{job_id}/complete"): ["bot:write"],
    ("POST", "/api/v1/bot/jobs/{job_id}/fail"): ["bot:write"],
    ("GET", "/api/v1/bot/operations"): ["bot:read"],
    ("GET", "/api/v1/bot/operations/{operation_id}"): ["bot:read"],
    ("POST", "/api/v1/bot/operations/{operation_id}/cancel"): ["bot:write"],
    ("GET", "/api/v1/bot/runtime/command-envs/resolve"): ["bot:read"],
    ("POST", "/api/v1/bot/runtime/principals/batch"): ["bot:read"],
    ("GET", "/api/v1/bot/runtime/principals/{discord_id}"): ["bot:read"],
    ("POST", "/api/v1/bot/runtime/students/{guild_id}/batch"): ["bot:read"],
    ("GET", "/api/v1/bot/runtime/students/{guild_id}/{discord_id}"): ["bot:read"],
    ("POST", "/api/v1/bot/runtime/tutors/{guild_id}/batch"): ["bot:read"],
    ("GET", "/api/v1/bot/runtime/tutors/{guild_id}/{discord_id}"): ["bot:read"],
    ("POST", "/api/v1/bot/students/activations/prepare"): ["bot:write"],
    ("POST", "/api/v1/bot/students/activations/{operation_id}/commit"): ["bot:write"],
    ("POST", "/api/v1/bot/students/{guild_id}/{student_discord_id}/deactivate/prepare"): ["bot:write"],
    ("POST", "/api/v1/bot/students/{guild_id}/{student_discord_id}/deactivate/{operation_id}/commit"): ["bot:write"],
    ("POST", "/api/v1/bot/students/{guild_id}/{student_discord_id}/pop/prepare"): ["bot:write"],
    ("POST", "/api/v1/bot/students/{guild_id}/{student_discord_id}/pop/{operation_id}/commit"): ["bot:write"],
    ("POST", "/api/v1/bot/students/{guild_id}/{student_discord_id}/stash/prepare"): ["bot:write"],
    ("POST", "/api/v1/bot/students/{guild_id}/{student_discord_id}/stash/{operation_id}/commit"): ["bot:write"],
    ("POST", "/api/v1/bot/tutors/activations/prepare"): ["bot:write"],
    ("POST", "/api/v1/bot/tutors/activations/{operation_id}/commit"): ["bot:write"],
    ("POST", "/api/v1/bot/tutors/{guild_id}/{tutor_discord_id}/deactivate/prepare"): ["bot:write"],
    ("POST", "/api/v1/bot/tutors/{guild_id}/{tutor_discord_id}/deactivate/{operation_id}/commit"): ["bot:write"],
    ("PUT", "/api/v1/bot/users/{discord_id}"): ["bot:write"],
    ("DELETE", "/api/v1/bot/users/{discord_id}/groups/{group_key}"): ["bot:write"],
    ("PUT", "/api/v1/bot/users/{discord_id}/groups/{group_key}"): ["bot:write"],
    ("POST", "/api/v1/crm/companies"): ["crm:write"],
    ("PATCH", "/api/v1/crm/companies/{party_id}"): ["crm:write"],
    ("DELETE", "/api/v1/crm/parties/{party_id}"): ["crm:write"],
    ("POST", "/api/v1/crm/parties/{party_id}/contact-infos"): ["crm:write"],
    ("DELETE", "/api/v1/crm/parties/{party_id}/contact-infos/{contact_info_id}"): ["crm:write"],
    ("PATCH", "/api/v1/crm/parties/{party_id}/contact-infos/{contact_info_id}"): ["crm:write"],
    ("GET", "/api/v1/crm/parties/{party_id}/relations"): ["crm:read"],
    ("DELETE", "/api/v1/crm/parties/{party_id}/relations/{type}/{to_party_id}"): ["crm:write"],
    ("PUT", "/api/v1/crm/parties/{party_id}/relations/{type}/{to_party_id}"): ["crm:write"],
    ("POST", "/api/v1/crm/persons"): ["crm:write"],
    ("PATCH", "/api/v1/crm/persons/{party_id}"): ["crm:write"],
    ("DELETE", "/api/v1/crm/persons/{party_id}/student"): ["crm:write"],
    ("PUT", "/api/v1/crm/persons/{party_id}/student"): ["crm:write"],
    ("DELETE", "/api/v1/crm/persons/{party_id}/tutor"): ["crm:write"],
    ("PUT", "/api/v1/crm/persons/{party_id}/tutor"): ["crm:write"],
    ("GET", "/api/v1/crm/subjects"): ["crm:read"],
    ("POST", "/api/v1/crm/subjects"): ["crm:write"],
    ("DELETE", "/api/v1/crm/subjects/{subject_id}"): ["crm:write"],
    ("PATCH", "/api/v1/crm/subjects/{subject_id}"): ["crm:write"],
}

# Every operation that was public at the rename: FastAPI renders it without a `security` key. A later
# slice that deliberately guards one removes it here in the same PR, with a comment naming the slice.
# Together the two pins name every operation at the rename that no later slice changed on purpose.
PUBLIC_AT_THE_RENAME: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/"),
    ("POST", "/api/v1/auth/token"),
    ("GET", "/health"),
    ("GET", "/health/dependencies"),
    ("GET", "/health/dependencies/{dependency_name}"),
    ("GET", "/health/live"),
    ("GET", "/health/workers"),
    ("GET", "/health/workers/{worker_name}"),
})


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return app.openapi()


def test_the_contract_declares_exactly_one_security_scheme(schema: dict[str, Any]):
    assert set(schema["components"]["securitySchemes"]) == {SCHEME_NAME}


def test_every_secured_operation_references_only_that_scheme(schema: dict[str, Any]):
    secured = {operation: security for operation, security in _security_requirements(schema).items() if security}

    assert secured
    for operation, security in secured.items():
        assert all(set(requirement) == {SCHEME_NAME} for requirement in security), operation


@pytest.mark.parametrize(
    ("operation", "scopes"),
    SCOPES_AT_THE_RENAME.items(),
    ids=[" ".join(operation) for operation in SCOPES_AT_THE_RENAME],
)
def test_the_rename_left_the_security_requirement_unchanged(
    schema: dict[str, Any], operation: tuple[str, str], scopes: list[str]
):
    assert _security_requirements(schema).get(operation) == [{SCHEME_NAME: scopes}]


@pytest.mark.parametrize("operation", sorted(PUBLIC_AT_THE_RENAME), ids=" ".join)
def test_the_rename_left_the_public_operations_public(schema: dict[str, Any], operation: tuple[str, str]):
    assert "security" not in _operations(schema)[operation]


def _operations(schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Every operation of the contract, keyed by ``(METHOD, path)``."""
    return {
        (method.upper(), path): operation
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def _security_requirements(schema: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, list[str]]]]:
    """The ``security`` list of every operation, keyed by ``(METHOD, path)``; empty for a public one."""
    return {key: operation.get("security", []) for key, operation in _operations(schema).items()}
