"""Public HTTP error contract; never expose exception or validation input text."""

from pydantic import BaseModel
from starlette.responses import JSONResponse


class APIError(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: str


# The HTTP status remains authoritative; only these fixed public values reach clients.
ERRORS: dict[int, tuple[str, str, bool]] = {
    400: ("bad_request", "Invalid request", False),
    401: ("unauthorized", "Authentication required", False),
    403: ("forbidden", "Access denied", False),
    404: ("not_found", "Resource not found", False),
    405: ("method_not_allowed", "Method not allowed", False),
    409: ("conflict", "Request conflicts with existing state", False),
    410: ("gone", "Resource no longer available", False),
    413: ("payload_too_large", "Request data too large", False),
    422: ("validation_error", "Invalid request data", False),
    429: ("rate_limited", "Too many requests", True),
    500: ("internal_error", "Internal server error", False),
    502: ("bad_gateway", "Upstream service unavailable", True),
    503: ("service_unavailable", "Service temporarily unavailable", True),
    504: ("gateway_timeout", "Upstream service timed out", True),
}

ERROR_RESPONSES = {status: {"model": APIError} for status in ERRORS}


def error_response(status: int, request_id: str, *, www_authenticate: bool = False) -> JSONResponse:
    code, message, retryable = ERRORS.get(status, ("http_error", "Request failed", False))
    response = JSONResponse(
        status_code=status,
        content=APIError(
            code=code, message=message, retryable=retryable, request_id=request_id,
        ).model_dump(),
    )
    if www_authenticate and status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response
