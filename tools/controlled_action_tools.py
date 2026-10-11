"""
tools/controlled_action_tools.py — JURIS Controlled Action Tools.

These tools perform write or side-effect operations within LED.
They are the most restricted tool category.

CONTROLLED ACTION RULES:
    1. is_write=True is set on all tools in this file.
    2. The orchestrator requires explicit user confirmation before
       executing any is_write tool (external integration — confirmation flow TBD).
    3. The model may SUGGEST an action; it may NOT force one.
    4. JURIS never autonomously creates, modifies, or sends anything
       without the user explicitly approving.

Allowed controlled actions (development baseline contracts):
    - draft_document    — AI drafts a document for user review
    - create_reminder   — Add a reminder to LED
    - send_notification — Send an in-app notification

NOT allowed (enforced by this tool layer):
    - Filing documents with courts
    - Modifying eCourts records
    - Sending emails/SMS on behalf of the user

development baseline: Interface + schema only.
external integration: execute() connects to LED APIs.
"""

from __future__ import annotations

from core.permissions import ResourceType, Action
from tools.base import (
    JurisTool,
    ToolDefinition,
    ToolInvocation,
    ToolParameter,
    ToolResult,
)


class DraftDocumentTool(JurisTool):
    """
    AI-assisted document drafting.

    Produces a draft for user review — never files or saves automatically.
    The draft is returned to LED for the user to review, edit, and decide.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="draft_document",
            description=(
                "Generate a draft legal document for user review. "
                "The draft is presented to the user in LED for editing — "
                "it is NEVER automatically filed, saved, or sent. "
                "Requires an active case context."
            ),
            parameters=[
                ToolParameter(
                    name="document_type",
                    description="Type of document to draft (e.g. vakalatnama, petition, reply).",
                    required=True,
                    type_hint="string",
                ),
                ToolParameter(
                    name="case_id",
                    description="The case this document is for.",
                    required=True,
                    type_hint="string",
                ),
                ToolParameter(
                    name="instructions",
                    description="Additional drafting instructions from the user.",
                    required=False,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.DOCUMENT,
            action=Action.DRAFT,
            is_write=True,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return self.error_result(
            "Document drafting is not yet connected. (external integration)"
        )


class CreateReminderTool(JurisTool):
    """Create a reminder in LED for the user."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="create_reminder",
            description=(
                "Create a reminder in LED on behalf of the user. "
                "Requires user confirmation before saving. "
                "Never sends external notifications automatically."
            ),
            parameters=[
                ToolParameter(
                    name="title",
                    description="Short title for the reminder.",
                    required=True,
                    type_hint="string",
                ),
                ToolParameter(
                    name="due_date",
                    description="ISO 8601 date/time for the reminder.",
                    required=True,
                    type_hint="string",
                    example="2025-01-15T10:00:00+05:30",
                ),
                ToolParameter(
                    name="case_id",
                    description="Associate reminder with a case (optional).",
                    required=False,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.CASE,
            action=Action.CREATE,
            is_write=True,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return self.error_result(
            "Reminder creation is not yet connected. (external integration)"
        )


class AskClarificationTool(JurisTool):
    """
    Request clarification from the user.

    This is a meta-tool — not a data tool.  Used when JURIS needs
    more information before it can meaningfully help.

    Unlike other tools, this one does not require a permission check
    because it reads no data.  The orchestrator handles it specially.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="ask_clarification",
            description=(
                "Ask the user a clarifying question before proceeding. "
                "Use when the request is ambiguous or case context is missing. "
                "JURIS should ask at most one clarifying question at a time."
            ),
            parameters=[
                ToolParameter(
                    name="question",
                    description="The clarifying question to ask the user.",
                    required=True,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.CASE,   # placeholder; orchestrator overrides
            action=Action.READ,
            is_write=False,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        # This tool's output is the question itself — the orchestrator
        # routes it directly to the ChatResponse rather than back to the model.
        question = invocation.arguments.get("question", "")
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"question": question},
            metadata={"requires_user_response": True},
        )
