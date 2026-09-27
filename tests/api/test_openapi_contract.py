"""Contract-level assertions over the generated OpenAPI document."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.api.v1.common import ErrorResponse
from app.core.auth import Scope
from app.main import app

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
OPERATION_ID_PATTERN = re.compile(r"(auth|bot|crm|system)_[a-z0-9_]+")
COMMITTED_OPENAPI_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return app.openapi()


def test_committed_contract_survives_a_javascript_round_trip():
    """release-please's `json` extra-file updater rewrites the whole file with JavaScript's
    JSON.parse + JSON.stringify. JavaScript has one numeric type, so an integer-valued float like
    `50.0` comes back as `50`, and `just openapi-check` would then find the file stale on every
    release PR."""
    text = COMMITTED_OPENAPI_PATH.read_text()
    float_tokens: list[str] = []
    json.loads(text, parse_float=float_tokens.append)

    integer_valued = [token for token in float_tokens if float(token).is_integer()]
    assert integer_valued == []


def _operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, operation)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def test_security_scheme_lists_every_scope_with_its_description(schema: dict[str, Any]):
    flows = [scheme["flows"] for scheme in schema["components"]["securitySchemes"].values() if "flows" in scheme]

    assert flows
    for flow in flows:
        assert flow["clientCredentials"]["scopes"] == {scope.value: scope.description for scope in Scope}


def test_every_operation_id_is_prefixed_with_its_domain_tag(schema: dict[str, Any]):
    operation_ids = [operation["operationId"] for _, _, operation in _operations(schema)]

    assert operation_ids
    for operation_id in operation_ids:
        assert OPERATION_ID_PATTERN.fullmatch(operation_id), operation_id


def test_operation_ids_are_unique(schema: dict[str, Any]):
    operation_ids = [operation["operationId"] for _, _, operation in _operations(schema)]

    assert len(operation_ids) == len(set(operation_ids))


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/v1/auth/token", "auth_create_token"),
        ("GET", "/api/v1/bot/jobs", "bot_list_jobs"),
        ("GET", "/health", "system_health_check"),
        ("GET", "/health/live", "system_liveness_check"),
        ("GET", "/", "system_root"),
    ],
)
def test_operation_id_spot_checks(schema: dict[str, Any], method: str, path: str, expected: str):
    assert schema["paths"][path][method.lower()]["operationId"] == expected


def test_every_operation_has_exactly_one_documented_domain_tag(schema: dict[str, Any]):
    documented = {tag["name"]: tag.get("description", "") for tag in schema.get("tags", [])}

    for method, path, operation in _operations(schema):
        assert len(operation.get("tags", [])) == 1, f"{method} {path}"
        assert documented.get(operation["tags"][0], "").strip(), f"{method} {path}"


def test_bearer_auth_errors_are_documented_exactly_where_a_token_is_required(schema: dict[str, Any]):
    guarded = 0
    for method, path, operation in _operations(schema):
        responses = operation["responses"]
        if operation.get("security"):
            guarded += 1
            assert _documents_bearer_401(responses), f"{method} {path}"
            if _requires_a_scope(operation):
                assert _documents_scope_403(responses), f"{method} {path}"
            else:
                # Any valid token passes (``GET /auth/me``), so there is no 403 to document.
                assert "403" not in responses, f"{method} {path}"
        else:
            # An unguarded operation may own a 401 (the token endpoint's ``invalid_client``), but
            # never the derived bearer-token one, and no 403.
            assert not _documents_bearer_401(responses), f"{method} {path}"
            assert "403" not in responses, f"{method} {path}"

    assert guarded


def _requires_a_scope(operation: dict[str, Any]) -> bool:
    return any(scopes for requirement in operation["security"] for scopes in requirement.values())


def _documents_bearer_401(responses: dict[str, Any]) -> bool:
    examples = responses.get("401", {}).get("content", {}).get("application/json", {}).get("examples", {})
    return {"missing_token", "invalid_token"} <= set(examples)


def _documents_scope_403(responses: dict[str, Any]) -> bool:
    forbidden = responses.get("403", {})
    examples = forbidden.get("content", {}).get("application/json", {}).get("examples", {})
    return "forbidden" in examples and "Missing required scope" in forbidden.get("description", "")


def test_forbidden_response_names_the_required_scope(schema: dict[str, Any]):
    forbidden = schema["paths"]["/api/v1/bot/jobs"]["get"]["responses"]["403"]

    assert "bot:read" in forbidden["description"]


def test_framework_validation_schemas_are_not_part_of_the_contract(schema: dict[str, Any]):
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "ValidationError" not in schema["components"]["schemas"]


def test_contract_has_no_dangling_refs(schema: dict[str, Any]):
    known = {f"#/components/schemas/{name}" for name in schema["components"]["schemas"]}

    assert _refs(schema) - known == set()


def test_every_documented_error_body_is_the_envelope(schema: dict[str, Any]):
    envelope = {"$ref": "#/components/schemas/ErrorResponse"}
    checked = 0
    for method, path, operation in _operations(schema):
        for status, response in operation["responses"].items():
            if status.startswith("2") or "content" not in response:
                continue
            checked += 1
            assert response["content"]["application/json"]["schema"] == envelope, f"{method} {path} {status}"

    assert checked


PAGED_ENDPOINTS = {
    "/api/v1/bot/jobs": ("Page_JobListItem_", {"status", "kind"}),
    "/api/v1/bot/operations": ("Page_OperationSummary_", {"guild_id", "subject_discord_id", "status", "kind"}),
    "/api/v1/auth/clients": ("Page_ApplicationClientResponse_", set()),
    "/api/v1/auth/users": ("Page_UserAccountListItem_", {"status", "party_id", "email"}),
    "/api/v1/auth/discord-links": ("Page_DiscordLink_", {"updated_since", "party_id", "active"}),
}


def test_no_operation_returns_a_bare_array(schema: dict[str, Any]):
    bare = [
        f"{method} {path}"
        for method, path, operation in _operations(schema)
        for status, response in operation["responses"].items()
        if status.startswith("2")
        and response.get("content", {}).get("application/json", {}).get("schema", {}).get("type") == "array"
    ]

    # The job claim hands out a batch of work, not a page of a list.
    assert bare == ["POST /api/v1/bot/jobs/claim"]


@pytest.mark.parametrize("path", PAGED_ENDPOINTS)
def test_paged_endpoint_documents_limit_and_offset(schema: dict[str, Any], path: str):
    parameters = {parameter["name"]: parameter for parameter in schema["paths"][path]["get"]["parameters"]}

    limit, offset = parameters["limit"], parameters["offset"]
    assert limit["description"]
    assert offset["description"]
    assert (limit["schema"]["default"], limit["schema"]["minimum"], limit["schema"]["maximum"]) == (50, 1, 100)
    assert (offset["schema"]["default"], offset["schema"]["minimum"]) == (0, 0)


@pytest.mark.parametrize(("path", "expected"), PAGED_ENDPOINTS.items())
def test_paged_endpoint_keeps_its_filters_and_describes_them(
    schema: dict[str, Any], path: str, expected: tuple[str, set[str]]
):
    _, filters = expected
    parameters = {parameter["name"]: parameter for parameter in schema["paths"][path]["get"]["parameters"]}

    assert set(parameters) == filters | {"limit", "offset"}
    for name in filters:
        assert parameters[name]["in"] == "query"
        assert not parameters[name]["required"]
        assert parameters[name]["description"], name


@pytest.mark.parametrize(("path", "expected"), PAGED_ENDPOINTS.items())
def test_paged_endpoint_returns_the_generic_page(schema: dict[str, Any], path: str, expected: tuple[str, set[str]]):
    page, _ = expected
    response = schema["paths"][path]["get"]["responses"]["200"]

    assert response["content"]["application/json"]["schema"] == {"$ref": f"#/components/schemas/{page}"}
    assert schema["components"]["schemas"][page]["required"] == ["items", "total", "limit", "offset"]


def test_domain_specific_page_schemas_are_gone(schema: dict[str, Any]):
    assert "JobPage" not in schema["components"]["schemas"]
    assert "OperationPage" not in schema["components"]["schemas"]


def test_every_documented_error_example_is_a_valid_envelope(schema: dict[str, Any]):
    examples = [
        (f"{method} {path} {status}", example)
        for method, path, operation in _operations(schema)
        for status, response in operation["responses"].items()
        if not status.startswith("2")
        for example in _examples(response)
    ]

    assert len(examples) > 100
    for where, example in examples:
        envelope = ErrorResponse.model_validate(example)
        assert envelope.code, where


def _examples(response: dict[str, Any]) -> list[dict[str, Any]]:
    content = response.get("content", {}).get("application/json", {})
    named = [example["value"] for example in content.get("examples", {}).values()]
    return [*named, *([content["example"]] if "example" in content else [])]


def _refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        own = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        return own.union(*(_refs(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(_refs(value) for value in node))
    return set()


def test_auth_client_operations_document_auth_errors(schema: dict[str, Any]):
    responses = schema["paths"]["/api/v1/auth/clients"]["get"]["responses"]

    assert "auth:clients:manage" in responses["403"]["description"]
    assert responses["401"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}
