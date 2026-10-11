"""
core/security.py — JURIS Authentication & Token Verification Boundary.

This module is the single authority for verifying caller identity before
any request enters the JURIS pipeline.

SECURITY PRINCIPLE:
    - In production/staging, requests MUST provide a valid Firebase Auth ID token.
    - Development tokens are strictly rejected in non-development environments.
    - An unverified user is explicitly marked `is_verified=False`.
    - No downstream component (permission engine, orchestrator, tools, model)
      may bypass authentication.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import jwt
from jwt import PyJWKClient

from core.config import settings
from core.identity import JurisUser, SubscriptionTier

logger = logging.getLogger("juris.security")

# Google's public key endpoint for Firebase Auth ID token verification
FIREBASE_JWKS_URL = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"


class AuthenticationError(Exception):
    """Raised when authentication fails (missing, expired, or invalid token)."""

    def __init__(self, message: str = "Invalid authentication credentials.") -> None:
        super().__init__(message)
        self.message = message


class TokenVerifier(ABC):
    """
    Abstract interface for token verification.

    Implementations verify the authentication token and map its claims
    to a typed, immutable JurisUser.
    """

    @abstractmethod
    async def verify_token(self, token: str) -> JurisUser:
        """
        Verify the raw token string and return an authenticated JurisUser.

        Raises:
            AuthenticationError: if token is expired, invalid, or malformed.
        """
        ...


class FirebaseTokenVerifier(TokenVerifier):
    """
    Production Firebase Auth ID Token verifier.

    Validates:
      - Token signature using Google's public JWK certificates.
      - Audience matches JURIS_FIREBASE_PROJECT_ID.
      - Issuer matches https://securetoken.google.com/<project_id>.
      - Token is not expired.
            - Maps verified identity claims (sub -> user_id, email, name, team_id).
                Subscription claims are not entitlement authority; plan access is
                loaded from the server-owned entitlement store.
    """

    def __init__(self, project_id: str | None = None) -> None:
        self.project_id = project_id or settings.firebase_project_id
        self._jwks_client: PyJWKClient | None = None

    def _get_jwks_client(self) -> PyJWKClient:
        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(FIREBASE_JWKS_URL, cache_keys=True, max_cached_keys=10)
        return self._jwks_client

    async def verify_token(self, token: str) -> JurisUser:
        if not token or not token.strip():
            raise AuthenticationError("Bearer token cannot be empty.")

        if not self.project_id:
            raise AuthenticationError(
                "Firebase project ID is not configured on the JURIS backend. "
                "Set JURIS_FIREBASE_PROJECT_ID."
            )

        try:
            # 1. Obtain the signing key from Google JWKS using the token's kid
            jwks_client = self._get_jwks_client()
            signing_key = jwks_client.get_signing_key_from_jwt(token)

            expected_issuer = f"https://securetoken.google.com/{self.project_id}"

            # 2. Decode and verify signature, audience, and issuer
            payload: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.project_id,
                issuer=expected_issuer,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                },
            )

            user_id = payload.get("sub") or payload.get("user_id")
            if not user_id:
                raise AuthenticationError("Token missing valid subject (user_id).")

            return JurisUser(
                user_id=str(user_id),
                email=str(payload.get("email", "")),
                display_name=str(payload.get("name", "")),
                team_id=payload.get("team_id"),
                subscription=SubscriptionTier.FREE,
                is_verified=True,
                metadata={
                    "auth_time": payload.get("auth_time"),
                    "exp": payload.get("exp"),
                    "iss": payload.get("iss"),
                },
            )

        except jwt.ExpiredSignatureError as e:
            raise AuthenticationError("Authentication token has expired.") from e
        except jwt.InvalidTokenError as e:
            raise AuthenticationError(f"Invalid authentication token: {str(e)}") from e
        except Exception as e:
            logger.error("Unexpected error verifying Firebase token: %s", e)
            raise AuthenticationError(f"Authentication verification failed: {str(e)}") from e


class DevTokenVerifier(TokenVerifier):
    """
    Development-only token verifier.

    Allows testing JURIS locally without connecting to live Firebase Auth.
    Strictly forbidden in staging or production.

    Supported token formats:
      - 'dev-placeholder' or 'dev:default' -> default dev user
      - 'dev:<user_id>' -> dev user with specified user_id
    - 'dev:<user_id>:<tier>' -> legacy syntax; tier is ignored
    """

    async def verify_token(self, token: str) -> JurisUser:
        # Absolute security invariant: NEVER allow dev tokens outside development
        if not settings.is_development:
            raise AuthenticationError(
                "CRITICAL: Development tokens are strictly forbidden in non-development environments."
            )

        if not token or not token.strip():
            raise AuthenticationError("Bearer token cannot be empty.")

        clean_token = token.strip()

        if clean_token in ("dev-placeholder", "dev:default"):
            return JurisUser(
                user_id="dev-user-001",
                email="dev@lawyersediary.local",
                display_name="Dev Advocate",
                subscription=SubscriptionTier.FREE,
                is_verified=False,  # Dev tokens are never cryptographically verified
                metadata={"token_type": "dev"},
            )

        if clean_token.startswith("dev:"):
            parts = clean_token.split(":")
            user_id = parts[1] if len(parts) > 1 and parts[1] else "dev-user"
            return JurisUser(
                user_id=user_id,
                email=f"{user_id}@lawyersediary.local",
                display_name=f"Dev Advocate ({user_id})",
                subscription=SubscriptionTier.FREE,
                is_verified=False,
                metadata={"token_type": "dev", "custom_id": user_id},
            )

        raise AuthenticationError(
            f"Unrecognized development token '{clean_token}'. "
            "Use 'dev-placeholder' or 'dev:<user_id>[:<tier>]'."
        )


def get_token_verifier() -> TokenVerifier:
    """
    Factory returning the active TokenVerifier based on environment and config.
    """
    if settings.is_production or settings.env == "staging":
        if settings.auth_mode == "dev":
            raise RuntimeError(
                "Security misconfiguration: JURIS_AUTH_MODE cannot be 'dev' in staging or production."
            )
        return FirebaseTokenVerifier()

    if settings.auth_mode == "firebase":
        return FirebaseTokenVerifier()

    return DevTokenVerifier()
