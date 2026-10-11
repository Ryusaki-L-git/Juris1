"""Legal research service boundary."""
from __future__ import annotations
from legal_research.base import LegalResearchProvider, LegalResearchResult
from legal_research.null import UnconfiguredLegalResearchProvider
from core.config import settings


class LegalResearchService:
    def __init__(self, provider: LegalResearchProvider | None = None) -> None:
        if provider is not None:
            self._provider = provider
        elif settings.official_research_enabled:
            from legal_research.official_web import OfficialWebLegalResearchProvider
            self._provider = OfficialWebLegalResearchProvider(
                timeout_seconds=settings.official_research_timeout_seconds
            )
        else:
            self._provider = UnconfiguredLegalResearchProvider()

    async def search(self, query: str, *, act: str | None = None, limit: int = 10) -> list[LegalResearchResult]:
        clean = (query or "").strip()
        if not clean:
            return []
        return await self._provider.search(clean, act=act, limit=max(1, min(limit, 20)))
