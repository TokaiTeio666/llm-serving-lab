from __future__ import annotations


class ServiceError(Exception):
    """Base class for errors the gateway knows how to translate to HTTP."""

    http_status: int = 500
    error_type: str = "internal_error"

    def __init__(self, message: str, *, upstream: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.upstream = upstream


class UpstreamError(ServiceError):
    http_status = 502
    error_type = "upstream_error"


class UpstreamTimeout(UpstreamError):
    error_type = "upstream_timeout"


class CircuitOpenError(ServiceError):
    http_status = 503
    error_type = "circuit_open"


class OverloadedError(ServiceError):
    http_status = 503
    error_type = "overloaded"


class RateLimitedError(ServiceError):
    http_status = 429
    error_type = "rate_limited"


class BadRequestError(ServiceError):
    http_status = 400
    error_type = "invalid_request"


class ClientDisconnected(ServiceError):
    http_status = 499
    error_type = "client_disconnected"

