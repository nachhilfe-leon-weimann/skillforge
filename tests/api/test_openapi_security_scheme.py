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
# P0-4 of docs/specs/bot-decoupling.md removed the bot API: its other 34 operations left the pins.
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


def test_neither_oauth2_flow_offers_a_bot_scope(schema: dict[str, Any]):
    """The bot scopes retired with the bot schema (bot-decoupling spec, P0-5)."""
    flows = schema["components"]["securitySchemes"][SCHEME_NAME]["flows"]

    assert set(flows) == {"clientCredentials", "password"}
    for name, flow in flows.items():
        assert flow["scopes"], name
        assert not [scope for scope in flow["scopes"] if scope.startswith("bot:")], name


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
