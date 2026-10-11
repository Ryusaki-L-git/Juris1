"""Provider/model router.

The orchestrator knows only this router. Provider SDKs, endpoints, and
credentials remain behind gateway/ and credential boundaries.
"""
from __future__ import annotations

from ai.contracts import AIModelSelection, AIProvider, AIUseMode
from ai.credentials import (
    CredentialResolver,
    DisabledBYOKCredentialResolver,
    FirestoreBYOKVault,
    ManagedCredentialResolver,
)
from core.config import settings
from core.errors import ConfigurationError
from gateway.base import JurisModelGateway
from gateway.echo_gateway import EchoGateway
from gateway.gemini_gateway import GeminiGateway
from gateway.openai_compatible_gateway import OpenAICompatibleGateway


class AIModelRouter:
    def __init__(
        self,
        managed_credentials: CredentialResolver | None = None,
        byok_credentials: CredentialResolver | None = None,
    ) -> None:
        self._managed_credentials = managed_credentials or ManagedCredentialResolver()
        if byok_credentials is not None:
            self._byok_credentials = byok_credentials
        elif settings.byok_enabled:
            self._byok_credentials = FirestoreBYOKVault()
        else:
            self._byok_credentials = DisabledBYOKCredentialResolver()

    async def gateway_for(self, user_id: str, selection: AIModelSelection) -> JurisModelGateway:
        if selection.mode is AIUseMode.MANAGED and selection.provider is AIProvider.LOCAL:
            raise ConfigurationError("Local models are available only through a user-managed/self-hosted endpoint.")
        resolver = self._managed_credentials if selection.mode is AIUseMode.MANAGED else self._byok_credentials
        credentials = await resolver.resolve(user_id, selection)
        model = selection.model.strip()

        if selection.provider is AIProvider.GEMINI:
            return GeminiGateway(api_key=credentials.api_key, model=model or settings.effective_gemini_model)

        if selection.provider is AIProvider.OPENAI_COMPATIBLE:
            return OpenAICompatibleGateway(
                provider_name="openai_compatible",
                api_key=credentials.api_key,
                model=model or settings.openai_model,
                base_url=credentials.base_url or settings.openai_base_url,
                timeout=settings.model_timeout,
            )

        if selection.provider is AIProvider.HUGGINGFACE:
            return OpenAICompatibleGateway(
                provider_name="huggingface",
                api_key=credentials.api_key,
                model=model or settings.huggingface_model,
                base_url=credentials.base_url or settings.huggingface_base_url,
                timeout=settings.model_timeout,
            )

        if selection.provider is AIProvider.LOCAL:
            return OpenAICompatibleGateway(
                provider_name="local",
                api_key=credentials.api_key,
                model=model or settings.local_model,
                base_url=credentials.base_url or settings.local_model_base_url,
                timeout=settings.model_timeout,
                require_api_key=False,
            )

        raise ConfigurationError(f"No provider adapter is registered for '{selection.provider.value}'.")

    def default_gateway(self) -> JurisModelGateway:
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
        raise ConfigurationError(f"Unsupported default gateway '{settings.gateway}'.")
