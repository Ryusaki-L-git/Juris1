"""LED-facing AI provider preference contracts."""
from __future__ import annotations
from pydantic import BaseModel, Field
from ai.contracts import AIProvider, AIUseMode

class AIConfigRequest(BaseModel):
    provider: AIProvider
    model: str = Field(..., min_length=1, max_length=200)
    mode: AIUseMode = AIUseMode.MANAGED
    api_key: str | None = Field(default=None, max_length=4096)

class AIConfigResponse(BaseModel):
    provider: AIProvider
    model: str
    mode: AIUseMode
    credential_configured: bool
