from core.config import Settings


def test_runtime_defaults_fail_closed(monkeypatch):
    for name in ("JURIS_ENV", "JURIS_GATEWAY", "JURIS_AUTH_MODE"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.env == "production"
    assert settings.gateway == "gemini"
    assert settings.auth_mode == "firebase"


def test_development_mode_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("JURIS_ENV", "development")
    monkeypatch.setenv("JURIS_AUTH_MODE", "dev")

    settings = Settings(_env_file=None)

    assert settings.is_development
    assert settings.auth_mode == "dev"
