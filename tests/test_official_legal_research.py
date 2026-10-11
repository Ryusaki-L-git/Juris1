from __future__ import annotations

import httpx
import pytest

from legal_research.official_web import OfficialWebLegalResearchProvider


CJI_HTML = """<!doctype html><html><head><title>Chief Justice &amp; Judges | Supreme Court of India</title></head>
<body><nav>Menu</nav><main><h1>Chief Justice of India</h1>
<p>Hon'ble Justice Surya Kant</p><p>Sworn in as the Chief Justice of India on 24th November, 2025.</p>
<p>Due to retire on 09th February, 2027.</p></main><footer>Contact us</footer></body></html>"""


@pytest.mark.asyncio
async def test_cji_query_returns_official_source_and_retrieval_metadata() -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=CJI_HTML)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    provider = OfficialWebLegalResearchProvider(client=client)
    try:
        results = await provider.search("Who is the current Chief Justice of India? State appointment date.")
    finally:
        await client.aclose()

    assert len(results) == 1
    result = results[0]
    assert result.verified is True
    assert result.source_name == "Supreme Court of India"
    assert result.source_url == "https://www.sci.gov.in/chief-justice-judges/"
    assert "Surya Kant" in result.excerpt
    assert "24th November, 2025" in result.excerpt
    assert result.metadata["verification_method"] == "allowlisted_official_https_source"
    assert result.metadata["retrieved_at"]
    assert len(requested) == 1


@pytest.mark.asyncio
async def test_unrelated_query_does_not_fetch_any_source() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No source should be fetched for an unsupported query")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OfficialWebLegalResearchProvider(client=client)
    try:
        assert await provider.search("What is the weather in Rotterdam?") == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_outside_official_allowlist_is_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/not-official"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    provider = OfficialWebLegalResearchProvider(client=client)
    try:
        assert await provider.search("current Chief Justice of India") == []
    finally:
        await client.aclose()
