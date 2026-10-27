"""
models/chat.py — LED ↔ JURIS API contract for the /chat endpoint.

ChatRequest  — what LED Flutter sends to JURIS.
ChatResponse — what JURIS returns to LED Flutter.

Design rules:
  - All fields are typed and documented.
  - No AI provider types leak into this contract.
  - LED must not need to change when JURIS switches model providers.
  - The contract is stable; internals change behind it.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from models.context import RequestContext
from models.ai import AIConfigRequest


class ChatRequest(BaseModel):
    """
    A request from LED Flutter to JURIS.

    Fields:
        message         — The user's natural language message.
        session_id      — LED's identifier for the current conversation.
                          Used for context threading; not an auth token.
                          Defaults to a generated UUID if omitted.
        context         — Structured LED context (screen, case, item, history).
                          Optional — JURIS functions with or without it, but
                          accuracy improves when LED provides context.
    """

    message: str = Field(..., min_length=1, max_length=8000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    context: RequestContext = Field(default_factory=RequestContext)
    client_version: str | None = Field(default=None, description="LED client application version.")
    confirmed_action_ids: list[str] = Field(default_factory=list, description="Server-issued IDs for write actions explicitly approved by the user.")
    ai: AIConfigRequest | None = Field(default=None, description="Optional provider/model preference. Credentials are never carried here.")
    billing_confirmation_token: str | None = Field(
        default=None,
        max_length=128,
        description="Single-use server-issued approval for the estimated JURIS AI Credit budget.",
    )


class ResponseStatus(str):
    """
    Machine-readable status codes LED can act on.
    Using plain strings rather than an Enum keeps the contract stable
    when new statuses are added.
    """

    OK = "ok"
    CLARIFICATION_NEEDED = "clarification_needed"
    PERMISSION_DENIED = "permission_denied"
    CONTEXT_MISSING = "context_missing"
    UNAUTHENTICATED = "unauthenticated"
    UNAVAILABLE = "unavailable"
    CONFIRMATION_REQUIRED = "confirmation_required"


class ChatResponse(BaseModel):
    """
    JURIS's response to LED Flutter.

    Fields:
        reply           — The natural language reply to show the user.
                          Empty string if JURIS cannot produce a reply.
        status          — Machine-readable outcome code (see ResponseStatus).
        session_id      — Echoed back so LED can match response to session.
        suggested_action — Optional structured hint for LED to act on
                          (e.g. open a case screen, show a document).
                          Shape is intentionally flexible for now.
        debug           — Internal debug info, only populated when
                          settings.debug is True.  Never shown to the user.
    """

    reply: str = ""
    status: str = ResponseStatus.OK
    session_id: str = ""
    suggested_action: dict | None = None
    debug: dict | None = None