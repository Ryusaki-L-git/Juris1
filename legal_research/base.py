"""Provider-neutral legal research contracts.

JURIS must never invent legal authorities. A provider must return source-backed
results or an explicit unavailable response.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field


class LegalResearchResult(BaseModel):
    title: str
    citation: str = ""
    excerpt: str = ""
    source_url: str | None = None
    source_name: str = ""
    verified: bool = False
    metadata: dict = Field(default_factory=dict)


class LegalResearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, *, act: str | None = None, limit: int = 10) -> list[LegalResearchResult]:
        """Return only source-backed legal authorities."""
        ...
