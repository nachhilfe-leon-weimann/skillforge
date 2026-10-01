from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.auth.scopes import BASE_OF

from .errors import VALIDATION_ERROR_CODE, VALIDATION_ERROR_DETAIL, code_for_status
from .schemas import ErrorResponse

ENDPOINT_SUFFIX = "_endpoint"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})
SCHEMA_REF_TEMPLATE = "#/components/schemas/{model}"

OPENAPI_TAGS: list[dict[str, Any]] = [
    {
        "name": "auth",
        "description": (
            "OAuth2 token issuance - client credentials for applications, password and refresh-token logins for "
            "people through a client, and the Discord-user exchange for a person's linked Discord user - plus "
            "logout, user accounts, Discord links and application client management."
        ),
    },
    {
        "name": "crm",
        "description": "Customer relationship management: parties and the data attached to them.",
    },
    {
        "name": "system",
        "description": "Service root plus liveness and health probes for dependencies and workers.",
    },
]

# Bodies raised by ``get_current_principal`` in ``app/core/auth/dependencies.py``.
UNAUTHORIZED_EXAMPLES: dict[str, dict[str, Any]] = {
    "missing_token": {"value": {"detail": "Not authenticated", "code": code_for_status(401)}},
    "invalid_token": {"value": {"detail": "Invalid authentication credentials", "code": code_for_status(401)}},
}
FORBIDDEN_EXAMPLES: dict[str, dict[str, Any]] = {
    "forbidden": {"value": {"detail": "Not enough permissions", "code": code_for_status(403)}},
}
VALIDATION_ERROR_EXAMPLE: dict[str, Any] = {
    "detail": VALIDATION_ERROR_DETAIL,
    "code": VALIDATION_ERROR_CODE,
    "errors": [
        {
            "loc": ["query", "limit"],
            "message": "Input should be less than or equal to 100",
            "type": "less_than_equal",
        }
    ],
}

# The schemas FastAPI generates for its built-in 422; replaced by the envelope (ADR 0006).
FRAMEWORK_VALIDATION_SCHEMAS = ("HTTPValidationError", "ValidationError")


def operation_id(route: APIRoute) -> str:
    """Build the operation ID ``{tag}_{function_name}`` for a route.

    The first tag is the domain (and the generated client's module). A trailing ``_endpoint`` and
    a leading ``{tag}_`` are stripped from the function name, so ``list_parties_endpoint`` and
    ``list_parties`` both become ``crm_list_parties`` and ``system_health_check`` does not stutter.
    """
    if not route.tags:
        raise RuntimeError(
            f"Route {route.path!r} ({route.name}) has no tag. Set exactly one domain tag on its router: "
            "the tag prefixes the operation ID and names the generated client's module."
        )

    tag = str(route.tags[0])
    name = route.name.removesuffix(ENDPOINT_SUFFIX).removeprefix(f"{tag}_")
    return f"{tag}_{name}"


def customize_openapi(app: FastAPI) -> None:
    """Wrap ``app.openapi`` with the post-processing that derives docs from declared facts.

    Call once, after all routers are included. Caching stays with FastAPI (``app.openapi_schema``,
    regenerated when routes change); a schema is post-processed exactly once, when FastAPI hands
    out a new one.

    The schema is built right away: FastAPI resolves included routers lazily, so without this a
    route rejected by ``operation_id`` would not fail at import but turn requests into 500s.
    """
    generate_openapi = app.openapi
    customized: dict[str, Any] | None = None

    def openapi() -> dict[str, Any]:
        nonlocal customized
        schema = generate_openapi()
        if schema is not customized:
            _register_error_envelope(schema)
            _derive_reach_alternatives(schema)
            _document_auth_errors(schema)
            _unify_validation_errors(schema)
            customized = schema
        return schema

    # Overriding the bound method is FastAPI's documented way to extend the schema.
    app.openapi = openapi  # ty: ignore[invalid-assignment]
    app.openapi()


def _register_error_envelope(schema: dict[str, Any]) -> None:
    """Make sure ``ErrorResponse`` (and what it nests) exists even if no route references it."""
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    envelope = ErrorResponse.model_json_schema(ref_template=SCHEMA_REF_TEMPLATE)
    for name, nested in envelope.pop("$defs", {}).items():
        schemas.setdefault(name, nested)
    schemas.setdefault(ErrorResponse.__name__, envelope)


def _derive_reach_alternatives(schema: dict[str, Any]) -> None:
    """Let the unqualified scope satisfy a requirement of its reach-qualified variant (``require_access``).

    FastAPI merges every marker on one scheme into one requirement, so the alternative is derived:
    a requirement naming a reach-qualified scope becomes one with its base in its place, then itself -
    ``[{"OAuth2": ["crm:read"]}, {"OAuth2": ["crm:read:own"]}]``.
    """
    for operation in _operations(schema):
        if operation.get("security"):
            operation["security"] = [
                alternative
                for requirement in operation["security"]
                for alternative in _reach_alternatives(requirement, operation_id=operation.get("operationId"))
            ]


