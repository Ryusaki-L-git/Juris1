"""Controlled retrieval from a small registry of official Indian legal sources.

This is deliberately NOT an open-web search or arbitrary-URL fetcher. Queries
select from a code-owned source registry; callers cannot provide URLs. Only
allowlisted official hosts are accepted, redirects are revalidated, and every
result records its retrieval timestamp and source URL.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlparse

import httpx

from legal_research.base import LegalResearchProvider, LegalResearchResult


_ALLOWED_HOSTS = frozenset({"www.sci.gov.in", "sci.gov.in"})
_MAX_RESPONSE_BYTES = 1_500_000


class _TextExtractor(HTMLParser):
    """Small standard-library HTML text extractor (no script/style content)."""

    _IGNORED = {"script", "style", "noscript", "svg", "header", "footer", "nav"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0
        self._title_depth = 0
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._IGNORED:
            self._ignored_depth += 1
        if tag == "title":
            self._title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in {"p", "div", "li", "h1", "h2", "h3", "br", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        clean = " ".join(data.split())
        if not clean:
            return
        if self._ignored_depth == 0:
            self.parts.append(clean)
        if self._title_depth:
            self.title_parts.append(clean)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()


class OfficialWebLegalResearchProvider(LegalResearchProvider):
    """Fetch matching pages from an explicit, trusted-source registry."""

    # Intentionally narrow initial coverage. Expand only with reviewed official
    # URLs and tests; do not turn this into user-controlled URL fetching.
    _SOURCES = (
        {
            "url": "https://www.sci.gov.in/chief-justice-judges/",
            "name": "Supreme Court of India",
            "title": "Chief Justice and Judges — Supreme Court of India",
            "keywords": (
                "chief justice", "chief justice of india", "cji", "surya kant",
                "chief justice appointment", "chief justice sworn", "current cji",
                "chief justice of the supreme court",
            ),
        },
    )

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = max(1.0, min(float(timeout_seconds), 15.0))
        self._client = client

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    @classmethod
    def _matching_sources(cls, query: str, act: str | None) -> Iterable[dict[str, object]]:
        normalized = cls._normalize(f"{query} {act or ''}")
        for source in cls._SOURCES:
            keywords = source["keywords"]
            if any(keyword in normalized for keyword in keywords):
                yield source

    @staticmethod
    def _extract_excerpt(text: str, query: str, max_chars: int = 1100) -> str:
        if len(text) <= max_chars:
            return text
        normalized = text.casefold()
        # Prefer the official appointment statement for CJI queries rather than
        # returning only the beginning of a long biographical page.
        priority_phrases = (
            "sworn in as the chief justice of india",
            "chief justice of india",
            "chief justice",
        )
        start_at = -1
        for phrase in priority_phrases:
            position = normalized.find(phrase)
            if position >= 0:
                start_at = max(0, position - 240)
                break
        if start_at < 0:
            query_terms = [term for term in re.findall(r"[a-z0-9]+", query.casefold()) if len(term) > 2]
            positions = [normalized.find(term) for term in query_terms if normalized.find(term) >= 0]
            start_at = max(0, min(positions) - 180) if positions else 0
        excerpt = text[start_at : start_at + max_chars].strip()
        if start_at:
            excerpt = "…" + excerpt
        if start_at + max_chars < len(text):
            excerpt += "…"
        return excerpt

    async def _fetch(self, url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
            raise ValueError("Source URL is not allowlisted")

        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
            headers={"User-Agent": "JURIS-OfficialResearch/1.0 (source verification)"},
        )
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                final = urlparse(str(response.url))
                if final.scheme != "https" or final.hostname not in _ALLOWED_HOSTS:
                    raise ValueError("Official source redirected outside the allowlist")
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type and "text/plain" not in content_type:
                    raise ValueError("Official source returned an unsupported content type")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        raise ValueError("Official source response exceeded the size limit")
                    chunks.append(chunk)
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return body, str(response.url)
        finally:
            if own_client:
                await client.aclose()

    async def search(
        self, query: str, *, act: str | None = None, limit: int = 10
    ) -> list[LegalResearchResult]:
        clean_query = (query or "").strip()
        if not clean_query:
            return []
        results: list[LegalResearchResult] = []
        for source in self._matching_sources(clean_query, act):
            try:
                html, final_url = await self._fetch(str(source["url"]))
                parser = _TextExtractor()
                parser.feed(html)
                text = parser.text
                if not text:
                    continue
                # The configured source itself is trusted for identity, but the
                # page is still treated as evidence, not executable instructions.
                excerpt = self._extract_excerpt(text, clean_query)
                results.append(
                    LegalResearchResult(
                        title=parser.title or str(source["title"]),
                        citation="",
                        excerpt=excerpt,
                        source_url=final_url,
                        source_name=str(source["name"]),
                        verified=True,
                        metadata={
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "verification_method": "allowlisted_official_https_source",
                            "source_host": urlparse(final_url).hostname,
                        },
                    )
                )
            except (httpx.HTTPError, UnicodeError, ValueError) as exc:
                # Do not leak connection details or turn a fetch failure into
                # an unverified answer. A missing result means not verified.
                continue
            if len(results) >= max(1, min(limit, 20)):
                break
        return results
