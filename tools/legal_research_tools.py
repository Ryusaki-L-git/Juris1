"""
tools/legal_research_tools.py — JURIS Legal Research Tools.

Provides read-only access to Indian law and eCourts data.

CRITICAL ECOURTS RULE:
    eCourts data is READ-ONLY.
    JURIS must NEVER attempt to write, file, or modify anything in eCourts.
    This rule is enforced at multiple levels:
      1. This tool is marked is_ecourts=True in its ToolDefinition.
      2. The orchestrator enforces read-only for all is_ecourts tools.
      3. The HTTP client (external integration) will be configured with no write methods.

development baseline: Interface + schema only.
external integration: execute() connects to eCourts API (read-only) and legal DB.
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


class SearchIndianLawTool(JurisTool):
    """
    Search Indian statutes, sections, and legal provisions.

    Source: Legal database (to be determined in external integration).
    Read-only.  Never modifies any external system.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_indian_law",
            description=(
                "Search configured, allowlisted official Indian legal sources for legal provisions "
                "and supported current legal facts (initial coverage includes the Supreme Court "
                "of India's Chief Justice and Judges page). Use this for specific laws, sections, "
                "appointments, or current legal-office-holder questions. Results include source URLs "
                "and retrieval metadata. If no source is returned, say the fact could not be verified; "
                "never substitute memory or invent a citation. Coverage is intentionally limited."
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    description="The legal query (e.g. 'Section 138 NI Act', 'IPC 420').",
                    required=True,
                    type_hint="string",
                ),
                ToolParameter(
                    name="act",
                    description="Narrow to a specific Act (e.g. 'IPC', 'CrPC', 'NI Act').",
                    required=False,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.LEGAL_RESEARCH,
            action=Action.SEARCH,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        from legal_research.service import LegalResearchService

        query = str(invocation.arguments.get("query") or "").strip()
        act = str(invocation.arguments.get("act") or "").strip() or None
        try:
            results = await LegalResearchService().search(query, act=act)
        except Exception:
            # Safe failure: provider/network details are not model-facing.
            return self.error_result(
                "Official-source retrieval failed. The requested fact or legal authority "
                "could not be verified. Do not answer it as a verified current fact."
            )
        if not results:
            return self.error_result(
                "No matching verified source was retrieved from JURIS's configured official-source "
                "registry. The requested fact or legal authority could not be verified. "
                "Do not invent a citation or present model memory as current verification."
            )
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"results": [item.model_dump(mode="json") for item in results]},
        )



class GetECourtsCaseTool(JurisTool):
    """
    Read eCourts case data from what LED has already stored.

    ARCHITECTURE BOUNDARY:
        JURIS does NOT call eCourts directly.
        JURIS does NOT have an eCourts API credential.

        This tool reads the 'ecourts_data' field that LED's authorized
        server-side integration stores in the Firestore case record.

        If the user wants to refresh eCourts data, they must use the
        "Refresh from eCourts" option in the LED case detail screen.
        LED's authorized server integration will fetch and store the data.
        JURIS can then read and analyze the updated stored data.

    READ-ONLY: Never writes to eCourts or to the case record.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_ecourts_case",
            description=(
                "Read eCourts case data that LED has already fetched and stored for this case. "
                "Requires a valid CNR (Case Number Record) — 16 alphanumeric characters, "
                "e.g. MHCC010012342024. "
                "JURIS reads LED-stored data only — it cannot independently call eCourts. "
                "If eCourts data is missing or stale, ask the user to use "
                "'Refresh from eCourts' in the LED case screen, then ask JURIS again."
            ),
            parameters=[
                ToolParameter(
                    name="cnr_number",
                    description="The Case Number Record (CNR) from eCourts (16 alphanumeric chars).",
                    required=True,
                    type_hint="string",
                    example="MHCC010012342024",
                ),
            ],
            resource_type=ResourceType.ECOURTS,
            action=Action.READ,
            is_ecourts=True,    # orchestrator enforces read-only for this flag
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        from core.cnr import normalize_cnr
        from core.errors import InvalidCnrError

        cnr_raw = invocation.arguments.get("cnr_number", "")
        try:
            cnr = normalize_cnr(cnr_raw)
        except InvalidCnrError as exc:
            return self.error_result(str(exc))

        # CRITICAL: resolve the CNR through AuthorizedLedAccess first.
        # This prevents arbitrary CNR enumeration across LED.
        try:
            access = await invocation.access.require_case_access_by_cnr(cnr)
        except Exception as exc:
            return self.error_result(str(exc))

        data = dict(access.case.ecourts_data or {})
        if not data:
            return ToolResult(
                tool_name=self.definition.name,
                success=True,
                data={
                    "cnr": cnr,
                    "status": (
                        "eCourts data has not been fetched for this LED Case. "
                        "Use the 'Refresh from eCourts' option in LED, then ask JURIS again."
                    ),
                    "case_id": access.case.id,
                    "case_title": access.case.case_title,
                    "source": "led_firestore",
                },
            )

        # Return only the eCourts snapshot already stored by LED.
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={
                "case_id": access.case.id,
                "case_title": access.case.case_title,
                "cnr": cnr,
                "source": "led_firestore",
                "read_only": True,
                "ecourts_data": data,
            },
        )
