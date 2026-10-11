"""
gateway/base.py — JURIS Model Gateway abstraction.

This is the architectural boundary between JURIS and any AI model provider.

DESIGN PRINCIPLE:
    The rest of JURIS (orchestrator, tools, API) must never import
    a provider-specific SDK or type directly.  All model interaction
    happens through this interface.

    Swapping providers = implementing a new subclass + updating config.
    Zero changes to orchestrator, tools, or API routes.

SECURITY PRINCIPLE:
    The model never receives raw user data, Firestore documents, or
    eCourts records.  It receives only what the orchestrator explicitly
    constructs and passes via ModelRequest.tool_results.

    The model may *request* a tool call.  The orchestrator validates
    and executes it.  The model does not execute tools directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


# ── Request / Response contracts ───────────────────────────────────────────────

class ToolCallRequest(BaseModel):
    """
    A tool invocation the model has requested.

    The model produces this; JURIS decides whether to honour it.

    Fields:
        tool_name   — Must match a registered JurisTool name.
        arguments   — The arguments the model wants to pass.
    """

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
    # Provider-specific opaque metadata required to replay a tool call.
    # Gemini uses thought_signature to validate tool-call round trips.
    thought_signature: str | None = None


class ToolResult(BaseModel):
    """
    The sanitised result of an authorised tool execution.

    Returned to the model after:
        permission check passed → tool executed → result sanitised.

    Fields:
        tool_name   — Echoed from the ToolCallRequest.
        success     — Whether the tool executed successfully.
        data        — The structured data the model may use.
        error       — Human-readable error if success is False.
    """

    tool_name: str
    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class GatewayMessage(BaseModel):
    """
    A single turn message in a multi-turn conversation or tool execution loop.

    Fields:
        role         — "user", "model", "tool", or "system".
        content      — Text content of the message.
        tool_calls   — Tool calls requested by the model (when role == "model").
        tool_results — Tool results returned to the model (when role == "tool").
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)


class ModelRequest(BaseModel):
    """
    Everything the gateway needs to call the model.

    Built by the orchestrator; the model provider never sees raw
    LED data, tokens, or internal state.

    Fields:
        system_prompt   — JURIS's behavioural instructions for this turn.
        user_message    — The user's message (after context injection).
        messages        — Multi-turn message history for tool reasoning loops.
        tool_results    — Results of tools already executed this turn,
                          if any (for multi-step reasoning).
        available_tools — Descriptions of tools the model may request.
                          The model requests; JURIS decides.
        session_id      — For provider-side session tracking if supported.
        metadata        — Extension point.
    """

    system_prompt: str = ""
    user_message: str = ""
    messages: list[GatewayMessage] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    available_tools: list[dict[str, Any]] = Field(default_factory=list)
    session_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """
    What the gateway returns to the orchestrator.

    The orchestrator decides what to surface to LED.

    Fields:
        text            — The model's natural language response.
        tool_calls      — Tool calls the model is requesting this turn.
        finish_reason   — Why generation stopped ("stop" | "tool_call" | ...).
        provider        — Which gateway produced this response.
        raw_metadata    — Provider-specific metadata (for debugging only).
    """

    text: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    finish_reason: str = "stop"
    provider: str = "unknown"
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


# ── Abstract gateway ───────────────────────────────────────────────────────────

class JurisModelGateway(ABC):
    """
    Abstract base class for all JURIS model gateway implementations.

    Implement one concrete subclass per provider.
    The orchestrator depends only on this interface.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider identifier, e.g. 'echo', 'gemini'."""
        ...

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """
        Send a request to the model and return a structured response.

        Must:
          - Return a valid ModelResponse even on failure (set text to
            an appropriate message, set finish_reason to "error").
          - Never raise provider-specific exceptions to the caller.
          - Never log sensitive content from ModelRequest.
        """
        ...
