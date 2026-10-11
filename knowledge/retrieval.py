"""Provider-neutral document retrieval boundary.

LED remains the source of truth. This layer exists so local/shared document
text can later be indexed without coupling the orchestrator to a vector DB.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    document_id: str
    case_id: str
    text: str
    score: float = 0.0
    page: int | None = None
    source_name: str = ""
    metadata: dict = Field(default_factory=dict)


class DocumentRetriever(ABC):
    @abstractmethod
    async def search(self, *, user_id: str, case_id: str, query: str, limit: int = 8) -> list[RetrievedChunk]:
        ...


class UnconfiguredDocumentRetriever(DocumentRetriever):
    async def search(self, *, user_id: str, case_id: str, query: str, limit: int = 8) -> list[RetrievedChunk]:
        return []
