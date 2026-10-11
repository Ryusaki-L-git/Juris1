"""
models/context.py — JURIS structured request context.

Defines the structured information that LED sends alongside a user message
so JURIS knows *what the user is looking at* when they make a request.

JURIS rule:
    "analyse this case" requires case context or a clearly named case from
    the conversation.  JURIS does not guess what the user means.

The context is provided by LED (not constructed by JURIS from the message).
LED knows what screen the user is on, what case is open, etc.

All fields are optional — LED sends only what is relevant to the current screen.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LEDScreen(str, Enum):
    """
    The LED screen / context the user was on when sending the message.
    Helps JURIS route the request to the right tools without guessing.
    """

    HOME = "home"
    CASE_LIST = "case_list"
    CASE_DETAIL = "case_detail"
    CASE_DOCUMENTS = "case_documents"
    HEARING_SCHEDULE = "hearing_schedule"
    CLIENT_PROFILE = "client_profile"
    DOCUMENT_VIEWER = "document_viewer"
    LEGAL_RESEARCH = "legal_research"
    SETTINGS = "settings"
    UNKNOWN = "unknown"


class ItemRef(BaseModel):
    """
    A lightweight reference to a specific LED resource.

    LED sends these to tell JURIS what item the user has selected,
    without sending the full data (local-first principle).

    The tool layer resolves ItemRef → actual data after permission checks.
    """

    resource_type: str          # matches ResourceType values in core/permissions.py
    resource_id: str
    display_name: str = ""      # human-readable label for logging / clarification


class ConversationContext(BaseModel):
    """
    The recent conversation history JURIS may use for continuity.

    LED manages the conversation thread.  JURIS receives a window of recent
    turns here rather than storing conversation state itself.

    Fields:
        turns           — Most recent N turns, oldest first.
        total_turns     — Total turns in the session (for JURIS to know
                          whether this is a new or continuing conversation).
    """

    class Turn(BaseModel):
        role: str           # "user" | "juris"
        content: str
        turn_index: int = 0

    turns: list[Turn] = Field(default_factory=list)
    total_turns: int = 0


class RequestContext(BaseModel):
    """
    The complete structured context for a single JURIS request.

    This is what the orchestrator works with.  It is built from:
      - The raw LED API request (models/chat.py → ChatRequest)
      - The resolved identity (core/identity.py → RequestIdentity)
      - Assembled by agent/context_resolver.py

    Fields:
        current_screen      — Which LED screen the user was on.
        active_case         — The case currently open in LED, if any.
        selected_item       — A specific item the user has selected/tapped.
        conversation        — Recent conversation turns for continuity.
        extra               — Extension point for future LED signals.
    """

    current_screen: LEDScreen = LEDScreen.UNKNOWN
    active_case: ItemRef | None = None
    selected_item: ItemRef | None = None
    conversation: ConversationContext = Field(default_factory=ConversationContext)
    extra: dict[str, Any] = Field(default_factory=dict)
