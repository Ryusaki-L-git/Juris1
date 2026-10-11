"""Credential-resolution boundary for managed AI and user BYOK.

The model/orchestrator never receives a raw API key. A provider adapter gets
an ephemeral credential object only at the final provider boundary.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from ai.contracts import AIModelSelection, AIProvider, AIUseMode, ProviderCredentials
from core.errors import ConfigurationError
from core.config import settings

class CredentialResolver(ABC):
    @abstractmethod
    async def resolve(self, user_id: str, selection: AIModelSelection) -> ProviderCredentials:
        ...

class ManagedCredentialResolver(CredentialResolver):
    def _base_url_for(self, selection: AIModelSelection) -> str | None:
        if selection.provider.value == "openai_compatible":
            return settings.openai_base_url
        if selection.provider.value == "huggingface":
            return settings.huggingface_base_url
        if selection.provider.value == "local":
            return settings.local_model_base_url
        return None

    async def resolve(self, user_id: str, selection: AIModelSelection) -> ProviderCredentials:
        if selection.mode is not AIUseMode.MANAGED:
            raise ConfigurationError("Managed credential resolver received a BYOK request.")
        if selection.provider is AIProvider.GEMINI:
            key = settings.effective_gemini_api_key.strip()
            if not key:
                raise ConfigurationError("Managed Gemini credentials are not configured.")
            return ProviderCredentials(api_key=key, base_url=None)
        if selection.provider is AIProvider.OPENAI_COMPATIBLE:
            key = settings.effective_openai_api_key.strip()
            if not key:
                raise ConfigurationError("Managed OpenAI-compatible credentials are not configured.")
            return ProviderCredentials(api_key=key, base_url=settings.openai_base_url)
        if selection.provider is AIProvider.HUGGINGFACE:
            key = settings.huggingface_api_key.strip()
            if not key:
                raise ConfigurationError("Managed Hugging Face credentials are not configured.")
            return ProviderCredentials(api_key=key, base_url=settings.huggingface_base_url)
        if selection.provider is AIProvider.LOCAL:
            return ProviderCredentials(api_key=None, base_url=settings.local_model_base_url)
        raise ConfigurationError(f"Managed provider '{selection.provider.value}' is not configured.")

class DisabledBYOKCredentialResolver(CredentialResolver):
    """Safe default until a production secret vault is configured."""
    async def resolve(self, user_id: str, selection: AIModelSelection) -> ProviderCredentials:
        raise ConfigurationError(
            "User BYOK is not enabled on this JURIS server. Configure a production credential vault first."
        )


class FirestoreBYOKVault(CredentialResolver):
    """Encrypted user-key storage boundary.

    API keys are encrypted before Firestore persistence and decrypted only in
    memory immediately before a provider call. Production deployments should
    supply the encryption key from a secret manager/KMS-backed environment.
    """
    def __init__(self, client=None) -> None:
        if not settings.byok_enabled:
            raise ConfigurationError("User BYOK is disabled on this JURIS server.")
        if not settings.credential_encryption_key:
            raise ConfigurationError("BYOK encryption key is not configured.")
        from cryptography.fernet import Fernet
        self._fernet = Fernet(settings.credential_encryption_key.encode())
        if client is None:
            if not settings.firebase_project_id:
                raise ConfigurationError("Firebase is required for persistent BYOK credentials.")
            from core.firestore_client import get_firestore_client
            client = get_firestore_client()
        self._client = client

    def _ref(self, user_id: str, provider: str):
        return (self._client.collection("_juris_credentials").document(user_id)
                .collection("providers").document(provider))

    async def resolve(self, user_id: str, selection: AIModelSelection) -> ProviderCredentials:
        import asyncio
        snap = await asyncio.to_thread(self._ref(user_id, selection.provider.value).get)
        if not snap.exists:
            raise ConfigurationError("No BYOK credential is configured for this provider.")
        encrypted = (snap.to_dict() or {}).get("api_key", "")
        if not encrypted:
            raise ConfigurationError("BYOK credential is empty.")
        return ProviderCredentials(api_key=self._fernet.decrypt(encrypted.encode()).decode())

    async def set_api_key(self, user_id: str, provider: AIProvider, api_key: str) -> None:
        import asyncio
        clean = api_key.strip()
        if not clean or len(clean) > 4096:
            raise ConfigurationError("Invalid API key.")
        encrypted = self._fernet.encrypt(clean.encode()).decode()
        await asyncio.to_thread(self._ref(user_id, provider.value).set, {"api_key": encrypted})

    async def delete_api_key(self, user_id: str, provider: AIProvider) -> None:
        import asyncio
        await asyncio.to_thread(self._ref(user_id, provider.value).delete)
