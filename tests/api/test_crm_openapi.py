"""Contract-level assertions over the CRM part of the generated OpenAPI document."""

import re
from typing import Any

import pytest

from app.core.auth import Scope
from app.main import app

HTTP_METHODS = {"get", "put", "post", "delete", "patch"}
CRM_PREFIX = "/api/v1/crm/"
CRM_OPERATION_ID = re.compile(r"crm_[a-z_]+")
SCHEMA_REF_PREFIX = "#/components/schemas/"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return app.openapi()


def _crm_operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, operation)
        for path, item in schema["paths"].items()
        if path.startswith(CRM_PREFIX)
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def _refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        own = {node["$ref"].removeprefix(SCHEMA_REF_PREFIX)} if isinstance(node.get("$ref"), str) else set()
        return own.union(*(_refs(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(_refs(value) for value in node))
    return set()


def _crm_schema_names(schema: dict[str, Any]) -> set[str]:
    """Every component schema reachable from a CRM operation."""
    schemas = schema["components"]["schemas"]
    pending = set().union(*(_refs(operation) for _, _, operation in _crm_operations(schema)))
    reachable: set[str] = set()
    while pending:
        name = pending.pop()
        if name not in reachable:
            reachable.add(name)
            pending |= _refs(schemas[name])
    return reachable


def test_both_crm_scopes_are_listed_with_their_description(schema: dict[str, Any]):
    flows = [scheme["flows"] for scheme in schema["components"]["securitySchemes"].values() if "flows" in scheme]

    assert flows
    for flow in flows:
        scopes = flow["clientCredentials"]["scopes"]
        assert scopes["crm:read"] == Scope.CRM_READ.description
        assert scopes["crm:write"] == Scope.CRM_WRITE.description
        assert scopes["crm:read"].strip() and scopes["crm:write"].strip()


def test_every_crm_operation_id_is_crm_prefixed_snake_case(schema: dict[str, Any]):
    operations = _crm_operations(schema)

    assert operations
    for method, path, operation in operations:
        assert CRM_OPERATION_ID.fullmatch(operation["operationId"]), f"{method} {path}"
        assert operation["tags"] == ["crm"], f"{method} {path}"


# The reach-aware reads (P0-7 of the user-authentication spec) accept `crm:read` or `crm:read:own`.
REACH_AWARE_OPERATIONS = {"crm_list_parties", "crm_get_party"}


def test_crm_reads_require_the_read_scope_and_everything_else_the_write_scope(schema: dict[str, Any]):
    for method, path, operation in _crm_operations(schema):
        requirements = [scopes for requirement in operation["security"] for scopes in requirement.values()]
        if operation["operationId"] in REACH_AWARE_OPERATIONS:
            assert requirements == [["crm:read"], ["crm:read:own"]], f"{method} {path}"
            continue
        required = [scope for scopes in requirements for scope in scopes]
        expected = ["crm:read"] if method == "GET" else ["crm:write"]
        assert required == expected, f"{method} {path}"


def test_the_403_of_every_other_operation_names_its_one_requirement_as_before(schema: dict[str, Any]):
    """Alternatives are derived only where `require_access` put a reach-qualified scope."""
    checked = 0
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS or operation["operationId"] in REACH_AWARE_OPERATIONS:
                continue
            if "403" not in operation["responses"]:
                continue
            (requirement,) = operation["security"]
            (scopes,) = requirement.values()
            noun = "scope" if len(scopes) == 1 else "scopes"
            # A 403 the route declares itself is appended after " / " (`_document` in openapi.py).
            derived = operation["responses"]["403"]["description"].split(" / ")[0]
            assert derived == f"Missing required {noun}: {', '.join(scopes)}", f"{method} {path}"
            checked += 1

    assert checked


def test_every_crm_schema_property_has_a_description(schema: dict[str, Any]):
    names = _crm_schema_names(schema)

    assert "SubjectResponse" in names
    for name in sorted(names):
        for property_name, definition in schema["components"]["schemas"][name].get("properties", {}).items():
            assert definition.get("description", "").strip(), f"{name}.{property_name}"


def test_every_crm_path_and_query_parameter_has_a_description(schema: dict[str, Any]):
    documented = 0
    for method, path, operation in _crm_operations(schema):
        for parameter in operation.get("parameters", []):
            documented += 1
            assert parameter.get("description", "").strip(), f"{method} {path}: {parameter['name']}"

    assert documented


def test_every_crm_path_parameter_has_examples(schema: dict[str, Any]):
    for method, path, operation in _crm_operations(schema):
        for parameter in operation.get("parameters", []):
            if parameter["in"] == "path":
                assert parameter.get("examples") or parameter["schema"].get("examples"), (
                    f"{method} {path}: {parameter['name']}"
                )


def test_party_detail_is_a_discriminated_union_of_person_and_company(schema: dict[str, Any]):
    assert schema["components"]["schemas"]["PartyDetail"] == {
        "oneOf": [
            {"$ref": "#/components/schemas/PersonDetail"},
            {"$ref": "#/components/schemas/CompanyDetail"},
        ],
        "discriminator": {
            "propertyName": "type",
            "mapping": {
                "person": "#/components/schemas/PersonDetail",
                "company": "#/components/schemas/CompanyDetail",
            },
        },
    }


def test_party_detail_is_referenced_by_exactly_one_operation(schema: dict[str, Any]):
    referencing = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in HTTP_METHODS and "PartyDetail" in _refs(operation)
    ]
    other_schemas = [
        name for name, definition in schema["components"]["schemas"].items() if "PartyDetail" in _refs(definition)
    ]

    assert referencing == ["crm_get_party"]
    assert other_schemas == []


def test_the_typed_write_routes_reference_their_union_member_directly(schema: dict[str, Any]):
    paths = schema["paths"]

    for path, method, status, member in [
        ("/api/v1/crm/persons", "post", "201", "PersonDetail"),
        ("/api/v1/crm/persons/{party_id}", "patch", "200", "PersonDetail"),
        ("/api/v1/crm/companies", "post", "201", "CompanyDetail"),
        ("/api/v1/crm/companies/{party_id}", "patch", "200", "CompanyDetail"),
    ]:
        response = paths[path][method]["responses"][status]
        assert response["content"]["application/json"]["schema"] == {"$ref": f"#/components/schemas/{member}"}


def test_the_union_members_differ_in_their_required_fields(schema: dict[str, Any]):
    """The generated client ignores the discriminator and tries the members in order."""
    person = set(schema["components"]["schemas"]["PersonDetail"]["required"])
    company = set(schema["components"]["schemas"]["CompanyDetail"]["required"])

    assert person - company
    assert company - person


def test_every_property_of_a_create_request_carries_examples(schema: dict[str, Any]):
    create_requests = {
        name: definition
        for name, definition in schema["components"]["schemas"].items()
        if name.endswith("CreateRequest") and name in _crm_schema_names(schema)
    }

    assert {"PersonCreateRequest", "CompanyCreateRequest", "ContactInfoCreateRequest"} <= set(create_requests)
    for name, definition in create_requests.items():
        for property_name, property_schema in definition["properties"].items():
            assert property_schema.get("examples"), f"{name}.{property_name}"


# The route map of the CRM API spec is closed: method, path and operation ID are the contract.
ROUTE_MAP = {
    ("GET", "/parties"): "crm_list_parties",
    ("GET", "/parties/{party_id}"): "crm_get_party",
    ("DELETE", "/parties/{party_id}"): "crm_delete_party",
    ("POST", "/persons"): "crm_create_person",
    ("PATCH", "/persons/{party_id}"): "crm_update_person",
    ("POST", "/companies"): "crm_create_company",
    ("PATCH", "/companies/{party_id}"): "crm_update_company",
    ("PUT", "/persons/{party_id}/student"): "crm_put_student_role",
    ("DELETE", "/persons/{party_id}/student"): "crm_remove_student_role",
    ("PUT", "/persons/{party_id}/tutor"): "crm_put_tutor_role",
    ("DELETE", "/persons/{party_id}/tutor"): "crm_remove_tutor_role",
    ("POST", "/parties/{party_id}/contact-infos"): "crm_add_contact_info",
    ("PATCH", "/parties/{party_id}/contact-infos/{contact_info_id}"): "crm_update_contact_info",
    ("DELETE", "/parties/{party_id}/contact-infos/{contact_info_id}"): "crm_remove_contact_info",
    ("GET", "/parties/{party_id}/relations"): "crm_list_relations",
    ("PUT", "/parties/{party_id}/relations/{type}/{to_party_id}"): "crm_put_relation",
    ("DELETE", "/parties/{party_id}/relations/{type}/{to_party_id}"): "crm_remove_relation",
    ("GET", "/subjects"): "crm_list_subjects",
    ("POST", "/subjects"): "crm_create_subject",
    ("PATCH", "/subjects/{subject_id}"): "crm_update_subject",
    ("DELETE", "/subjects/{subject_id}"): "crm_delete_subject",
}
SUCCESS_STATUS = {"POST": "201", "DELETE": "204"}


def test_the_crm_routes_are_exactly_the_route_map_of_the_spec(schema: dict[str, Any]):
    actual = {
        (method, path.removeprefix("/api/v1/crm")): operation["operationId"]
        for method, path, operation in _crm_operations(schema)
    }

    assert actual == ROUTE_MAP


def test_every_crm_route_answers_with_the_success_status_of_its_method(schema: dict[str, Any]):
    """Decision E: `POST` is 201, every `DELETE` is 204, everything else 200."""
    for method, path, operation in _crm_operations(schema):
        success = [status for status in operation["responses"] if status.startswith("2")]
        assert success == [SUCCESS_STATUS.get(method, "200")], f"{method} {path}"


def test_the_lists_return_the_generic_page(schema: dict[str, Any]):
    for path, item in [
        ("/api/v1/crm/parties", "PartyListItem"),
        ("/api/v1/crm/parties/{party_id}/relations", "RelationResponse"),
        ("/api/v1/crm/subjects", "SubjectResponse"),
    ]:
        response = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert response == {"$ref": f"#/components/schemas/Page_{item}_"}


@pytest.mark.parametrize("name", ["ContactInfoResponse", "ContactInfoCreateRequest", "ContactInfoUpdateRequest"])
def test_every_contact_value_says_how_a_phone_number_is_stored(schema: dict[str, Any], name: str):
    description = schema["components"]["schemas"][name]["properties"]["value"]["description"]

    assert "E.164" in description
    assert "without whitespace" not in description


def test_the_search_says_how_to_find_a_phone_number(schema: dict[str, Any]):
    """The stored form has no national leading zero, and search is a plain substring match."""
    parameters = schema["paths"]["/api/v1/crm/parties"]["get"]["parameters"]
    (q,) = [parameter for parameter in parameters if parameter["name"] == "q"]

    assert "E.164" in q["description"]
    assert "leading zero" in q["description"]


def test_every_party_list_item_carries_a_required_updated_at(schema: dict[str, Any]):
    """Bot-decoupling P0-6: the party list and the other side of a relation carry the pull signal, placed last."""
    item = schema["components"]["schemas"]["PartyListItem"]
    updated_at = item["properties"]["updated_at"]

    assert item["required"] == ["id", "type", "display_name", "roles", "updated_at"]
    assert (updated_at["type"], updated_at["format"]) == ("string", "date-time")
    assert " ".join(updated_at["description"].split()) == (
        "When anything in the party last changed - the person or company, a role, a contact info, a relation - or a "
        "party related to it was deleted. The start of the writing transaction: pull as `updated_since` describes."
    )
    assert schema["components"]["schemas"]["RelationResponse"]["properties"]["party"] == {
        "$ref": "#/components/schemas/PartyListItem",
        "description": "The party on the other side of the relation.",
    }
