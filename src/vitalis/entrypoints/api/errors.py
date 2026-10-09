"""Public HTTP error contract; never expose exception or validation input text."""

from typing import Literal

from pydantic import BaseModel
from starlette.responses import JSONResponse


NextAction = Literal[
    "correct_request", "authenticate", "request_scope", "check_resource",
    "refresh_and_retry", "retry_later", "check_service",
]


class APIError(BaseModel):
    state: Literal["failed"]
    failure_code: str
    message: str
    retryable: bool
    next_action: NextAction
    request_id: str


# The HTTP status remains authoritative; only these fixed public values reach clients.
ERRORS: dict[int, tuple[str, str, bool, NextAction]] = {
    400: ("bad_request", "Invalid request", False, "correct_request"),
    401: ("unauthorized", "Authentication required", False, "authenticate"),
    403: ("forbidden", "Access denied", False, "request_scope"),
    404: ("not_found", "Resource not found", False, "check_resource"),
    405: ("method_not_allowed", "Method not allowed", False, "correct_request"),
    409: ("conflict", "Request conflicts with existing state", False, "refresh_and_retry"),
    410: ("gone", "Resource no longer available", False, "check_resource"),
    413: ("payload_too_large", "Request data too large", False, "correct_request"),
    422: ("validation_error", "Invalid request data", False, "correct_request"),
    429: ("rate_limited", "Too many requests", True, "retry_later"),
    500: ("internal_error", "Internal server error", False, "check_service"),
    502: ("bad_gateway", "Upstream service unavailable", True, "retry_later"),
    503: ("service_unavailable", "Service temporarily unavailable", True, "retry_later"),
    504: ("gateway_timeout", "Upstream service timed out", True, "retry_later"),
}

ERROR_RESPONSES = {status: {"model": APIError} for status in ERRORS}


def error_response(status: int, request_id: str, *, www_authenticate: bool = False) -> JSONResponse:
    code, message, retryable, next_action = ERRORS.get(
        status, ("http_error", "Request failed", False, "check_service"),
    )
    response = JSONResponse(
        status_code=status,
        content=APIError(
            state="failed", failure_code=code, message=message,
            retryable=retryable, next_action=next_action, request_id=request_id,
        ).model_dump(),
    )
    if www_authenticate and status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response
