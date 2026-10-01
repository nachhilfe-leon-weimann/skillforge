from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Query, Response
from httpx import ASGITransport, AsyncClient

from app.api.v1.common.errors import (
    STATUS_BY_ERROR,
    ApiError,
    code_for_status,
    register_exception_handlers,
    status_for,
)
from app.core.errors import ConflictError, DomainError, DomainValidationError, NotFoundError

INVALID_CLIENT = ApiError(
    401, code="invalid_client", detail="Invalid client credentials", headers={"WWW-Authenticate": "Bearer"}
)


class WidgetServiceError(Exception):
    """Stand-in for a domain's legacy base class (like ``BotServiceError``)."""


class WidgetNotFoundError(WidgetServiceError, NotFoundError):
    message = "Widget not found"


class WidgetLockedError(ConflictError):
    message = "Widget is locked"
    expose_message = True


class WidgetRuleError(DomainValidationError):
    message = "Widget violates a rule"


class UncategorizedWidgetError(DomainError):
    """Derives from no category, so ``STATUS_BY_ERROR`` cannot map it."""

    code = "test_uncategorized_widget"


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (NotFoundError, 404),
        (ConflictError, 409),
        (DomainValidationError, 422),
        (WidgetNotFoundError, 404),
        (WidgetLockedError, 409),
        (WidgetRuleError, 422),
    ],
)
def test_status_for_resolves_along_the_mro(error_type: type[DomainError], expected: int):
    assert status_for(error_type) == expected


def test_status_for_rejects_an_error_outside_every_category():
    with pytest.raises(LookupError, match="DomainError"):
        status_for(DomainError)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (405, "method_not_allowed"),
        (409, "conflict"),
        (422, "unprocessable_content"),
        (599, "http_error"),
    ],
)
def test_status_derived_codes_are_pinned(status_code: int, expected: str):
    # These codes are contract, but derived from the stdlib's status phrases, which have changed
    # between Python versions (422 was "Unprocessable Entity"). An upgrade must not rename them silently.
    assert code_for_status(status_code) == expected


def test_status_table_only_maps_taxonomy_categories():
    assert STATUS_BY_ERROR == {NotFoundError: 404, ConflictError: 409, DomainValidationError: 422}


async def test_domain_error_becomes_the_envelope_with_its_code():
    response = await _request("GET", "/domain/not-found")

    assert response.status_code == 404
    assert response.json() == {"detail": "Widget not found", "code": "widget_not_found"}


async def test_instance_message_is_not_leaked_by_default():
    response = await _request("GET", "/domain/not-found")

    assert "7f3a" not in response.text


async def test_instance_message_is_shown_when_the_class_exposes_it():
    response = await _request("GET", "/domain/locked", params={"reason": "Locked by operation 42"})

    assert response.status_code == 409
    assert response.json() == {"detail": "Locked by operation 42", "code": "widget_locked"}


async def test_empty_instance_message_falls_back_to_the_class_message():
    response = await _request("GET", "/domain/locked")

    assert response.json() == {"detail": "Widget is locked", "code": "widget_locked"}


async def test_domain_validation_error_is_a_422_without_a_field_list():
    response = await _request("GET", "/domain/rule")

    assert response.status_code == 422
    assert response.json() == {"detail": "Widget violates a rule", "code": "widget_rule"}


async def test_request_validation_error_lists_every_offending_field():
    response = await _request("GET", "/items/not-a-number", params={"limit": 11})

    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "Request validation failed"
    assert body["code"] == "validation_error"
    assert {tuple(error["loc"]) for error in body["errors"]} == {("path", "item_id"), ("query", "limit")}
    for error in body["errors"]:
        assert set(error) == {"loc", "message", "type"}
        assert error["message"]
        assert error["type"]


async def test_http_exception_keeps_status_detail_and_headers():
    response = await _request("GET", "/http/unauthorized")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer scope="auth:clients:manage"'
    assert response.json() == {"detail": "Not authenticated", "code": "unauthorized"}


async def test_http_exception_with_a_structured_detail_falls_back_to_the_status_phrase():
    response = await _request("GET", "/http/structured")

    assert response.status_code == 409
    assert response.json() == {"detail": "Conflict", "code": "conflict"}


async def test_http_exception_for_a_bodiless_status_stays_bodiless():
    response = await _request("GET", "/http/not-modified")

    assert response.status_code == 304
    assert response.content == b""


def test_api_error_with_headers_is_hashable_so_declarations_can_live_in_sets_and_dict_keys():
    assert {INVALID_CLIENT: "documented"}[INVALID_CLIENT] == "documented"


async def test_raised_api_error_keeps_its_own_code_and_headers():
    response = await _request("GET", "/api-error/raised")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Invalid client credentials", "code": "invalid_client"}


async def test_returned_api_error_has_the_same_shape_as_a_raised_one():
    raised = await _request("GET", "/api-error/raised")
    returned = await _request("GET", "/api-error/returned")

    assert (returned.status_code, returned.json()) == (raised.status_code, raised.json())
    assert returned.headers["www-authenticate"] == raised.headers["www-authenticate"]


async def test_unhandled_exception_becomes_a_generic_500_envelope():
    response = await _request("GET", "/boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}


async def test_500_envelope_works_without_the_request_logging_middleware():
    response = await _request("GET", "/boom")

    assert response.status_code == 500
    assert "x-request-id" not in response.headers


async def test_unhandled_exception_does_not_leak_its_message():
    response = await _request("GET", "/boom")

    assert "db password" not in response.text


async def test_domain_error_outside_every_category_is_a_500_not_a_guess():
    response = await _request("GET", "/domain/unmapped")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}


async def test_unknown_route_uses_the_envelope():
    response = await _request("GET", "/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found", "code": "not_found"}


async def test_wrong_method_uses_the_envelope():
    response = await _request("POST", "/domain/not-found")

    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed", "code": "method_not_allowed"}


async def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    # The 500 cases want the response the client gets, not the exception the server saw.
    transport = ASGITransport(app=_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/domain/not-found")
    async def not_found() -> None:
        raise WidgetNotFoundError("widget 7f3a is gone")

    @app.get("/domain/locked")
    async def locked(reason: str = "") -> None:
        raise WidgetLockedError(reason)

    @app.get("/domain/rule")
    async def rule() -> None:
        raise WidgetRuleError("rule 12 failed for tenant 9")

    @app.get("/items/{item_id}")
    async def read_item(item_id: int, limit: Annotated[int, Query(le=10)] = 5) -> None: ...

    @app.get("/http/unauthorized")
    async def unauthorized() -> None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": 'Bearer scope="auth:clients:manage"'},
        )

    @app.get("/http/structured")
    async def structured() -> None:
        raise HTTPException(status_code=409, detail={"reason": "busy"})

    @app.get("/http/not-modified")
    async def not_modified() -> None:
        raise HTTPException(status_code=304)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("db password is hunter2")

    @app.get("/domain/unmapped")
    async def unmapped() -> None:
        raise UncategorizedWidgetError("no category")

    @app.get("/api-error/raised")
    async def api_error_raised() -> None:
        raise INVALID_CLIENT.exception()

    @app.get("/api-error/returned")
    async def api_error_returned() -> Response:
        return INVALID_CLIENT.response()

    return app
