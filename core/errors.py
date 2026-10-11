"""
core/errors.py — Structured JURIS errors.

These types are the public error vocabulary.  HTTP handlers map them to
status codes.  Messages must never include secrets, tokens, or stack traces.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    AUTHENTICATION_FAILURE = "authentication_failure"
    AUTHORIZATION_FAILURE = "authorization_failure"
    SUBSCRIPTION_NOT_ELIGIBLE = "subscription_not_eligible"
    INSUFFICIENT_TOKENS = "insufficient_tokens"
    TOKEN_OPERATION_CONFLICT = "token_operation_conflict"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    INVALID_CNR = "invalid_cnr"
    CONTEXT_MISSING = "context_missing"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_MALFORMED_RESPONSE = "provider_malformed_response"
    CONFIGURATION_ERROR = "configuration_error"
    INTERNAL_ERROR = "internal_error"
    ECOURTS_WRITE_FORBIDDEN = "ecourts_write_forbidden"


class JurisError(Exception):
    """Base error.  `message` is safe to return to LED."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        http_status: int = 400,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {
            "error": self.code.value,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class AuthenticationFailure(JurisError):
    def __init__(self, message: str = "Authentication failed.") -> None:
        super().__init__(
            ErrorCode.AUTHENTICATION_FAILURE,
            message,
            http_status=401,
        )


class AuthorizationFailure(JurisError):
    def __init__(self, message: str = "Access denied.") -> None:
        super().__init__(
            ErrorCode.AUTHORIZATION_FAILURE,
            message,
            http_status=403,
        )


class SubscriptionNotEligible(JurisError):
    def __init__(self, message: str = "This feature requires an eligible paid plan.") -> None:
        super().__init__(
            ErrorCode.SUBSCRIPTION_NOT_ELIGIBLE,
            message,
            http_status=403,
        )


class InsufficientTokens(JurisError):
    def __init__(self, required: int, available: int) -> None:
        super().__init__(
            ErrorCode.INSUFFICIENT_TOKENS,
            "The current token balance is insufficient for this operation.",
            http_status=403,
            details={"required": required, "available": available},
        )


class TokenOperationConflict(JurisError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.TOKEN_OPERATION_CONFLICT,
            "This operation reference was already used with different token details.",
            http_status=409,
        )


class NotFoundError(JurisError):
    def __init__(self, message: str = "Resource not found.") -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, http_status=404)


class InvalidInputError(JurisError):
    def __init__(self, message: str = "Invalid input.") -> None:
        super().__init__(ErrorCode.INVALID_INPUT, message, http_status=422)


class InvalidCnrError(JurisError):
    def __init__(self, message: str = "Invalid CNR.") -> None:
        super().__init__(ErrorCode.INVALID_CNR, message, http_status=422)


class ContextMissingError(JurisError):
    def __init__(self, message: str = "Case context is required.") -> None:
        super().__init__(ErrorCode.CONTEXT_MISSING, message, http_status=422)


class ProviderTimeoutError(JurisError):
    def __init__(self, message: str = "External provider timed out.") -> None:
        super().__init__(ErrorCode.PROVIDER_TIMEOUT, message, http_status=504)


class ProviderUnavailableError(JurisError):
    def __init__(self, message: str = "External provider is unavailable.") -> None:
        super().__init__(ErrorCode.PROVIDER_UNAVAILABLE, message, http_status=503)


class ProviderRateLimitError(JurisError):
    def __init__(self, message: str = "External provider rate limit reached.") -> None:
        super().__init__(ErrorCode.PROVIDER_RATE_LIMIT, message, http_status=429)


class ProviderMalformedResponseError(JurisError):
    def __init__(self, message: str = "External provider returned an invalid response.") -> None:
        super().__init__(ErrorCode.PROVIDER_MALFORMED_RESPONSE, message, http_status=502)


class ConfigurationError(JurisError):
    def __init__(self, message: str = "Server configuration error.") -> None:
        super().__init__(ErrorCode.CONFIGURATION_ERROR, message, http_status=500)


class ECourtsWriteForbidden(JurisError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.ECOURTS_WRITE_FORBIDDEN,
            "eCourts is read-only. JURIS cannot write to the government source.",
            http_status=403,
        )
