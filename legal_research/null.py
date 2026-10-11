"""Explicitly unavailable legal research provider."""
from __future__ import annotations
from legal_research.base import LegalResearchProvider, LegalResearchResult


class UnconfiguredLegalResearchProvider(LegalResearchProvider):
    async def search(self, query: str, *, act: str | None = None, limit: int = 10) -> list[LegalResearchResult]:
        return []
