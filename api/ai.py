"""User AI configuration endpoints.

Raw BYOK credentials are accepted only over the authenticated API boundary,
encrypted before storage, and never returned to the client or model layer.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ai.credentials import FirestoreBYOKVault
from api.deps import get_request_identity
from core.config import settings
from core.errors import ConfigurationError
from core.identity import RequestIdentity
from models.ai import AIConfigRequest, AIConfigResponse

router = APIRouter(tags=["ai"])


@router.put("/ai/config", response_model=AIConfigResponse)
async def configure_ai(
    body: AIConfigRequest,
    identity: RequestIdentity = Depends(get_request_identity),
) -> AIConfigResponse:
    """Configure the caller's provider preference and, for BYOK, store its key."""
    if not identity.user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A verified account is required to configure AI credentials.",
        )

    if body.mode.value == "byok":
        if not settings.byok_enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User BYOK is not enabled on this JURIS server.",
            )
        if not body.api_key:
            raise HTTPException(status_code=422, detail="An API key is required when configuring BYOK.")
        try:
            vault = FirestoreBYOKVault()
            await vault.set_api_key(identity.user.user_id, body.provider, body.api_key)
        except ConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return AIConfigResponse(
            provider=body.provider,
            model=body.model,
            mode=body.mode,
            credential_configured=True,
        )

    if body.api_key:
        raise HTTPException(
            status_code=400,
            detail="Do not send an API key when using managed AI.",
        )

    return AIConfigResponse(
        provider=body.provider,
        model=body.model,
        mode=body.mode,
        credential_configured=True,
    )


@router.delete("/ai/config/{provider}", status_code=204)
async def delete_byok_credential(
    provider: str,
    identity: RequestIdentity = Depends(get_request_identity),
) -> None:
    """Delete the caller's stored BYOK credential for a provider."""
    if not identity.user.is_verified:
        raise HTTPException(status_code=401, detail="A verified account is required.")
    if not settings.byok_enabled:
        raise HTTPException(status_code=403, detail="User BYOK is not enabled on this JURIS server.")
    from ai.contracts import AIProvider
    try:
        selected = AIProvider(provider)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Unsupported AI provider.") from exc
    try:
        vault = FirestoreBYOKVault()
        await vault.delete_api_key(identity.user.user_id, selected)
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
