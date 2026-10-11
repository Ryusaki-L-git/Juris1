"""
tools/base.py — JURIS Tool interface contracts.

Defines the abstract base class every JURIS tool must implement,
plus the data types that flow between the orchestrator and tools.

ARCHITECTURE:
    Model → requests ToolCallRequest (gateway/base.py)
        ↓
    Orchestrator validates tool name + arguments
        ↓
    PermissionEngine.require(context)   ← may raise PermissionDenied
        ↓
    JurisTool.execute(invocation)
        ↓
    ToolResult returned to orchestrator
        ↓
    Orchestrator feeds result into next ModelRequest

RULES FOR ALL TOOL IMPLEMENTATIONS:
    1. Tools must never call the model or the gateway.
    2. Tools must never perform permission checks themselves —
       the orchestrator does that before calling execute().
    3. Tools must not store state between invocations.
    4. eCourts-touching tools must be read-only by contract.
    5. Tools must return ToolResult even on failure — never raise
       uncaught exceptions to the orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.authorized_access import AuthorizedLedAccess
from core.identity import JurisUser
from core.permissions import PermissionContext, ResourceType, Action


# ── Tool definition ────────────────────────────────────────────────────────────

class ToolParameter(BaseModel):
    """Describes a single parameter a tool accepts."""

    name: str
    description: str
    required: bool = True
    type_hint: str = "string"       # "string" | "integer" | "boolean" | "object"
    example: Any = None


class ToolDefinition(BaseModel):
    """
    The schema the orchestrator uses to describe a tool to the model.

    The model sees ToolDefinitions (via ModelRequest.available_tools).
    The model never sees the tool implementation.
    """

    name: str
    description: str
    parameters: list[ToolParameter] = Field(default_factory=list)
    resource_type: ResourceType
    action: Action = Action.READ
    is_ecourts: bool = False        # True → read-only enforced at orchestrator level
    is_write: bool = False          # True → requires explicit user confirmation


# ── Invocation / result ────────────────────────────────────────────────────────

class ToolInvocation(BaseModel):
    """
    A validated, authorised tool call ready for execution.

    Built by the orchestrator after:
      - validating tool name and arguments
      - running the permission check

    The tool implementation receives this; it does NOT receive raw
    model output or the original ToolCallRequest.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    invoking_user: JurisUser
    permission_context: PermissionContext
    access: AuthorizedLedAccess | None = None


class ToolResult(BaseModel):
    """
    The result of a tool execution, returned to the orchestrator.

    Maps to gateway/base.py ToolResult — the orchestrator translates
    between these types so neither layer leaks into the other.

    Fields:
        tool_name   — Identifies which tool produced this result.
        success     — False if the tool encountered an error.
        data        — Structured data for the model to use.
                      Must contain only what the model is authorised
                      to receive (the tool is responsible for this).
        error       — Safe error message if success is False.
                      Must not contain internal system details.
        metadata    — Non-model-facing metadata (e.g. cache hints).
    """

    tool_name: str
    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Abstract tool ──────────────────────────────────────────────────────────────

class JurisTool(ABC):
    """
    Abstract base class for all JURIS tools.

    Subclasses must implement:
        definition  — property returning the ToolDefinition.
        execute     — async method performing the actual work.

    Subclasses must NOT:
        - Perform permission checks (orchestrator's responsibility).
        - Call the model gateway.
        - Raise exceptions to the orchestrator — return ToolResult(success=False).
        - Return data the user is not authorised to see.
    """

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return the static schema describing this tool."""
        ...

    @abstractmethod
    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        """
        Execute the tool and return a result.

        The invocation has already been authorised.
        The tool only needs to perform its function and return data.
        """
        ...

    # ── Convenience ──────────────────────────────────────────────────────────

    def error_result(self, message: str) -> ToolResult:
        """Return a failed ToolResult with a safe error message."""
        return ToolResult(
            tool_name=self.definition.name,
            success=False,
            error=message,
        )
