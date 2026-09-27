"""The contract of the account, redeem and Discord link routes in the generated OpenAPI document."""

import re
from typing import Any

import pytest

from app.main import app

HTTP_METHODS = {"get", "put", "post", "delete", "patch"}
AUTH_OPERATION_ID = re.compile(r"^auth_[a-z_]+$")
SCHEMA_REF_PREFIX = "#/components/schemas/"
PREFIX = "/api/v1/auth"

# The route map of the spec for this slice: method, path, operation ID, success status and scope.
ROUTE_MAP: dict[tuple[str, str], tuple[str, str, str]] = {
    ("POST", "/users"): ("auth_create_user", "201", "auth:users:manage"),
    ("GET", "/users"): ("auth_list_users", "200", "auth:users:manage"),
    ("GET", "/users/{user_id}"): ("auth_get_user", "200", "auth:users:manage"),
    ("PATCH", "/users/{user_id}"): ("auth_update_user", "200", "auth:users:manage"),
    ("PUT", "/users/{user_id}/roles/{role}"): ("auth_add_user_role", "200", "auth:users:manage"),
    ("DELETE", "/users/{user_id}/roles/{role}"): ("auth_remove_user_role", "204", "auth:users:manage"),
    ("POST", "/users/{user_id}/invitation"): ("auth_issue_invitation", "201", "auth:users:manage"),
    ("POST", "/users/{user_id}/password-reset"): ("auth_issue_password_reset", "201", "auth:users:manage"),
    ("DELETE", "/users/{user_id}/sessions"): ("auth_revoke_user_sessions", "204", "auth:users:manage"),
    ("POST", "/password/redeem"): ("auth_redeem_password", "204", "auth:users:login"),
    ("GET", "/discord-links"): ("auth_list_discord_links", "200", "auth:discord-links:read"),
    ("GET", "/discord-links/{discord_user_id}"): ("auth_get_discord_link", "200", "auth:discord-links:read"),
    ("PUT", "/discord-links/{discord_user_id}"): ("auth_link_discord_account", "200", "auth:users:manage"),
    ("DELETE", "/discord-links/{discord_user_id}"): ("auth_unlink_discord_account", "204", "auth:users:manage"),
}
# The error codes each operation declares next to the derived 401/403 and the validation 422.
DECLARED_CODES: dict[str, set[str]] = {
    "auth_create_user": {
        "unknown_account_party",
        "account_party_not_a_person",
        "user_account_already_exists",
        "user_email_already_in_use",
    },
    "auth_list_users": set(),
    "auth_get_user": {"user_account_not_found"},
    "auth_update_user": {"user_account_not_found", "user_email_already_in_use", "user_account_state"},
    "auth_add_user_role": {"user_account_not_found"},
    "auth_remove_user_role": {"user_account_not_found", "user_role_not_found"},
    "auth_issue_invitation": {"user_account_not_found", "user_account_state"},
    "auth_issue_password_reset": {"user_account_not_found", "user_account_state"},
    "auth_revoke_user_sessions": {"user_account_not_found"},
    "auth_redeem_password": {"invalid_action_token", "weak_password"},
    "auth_list_discord_links": set(),
    "auth_get_discord_link": {"discord_link_not_found"},
    "auth_link_discord_account": {"unknown_link_party", "link_party_not_a_person", "discord_account_already_linked"},
    "auth_unlink_discord_account": {"discord_link_not_found"},
}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return app.openapi()


def _operations(schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    paths = {path for _, path in ROUTE_MAP}
    return {
        (method.upper(), path.removeprefix(PREFIX)): operation
        for path, item in schema["paths"].items()
        if path.removeprefix(PREFIX) in paths
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def _refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        own = {node["$ref"].removeprefix(SCHEMA_REF_PREFIX)} if isinstance(node.get("$ref"), str) else set()
        return own.union(*(_refs(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(_refs(value) for value in node))
    return set()


def test_the_routes_are_exactly_the_route_map_of_the_spec(schema: dict[str, Any]):
    operations = _operations(schema)

    assert {key: operation["operationId"] for key, operation in operations.items()} == {
        key: operation_id for key, (operation_id, _, _) in ROUTE_MAP.items()
    }
    assert all(AUTH_OPERATION_ID.match(operation["operationId"]) for operation in operations.values())


@pytest.mark.parametrize(("key", "expected"), ROUTE_MAP.items(), ids=[" ".join(key) for key in ROUTE_MAP])
def test_each_operation_answers_its_status_under_its_scope(
    schema: dict[str, Any], key: tuple[str, str], expected: tuple[str, str, str]
):
    operation = _operations(schema)[key]
    operation_id, success, scope = expected

    assert success in operation["responses"]
    assert operation["security"] == [{"OAuth2": [scope]}]
    documented = {
        code
        for status, response in operation["responses"].items()
        if status in {"404", "409", "422"}
        for code in response["content"]["application/json"].get("examples", {})
    }
    assert documented - {"validation_error"} == DECLARED_CODES[operation_id]


def test_every_property_and_parameter_of_the_new_operations_is_described(schema: dict[str, Any]):
    operations = _operations(schema).values()
    components = schema["components"]["schemas"]
    pending = set().union(*(_refs(operation) for operation in operations)) - {"ErrorResponse", "FieldError"}
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        seen.add(name)
        component = components[name]
        for field, definition in component.get("properties", {}).items():
            assert definition.get("description"), f"{name}.{field}"
        pending |= _refs(component) - seen - {"ErrorResponse", "FieldError"}

    parameters = [parameter for operation in operations for parameter in operation.get("parameters", [])]
    assert {"UserAccountCreateRequest", "UserAccountDetail", "ActionTokenResponse", "PasswordRedeemRequest"} <= seen
    assert parameters
    assert all(parameter["description"] for parameter in parameters)
