"""
tools/client_tools.py — JURIS Client Tools.

Read access to LED client profiles, served exclusively through the
authorized data-access boundary. Client data is sensitive personal
information, so every read is scoped to the authenticated owner.
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


class GetClientTool(JurisTool):
    """Retrieve a single LED client profile by client_id."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_client",
            description=(
                "Retrieve profile details for a single LED client. "
                "Returns name, contact information, and address. "
                "Only clients owned by the authenticated user are returned."
            ),
            parameters=[
                ToolParameter(
                    name="client_id",
                    description="The LED client identifier.",
                    required=True,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.CLIENT,
            action=Action.READ,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        access = invocation.access
        if access is None:
            return self.error_result("No authorized LED access is available.")
        client_id = str(invocation.arguments.get("client_id") or "").strip()
        if not client_id:
            return self.error_result("A client_id is required.")
        try:
            client = await access.get_client(client_id)
        except JurisError as error:
            return self.error_result(error.message)
        except Exception:
            return self.error_result("The Client could not be retrieved.")
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"client": client.to_context_dict()},
        )


class SearchClientsTool(JurisTool):
    """Search the authenticated user's own LED clients."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_clients",
            description=(
                "Search LED clients by name or contact details. "
                "Returns summary records only. "
                "Only the authenticated user's clients are searched."
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    description="Name or contact keyword to search.",
                    required=True,
                    type_hint="string",
                ),
                ToolParameter(
                    name="limit",
                    description="Maximum results (default 10).",
                    required=False,
                    type_hint="integer",
                    example=10,
                ),
            ],
            resource_type=ResourceType.CLIENT,
            action=Action.SEARCH,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        access = invocation.access
        if access is None:
            return self.error_result("No authorized LED access is available.")
        arguments = invocation.arguments
        limit = _parse_limit(arguments.get("limit"))
        try:
            clients = await access.search_clients(
                str(arguments.get("query") or ""), limit
            )
        except JurisError as error:
            return self.error_result(error.message)
        except Exception:
            return self.error_result("Client search could not be completed.")
        payload = [client.to_context_dict() for client in clients]
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"clients": payload, "count": len(payload)},
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
