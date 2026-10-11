"""
api/chat.py — JURIS /chat API router.

This is the LED ↔ JURIS HTTP boundary for conversational requests.

Responsibility:
    - Accept and validate the ChatRequest from LED Flutter.
    - Resolve a RequestIdentity for the caller.
    - Pass the request to the orchestrator.
    - Return the ChatResponse to LED.

This router does NOT:
    - Contain business logic.
    - Access tools, databases, or the model directly.
    - Perform permission checks (that is the orchestrator's job).

Authentication (production auth):
    The get_identity dependency will verify the caller's JWT and return
    a real JurisUser.  For development baseline it returns a placeholder identity so
    the full pipeline runs end-to-end without auth infrastructure.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from agent.orchestrator import JurisOrchestrator
from api.deps import get_request_identity
from core.identity import RequestIdentity
from models.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])

# ── Orchestrator ───────────────────────────────────────────────────────────────
# Shared, stateless — safe for concurrent requests.
_orchestrator = JurisOrchestrator()


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    identity: RequestIdentity = Depends(get_request_identity),
) -> ChatResponse:
    """
    Accept a message from LED Flutter and return JURIS's response.

    The full pipeline runs here:
        LED request → authenticated identity resolution → orchestrator → gateway → response
    """
    # Ensure session_id from request body is synced into the RequestIdentity
    if not identity.session_id and request.session_id:
        identity = RequestIdentity(
            user=identity.user,
            session_id=request.session_id,
            request_id=identity.request_id,
            source=identity.source,
        )

    return await _orchestrator.handle(request, identity)