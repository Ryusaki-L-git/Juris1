from __future__ import annotations

import asyncio

from core.ai_usage_confirmations import AIUsageConfirmationStore


def test_ai_usage_quote_is_user_bound_and_single_use() -> None:
    async def run() -> None:
        store = AIUsageConfirmationStore()
        token = await store.issue(
            "user-a", "session-a", "Summarize this order", "gemini-3.8-flash", 1250
        )
        assert await store.consume(
            token, "user-b", "session-a", "Summarize this order", "gemini-3.8-flash"
        ) is None
        assert await store.consume(
            token, "user-a", "session-a", "Summarize a different order", "gemini-3.8-flash"
        ) is None
        assert await store.consume(
            token, "user-a", "session-a", "Summarize this order", "gemini-3.8-flash"
        ) == 1250
        assert await store.consume(
            token, "user-a", "session-a", "Summarize this order", "gemini-3.8-flash"
        ) is None

    asyncio.run(run())


def test_ai_usage_quote_is_bound_to_model_and_session() -> None:
    async def run() -> None:
        store = AIUsageConfirmationStore()
        token = await store.issue(
            "user-a", "session-a", "Explain limitation period", "gemini-3.8-flash", 800
        )
        assert await store.consume(
            token, "user-a", "session-b", "Explain limitation period", "gemini-3.8-flash"
        ) is None
        assert await store.consume(
            token, "user-a", "session-a", "Explain limitation period", "gemini-2.5-flash"
        ) is None
        assert await store.consume(
            token, "user-a", "session-a", "Explain limitation period", "gemini-3.8-flash"
        ) == 800

    asyncio.run(run())
