"""
tools/document_tools.py — JURIS Document Tools.

LOCAL-FIRST ARCHITECTURE
    LED keeps heavy documents on the user's device. The cloud representation
    stores document METADATA only. JURIS therefore returns metadata freely
    (within an authorized Case) but reports content as unavailable unless the
    existing architecture actually exposes it. JURIS never fabricates or
    fetches content from an unapproved store.
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


class GetCaseDocumentsTool(JurisTool):
    """List documents associated with an authorized case (metadata only)."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_case_documents",
            description=(
                "List documents attached to a LED case. "
                "Returns document metadata (name, type, size) only. "
                "Does NOT return document content."
            ),
            parameters=[
                ToolParameter(
                    name="case_id",
                    description="The LED case identifier.",
                    required=True,
                    type_hint="string",
                ),
                ToolParameter(
                    name="doc_type",
                    description="Optional document type filter.",
                    required=False,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.DOCUMENT,
            action=Action.READ,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        access = invocation.access
        if access is None:
            return self.error_result("No authorized LED access is available.")
        arguments = invocation.arguments
        case_id = str(arguments.get("case_id") or "").strip()
        if not case_id:
            return self.error_result("A case_id is required.")
        try:
            documents = await access.list_documents(case_id)
        except JurisError as error:
            return self.error_result(error.message)
        except Exception:
            return self.error_result("Documents could not be listed.")

        doc_type = str(arguments.get("doc_type") or "").strip().lower()
        if doc_type:
            documents = [
                document
                for document in documents
                if document.type.strip().lower() == doc_type
            ]
        payload = [
            {
                "name": document.name,
                "type": document.type,
                "size": document.size,
                "content_available": document.content_available,
            }
            for document in documents
        ]
        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={"documents": payload, "count": len(payload)},
        )


class GetDocumentContentTool(JurisTool):
    """
    Retrieve document content for analysis, where the architecture allows it.

    LED stores documents on-device, so this returns an explicit
    "content unavailable" result rather than inventing content.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_document_content",
            description=(
                "Retrieve the text content of a specific LED document for "
                "analysis. Use ONLY when the user explicitly asks JURIS to "
                "read or analyse a document. If LED has not shared a synced "
                "copy, JURIS reports the content as unavailable."
            ),
            parameters=[
                ToolParameter(
                    name="document_id",
                    description="The document name/identifier.",
                    required=True,
                    type_hint="string",
                ),
                ToolParameter(
                    name="case_id",
                    description="The case the document belongs to.",
                    required=True,
                    type_hint="string",
                ),
            ],
            resource_type=ResourceType.DOCUMENT,
            action=Action.READ,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        access = invocation.access
        if access is None:
            return self.error_result("No authorized LED access is available.")
        arguments = invocation.arguments
        case_id = str(arguments.get("case_id") or "").strip()
        document_id = str(arguments.get("document_id") or "").strip()
        if not case_id or not document_id:
            return self.error_result("A case_id and document_id are required.")
        try:
            content = await access.get_document_content(case_id, document_id)
        except JurisError as error:
            return self.error_result(error.message)
        except Exception:
            return self.error_result("The document could not be retrieved.")

        return ToolResult(
            tool_name=self.definition.name,
            success=True,
            data={
                "document_id": content.document_id,
                "case_id": content.case_id,
                "name": content.name,
                "type": content.type,
                "content": content.content,
                "content_available": content.content_available,
                "unavailable_reason": content.unavailable_reason,
            },
        )