def _reach_alternatives(requirement: dict[str, list[str]], *, operation_id: str | None) -> list[dict[str, list[str]]]:
    """Return ``requirement`` with each reach-qualified scope replaced by its base, then ``requirement`` itself.

    A requirement without a reach-qualified scope stays alone. One naming both ``x`` and its variant
    mixes ``require_scopes(x)`` with ``require_access(x)`` - a contradiction, refused when the schema
    is built.
    """
    for scopes in requirement.values():
        if mixed := {BASE_OF[scope] for scope in scopes if scope in BASE_OF} & set(scopes):
            raise RuntimeError(
                f"Operation {operation_id} demands {', '.join(sorted(mixed))} through require_scopes and "
                "require_access at once. Drop require_scopes: require_access admits both forms."
            )

    base = {scheme: [BASE_OF.get(scope, scope) for scope in scopes] for scheme, scopes in requirement.items()}
    return [requirement] if base == requirement else [base, requirement]


def _document_auth_errors(schema: dict[str, Any]) -> None:
    """Document 401 on every operation that declares a ``security`` requirement, and 403 if it names a scope.

    Requirements are alternatives. If one of them names no scope, any valid token passes, so the
    operation cannot answer 403.
    """
    for operation in _operations(schema):
        if not operation.get("security"):
            continue

        responses = operation.setdefault("responses", {})
        _document(responses, "401", "Missing or invalid bearer token", UNAUTHORIZED_EXAMPLES)
        alternatives = [_scopes_of(requirement) for requirement in operation["security"]]
        if all(alternatives):
            _document(responses, "403", _forbidden_description(alternatives), FORBIDDEN_EXAMPLES)


def _document(responses: dict[str, Any], status: str, description: str, examples: dict[str, dict[str, Any]]) -> None:
    """Set a derived error response; one the route declared via ``error_responses`` is kept next to it."""
    declared = responses.get(status, {})
    declared_examples = declared.get("content", {}).get("application/json", {}).get("examples")
    if declared_examples is None:
        responses[status] = _error_response(description, examples=examples)
        return

    responses[status] = _error_response(
        f"{description} / {declared['description']}",
        examples={**examples, **declared_examples},
    )


def _unify_validation_errors(schema: dict[str, Any]) -> None:
    """Replace FastAPI's auto-generated 422 with the envelope.

    A 422 a route declares itself stays as it is. FastAPI then omits its own 422, although a route
    that takes input can still fail request validation - so if the declared 422 lists ``examples``
    (as ``error_responses`` does), the validation example joins them.
    """
    framework_ref = {"$ref": SCHEMA_REF_TEMPLATE.format(model=FRAMEWORK_VALIDATION_SCHEMAS[0])}
    for operation in _operations(schema):
        content = operation.get("responses", {}).get("422", {}).get("content", {}).get("application/json", {})
        if content.get("schema") == framework_ref:
            operation["responses"]["422"] = _error_response(VALIDATION_ERROR_DETAIL, example=VALIDATION_ERROR_EXAMPLE)
        elif "examples" in content and (operation.get("parameters") or operation.get("requestBody")):
            content["examples"].setdefault(VALIDATION_ERROR_CODE, {"value": VALIDATION_ERROR_EXAMPLE})

    schemas = schema["components"]["schemas"]
    for name in FRAMEWORK_VALIDATION_SCHEMAS:
        if SCHEMA_REF_TEMPLATE.format(model=name) not in _refs(schema):
            schemas.pop(name, None)


def _operations(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                yield operation


def _refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        own = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        return own.union(*(_refs(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(_refs(value) for value in node))
    return set()


def _forbidden_description(alternatives: list[list[str]]) -> str:
    """Name what a token lacks: the scopes of one requirement are all needed, the requirements are alternatives."""
    noun = "scope" if all(len(scopes) == 1 for scopes in alternatives) else "scopes"
    return f"Missing required {noun}: {' or '.join(', '.join(scopes) for scopes in alternatives)}"


def _scopes_of(requirement: dict[str, list[str]]) -> list[str]:
    return list(dict.fromkeys(scope for scopes in requirement.values() for scope in scopes))


def _error_response(description: str, **content: Any) -> dict[str, Any]:
    schema_ref = {"$ref": SCHEMA_REF_TEMPLATE.format(model=ErrorResponse.__name__)}
    return {
        "description": description,
        "content": {"application/json": {"schema": schema_ref, **content}},
    }
