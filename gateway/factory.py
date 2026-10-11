"""Default gateway factory for backwards-compatible application startup."""
from __future__ import annotations
from functools import lru_cache
from core.config import settings
from core.errors import ConfigurationError
from gateway.base import JurisModelGateway
from gateway.echo_gateway import EchoGateway
from gateway.gemini_gateway import GeminiGateway
from gateway.openai_compatible_gateway import OpenAICompatibleGateway


@lru_cache(maxsize=1)
def get_gateway() -> JurisModelGateway:
    if settings.gateway == "echo":
        return EchoGateway()
    if settings.gateway == "gemini":
        return GeminiGateway()
    if settings.gateway == "openai_compatible":
        return OpenAICompatibleGateway(
            provider_name="openai_compatible",
            api_key=settings.effective_openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout=settings.model_timeout,
        )
    raise ConfigurationError(f"Unknown JURIS gateway: {settings.gateway}")
