"""
core/config.py — JURIS centralised application settings.

All configuration is read from environment variables and / or a .env file.
No secrets are hardcoded here.

Usage anywhere in the application:
    from core.config import settings
    print(settings.env)
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    JURIS application settings.

    Values are loaded (in priority order) from:
      1. Real environment variables
      2. .env file at the project root
      3. Defaults defined here

    Add new configuration here — never in source code directly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="JURIS_",
        case_sensitive=False,
        extra="ignore",  # silently ignore unknown env vars
    )

    # ── Application ──────────────────────────────────────────────────────────
    # Fail closed if hosting omits JURIS_ENV. Local development must explicitly set JURIS_ENV=development.
    env: Literal["development", "staging", "production"] = "production"
    version: str = "0.4.0"
    debug: bool = False

    # ── API ──────────────────────────────────────────────────────────────────
    api_prefix: str = ""
    # Stored as a raw comma-separated string so pydantic-settings does not
    # attempt JSON decoding before we can parse it.
    # Always access origins via the `allowed_origins` property.
    allowed_origins_raw: str = "http://localhost"

    @property
    def allowed_origins(self) -> list[str]:
        """Return CORS origins as a list, parsed from the comma-separated env var."""
        return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]

    # ── Model Gateway ─────────────────────────────────────────────────────────
    # Echo is a development/testing adapter, never the implicit production default.
    gateway: Literal["echo", "gemini", "openai_compatible"] = "gemini"
    # Expand Literal when real providers are wired: "gemini" | "openai" | ...
    # Provider API keys are read here but never logged or exposed via the API.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6"
    openai_base_url: str = "https://api.openai.com/v1"
    huggingface_api_key: str = ""
    huggingface_model: str = "Qwen/Qwen2.5-7B-Instruct"
    huggingface_base_url: str = "https://router.huggingface.co/v1"
    local_model: str = "local-model"
    local_model_base_url: str = "http://127.0.0.1:1234/v1"
    model_timeout: float = 30.0
    byok_enabled: bool = False
    credential_encryption_key: str = ""
    max_tool_rounds: int = 5

    # Controlled official-source retrieval. Only the code-owned source registry
    # is queried; this does not enable arbitrary open-web or URL access.
    official_research_enabled: bool = True
    official_research_timeout_seconds: float = 8.0

    @property
    def effective_gemini_api_key(self) -> str:
        import os
        return self.gemini_api_key or os.getenv("GEMINI_API_KEY", "")

    @property
    def effective_openai_api_key(self) -> str:
        import os
        return self.openai_api_key or os.getenv("OPENAI_API_KEY", "")

    @property
    def effective_gemini_model(self) -> str:
        import os
        return self.gemini_model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

    # ── LED Integration ───────────────────────────────────────────────────────
    led_api_url: str = ""
    # Blank until the LED↔JURIS integration phase.

    # ── eCourts ────────────────────────────────────────────────────────────────
    # IMPORTANT: JURIS does NOT hold eCourts credentials.
    # The eCourts API key lives exclusively in the LED server-side integration.
    # JURIS only reads eCourts-sourced data that LED has already authorized and
    # stored in the user's Firestore case record. JURIS cannot call eCourts directly.

    # ── Firebase / Auth ───────────────────────────────────────────────────────
    # Local dev tokens must be opted into explicitly; production defaults to Firebase verification.
    auth_mode: Literal["firebase", "dev"] = "firebase"
    # "firebase": verifies incoming JWT against Firebase Auth.
    # "dev": allows local dev tokens for testing without Firebase.
    # In production/staging, "dev" is strictly forbidden.
    dev_allow_unauthenticated: bool = True
    # If True and in development mode, requests lacking an Authorization
    # header will be assigned a development fallback user (is_verified=False).
    # If False or in staging/production, missing auth yields HTTP 401.
    firebase_project_id: str = ""
    # Optional service-account JSON provided through a hosting secret.
    # Leave blank when the hosting platform supplies Application Default Credentials.
    firebase_service_account_json: str = ""

    # ── Case Token pricing (CENTRAL configuration point) ──────────────────────
    # The exact product prices for eCourts operations are NOT defined in the
    # repository or specification.  These values are the single place token
    # costs are declared.  A value of 0 means "cost not yet agreed" and
    # charging is skipped rather than inventing a business price.
    case_token_cost_ecourts_lookup: int = 0
    case_token_cost_ecourts_refresh: int = 0
    case_token_cost_monitor_create: int = 0

    # ── Automatic monitoring ─────────────────────────────────────────────────
    # The scheduler only runs when explicitly enabled, and never runs a tight
    # polling loop: it wakes on an interval and processes due monitors only.
    monitoring_enabled: bool = False
    monitor_interval_minutes: int = 180
    monitor_max_consecutive_failures: int = 5
    monitor_batch_size: int = 25

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def is_development(self) -> bool:
        return self.env == "development"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_auth_enforced(self) -> bool:
        """True if authentication is strictly enforced (non-dev or unauthenticated disabled)."""
        return not self.is_development or not self.dev_allow_unauthenticated


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance.

    Uses lru_cache so the .env file is read exactly once per process.
    In tests, call get_settings.cache_clear() to reload settings.
    """
    return Settings()


# Module-level convenience alias.
# Prefer importing this directly: `from core.config import settings`
settings: Settings = get_settings()
