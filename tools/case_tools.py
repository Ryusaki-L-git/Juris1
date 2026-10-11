"""
tools/case_tools.py — JURIS Case Tools.

Read access to a user's LED cases, served exclusively through the
authorized data-access boundary (core/authorized_access.py).

RULES
    - The model never reads Firestore. It requests a tool; JURIS decides.
    - Every read is owner/assignment/team scoped by the data layer.
    - Missing data is reported as missing — never invented.
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

_MAX_SEARCH_RESULTS = 25


class GetCaseTool(JurisTool):
    """Retrieve the details of a single LED case by case_id."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_case",
            description=(
                "Retrieve details for a single LED case. "
                "Requires a case_id from the current session context. "
                "Returns case metadata, parties, status, and hearing summary."
            ),
            parameters=[
                ToolParameter(
                    name="case_id",
                    description="The LED case identifier.",
                    required=True,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.CASE,
            action=Action.READ,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        access = invocation.access
        if access is None:
            return self.error_result("No authorized LED access is available.")
        case_id = str(invocation.arguments.get("case_id") or "").strip()
        if not case_id:
            return self.error_result("A case_id is required.")
        try:
            case = await access.get_case(case_id)
        except JurisError as error:
            return self.error_result(error.message)
        except Exception:
            return self.error_result("The Case could not be retrieved.")
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"case": case.to_context_dict()},
        )


class SearchCasesTool(JurisTool):
    """Search the user's accessible LED cases by keyword and/or status."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_cases",
            description=(
                "Search the user's LED cases. "
                "Supports a keyword query and an optional status filter. "
                "Returns matching cases with summary fields only. "
                "Only cases the user owns, is assigned, or shares via a team "
                "are ever returned."
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    description="Search keyword or natural language query.",
                    required=False,
                    type_hint="string",
                ),
                ToolParameter(
                    name="status",
                    description="Filter by case status (active, closed, etc.).",
                    required=False,
                    type_hint="string",
                ),
                ToolParameter(
                    name="limit",
                    description="Maximum number of results (default 10).",
                    required=False,
                    type_hint="integer",
                    example=10,
                ),
            ],
            resource_type=ResourceType.CASE,
            action=Action.SEARCH,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        access = invocation.access
        if access is None:
            return self.error_result("No authorized LED access is available.")
        arguments = invocation.arguments
        limit = _parse_limit(arguments.get("limit"))
        status = str(arguments.get("status") or "").strip().lower()
        try:
            cases = await access.search_cases(
                str(arguments.get("query") or ""), limit
            )
        except JurisError as error:
            return self.error_result(error.message)
        except Exception:
            return self.error_result("Case search could not be completed.")

        if status:
            cases = [case for case in cases if case.status.lower() == status]
        payload = [case.to_context_dict() for case in cases]
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"cases": payload, "count": len(payload)},
        )


def _parse_limit(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 10
    if isinstance(value, int):
        return max(1, min(value, _MAX_SEARCH_RESULTS))
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 10
    return max(1, min(parsed, _MAX_SEARCH_RESULTS))
