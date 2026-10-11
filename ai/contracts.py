"""Provider-neutral AI contracts used by the JURIS model router."""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field

class AIProvider(str, Enum):
    GEMINI = "gemini"
    OPENAI_COMPATIBLE = "openai_compatible"
    HUGGINGFACE = "huggingface"
    LOCAL = "local"

class AIUseMode(str, Enum):
    MANAGED = "managed"
    BYOK = "byok"

class AIModelSelection(BaseModel):
    provider: AIProvider
    model: str
    mode: AIUseMode = AIUseMode.MANAGED

class UserAIConfig(BaseModel):
    """Non-secret model preference. Secrets are resolved server-side."""
    provider: AIProvider
    model: str
    mode: AIUseMode

class ProviderCredentials(BaseModel):
    """Ephemeral credentials. Never persist this object in logs or API responses."""
    api_key: str | None = None
    base_url: str | None = None

class ProviderRequest(BaseModel):
    selection: AIModelSelection
    credentials: ProviderCredentials | None = None
