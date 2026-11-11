"""Maps the HTTP-agnostic error taxonomy to the error envelope (ADR 0006).

``STATUS_BY_ERROR`` is the single place that knows which category becomes which status code. It
feeds the exception handlers below and the ``error_responses`` docs helper, so runtime behavior
and OpenAPI docs cannot drift.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.utils import is_body_allowed_for_status_code
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import ConflictError, DomainError, DomainValidationError, NotFoundError, UpstreamError
from app.core.logging import REQUEST_ID_HEADER, get_request_id

from .schemas import ErrorResponse, FieldError

STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: 404,
    ConflictError: 409,
    DomainValidationError: 422,
    UpstreamError: 502,
}

VALIDATION_ERROR_CODE = "validation_error"
VALIDATION_ERROR_DETAIL = "Request validation failed"
INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_DETAIL = "Internal server error"

_NON_CODE_CHARACTERS = re.compile(r"[^a-z0-9]+")


class ApiException(StarletteHTTPException):
    """An ``HTTPException`` that brings its own ``code``; raise it via ``ApiError.exception()``."""

    def __init__(self, status_code: int, *, detail: str, code: str, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


@dataclass(frozen=True)
class ApiError:
    """An error the API layer declares itself, outside the domain taxonomy.

    For checks that belong to HTTP rather than to a service (OAuth2 form rules) and for local status
    mappings ``STATUS_BY_ERROR`` does not cover. Declare it once, then raise it, return it, or pass
    it to ``error_responses`` to document it.
    """

    status_code: int
    code: str
    detail: str
    # Excluded from the hash: a dict is unhashable, and status, code and detail identify the error.
    headers: Mapping[str, str] | None = field(default=None, hash=False)

    def exception(self) -> ApiException:
        return ApiException(self.status_code, detail=self.detail, code=self.code, headers=self.headers)

    def response(self) -> JSONResponse:
        """Return the error instead of raising it.

        Only for an endpoint whose transaction must commit although the request failed (e.g. to keep
        the audit entry of a denied token request): raising would roll the session back.
        """
        return _envelope(self.status_code, ErrorResponse(detail=self.detail, code=self.code), headers=self.headers)


def status_for(error_type: type[DomainError]) -> int:
    """Return the status code of the first mapped category along the error's MRO."""
    for base in error_type.__mro__:
        if issubclass(base, DomainError) and base in STATUS_BY_ERROR:
            return STATUS_BY_ERROR[base]

    raise LookupError(
        f"{error_type.__name__} is not mapped to a status code: derive it from a category in "
        "app/core/errors.py (NotFoundError, ConflictError, DomainValidationError, UpstreamError)."
    )


def public_detail(error: DomainError) -> str:
    """Return the text a client may see: the instance message only if the class exposes it."""
    instance_message = str(error)
    if error.expose_message and instance_message:
        return instance_message

    return error.message


def code_for_status(status_code: int) -> str:
    """Derive a coarse code from the status phrase (401 -> ``unauthorized``)."""
    return _NON_CODE_CHARACTERS.sub("_", _phrase_for(status_code).lower()).strip("_")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> Response:
        return _envelope(status_for(type(exc)), ErrorResponse(detail=public_detail(exc), code=exc.code))

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(request: Request, exc: RequestValidationError) -> Response:
        errors = [
            FieldError(loc=list(error["loc"]), message=error["msg"], type=error["type"]) for error in exc.errors()
        ]
        return _envelope(422, ErrorResponse(detail=VALIDATION_ERROR_DETAIL, code=VALIDATION_ERROR_CODE, errors=errors))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=exc.headers)

        detail = exc.detail if isinstance(exc.detail, str) else _phrase_for(exc.status_code)
        code = exc.code if isinstance(exc, ApiException) else code_for_status(exc.status_code)
        return _envelope(exc.status_code, ErrorResponse(detail=detail, code=code), headers=exc.headers)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
        # Runs in Starlette's outermost ServerErrorMiddleware, which re-raises after responding. The
        # request-logging middleware sits inside it and has logged the traceback by then, so this
        # only shapes the body - and never shows the exception's message. For the same reason the
        # middleware cannot stamp this response, so the request id is added here.
        request_id = get_request_id(request)
        headers = {REQUEST_ID_HEADER: request_id} if request_id else None
        return _envelope(500, ErrorResponse(detail=INTERNAL_ERROR_DETAIL, code=INTERNAL_ERROR_CODE), headers=headers)


def _phrase_for(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP error"


def _envelope(status_code: int, body: ErrorResponse, *, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True), headers=headers)
