"""
tools/hearing_tools.py — JURIS Hearing Tools.

Read access to LED hearing information for cases the caller may access.

LED currently models the next hearing as a field on the Case document, so
hearings are derived from authorized cases. When LED introduces a dedicated
hearings collection, only this file changes.
"""

from __future__ import annotations

from core.errors import JurisError
from core.permissions import Action, ResourceType
from tools.base import (
    JurisTool,
    ToolDefinition,
    ToolInvocation,
    ToolParameter,
    ToolResult,
)

_MAX_HEARINGS = 25


class GetHearingsTool(JurisTool):
    """Retrieve upcoming hearings for a case, or across the user's cases."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_hearings",
            description=(
                "Retrieve upcoming hearing dates. "
                "If case_id is provided, returns hearings for that case only. "
                "If omitted, returns the next hearings across the user's "
                "accessible cases. Results include court name and date."
            ),
            parameters=[
                ToolParameter(
                    name="case_id",
                    description="Limit hearings to this case.",
                    required=False,
                    type_hint="string",
                ),
                ToolParameter(
                    name="limit",
                    description="Maximum hearings to return (default 5).",
                    required=False,
                    type_hint="integer",
                    example=5,
                ),
            ],
            resource_type=ResourceType.HEARING,
            action=Action.READ,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        access = invocation.access
        if access is None:
            return self.error_result("No authorized LED access is available.")
        arguments = invocation.arguments
        case_id = str(arguments.get("case_id") or "").strip() or None
        limit = _parse_limit(arguments.get("limit"))
        try:
            hearings = await access.list_hearings(case_id, limit)
        except JurisError as error:
            return self.error_result(error.message)
        except Exception:
            return self.error_result("Hearings could not be retrieved.")
        payload = [hearing.to_context_dict() for hearing in hearings]
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"hearings": payload, "count": len(payload)},
        )


def _parse_limit(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 5
    if isinstance(value, int):
        return max(1, min(value, _MAX_HEARINGS))
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 5
    return max(1, min(parsed, _MAX_HEARINGS))
