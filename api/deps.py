"""
api/deps.py — Shared FastAPI dependencies for JURIS.

Provides dependency injection for:
    - Authenticated user resolution (get_current_user)
    - Full request identity and tracing context (get_request_identity)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import settings
from core.identity import JurisUser, RequestIdentity, SubscriptionTier
from core.security import AuthenticationError, get_token_verifier

logger = logging.getLogger("juris.deps")

# HTTP Bearer scheme extractor (auto_error=False allows handling dev fallback)
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> JurisUser:
    """
    Authenticate the incoming request from the Authorization header.

    Rules:
      1. If a Bearer token is provided, verify it through the active TokenVerifier.
      2. If token is invalid or expired, reject with HTTP 401.
      3. If no token is provided:
         - In staging/production: reject with HTTP 401.
         - In development with dev_allow_unauthenticated=True: provide fallback dev user.
    """
    verifier = get_token_verifier()

    if credentials is not None and credentials.credentials:
        token = credentials.credentials.strip()
        try:
            user = await verifier.verify_token(token)
            return user
        except AuthenticationError as e:
            logger.warning("Authentication failure: %s", e.message)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=e.message,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from e

    # No token provided
    if settings.is_auth_enforced:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided. Pass 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Local development unauthenticated fallback (is_verified=False)
    logger.debug("Allowing unauthenticated fallback user in local development mode.")
    return JurisUser(
        user_id="dev-unauthenticated",
        email="dev-unauthenticated@lawyersediary.local",
        display_name="Local Dev User",
        subscription=SubscriptionTier.SOLO,
        is_verified=False,
        metadata={"auth": "dev_fallback"},
    )


async def get_request_identity(
    request: Request,
    user: JurisUser = Depends(get_current_user),
) -> RequestIdentity:
    """
    Construct the immutable RequestIdentity for the current request.
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    source = request.headers.get("X-Client-Source", "led_flutter")
    session_id = request.headers.get("X-Session-ID", "")

    return RequestIdentity(
        user=user,
        session_id=session_id,
        request_id=request_id,
        source=source,
    )
