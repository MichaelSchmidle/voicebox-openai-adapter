from __future__ import annotations

import pytest
from pydantic import ValidationError

from voicebox_openai_adapter.config import Settings


def valid_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "_env_file": None,
        "ADAPTER_API_KEY": "synthetic-secret",
        "VOICEBOX_BASE_URL": "https://voicebox.example.test",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "overrides",
    [
        {"ADAPTER_API_KEY": ""},
        {"ADAPTER_API_KEY": "   "},
        {"ADAPTER_API_KEY": "secret\r\ninjected"},
        {"VOICEBOX_BASE_URL": "not-a-url"},
        {"VOICEBOX_BASE_URL": "ftp://voicebox.example.test"},
        {"VOICEBOX_API_KEY": ""},
        {"VOICEBOX_CLIENT_ID": ""},
        {"VOICEBOX_CLIENT_ID": "client\ninjected"},
        {"MAX_INPUT_CHARS": 0},
        {"MAX_INPUT_CHARS": 10_001},
        {"MAX_AUDIO_BYTES": 0},
        {"VOICEBOX_REQUEST_TIMEOUT_SECONDS": 0},
        {"VOICEBOX_ALLOWED_ENGINES": ""},
        {"VOICEBOX_ALLOWED_ENGINES": "qwen,arbitrary"},
        {"VOICEBOX_DEFAULT_ENGINE": "kokoro", "VOICEBOX_ALLOWED_ENGINES": "qwen"},
        {"LOG_LEVEL": "verbose"},
    ],
)
def test_invalid_configuration_fails_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**valid_settings(**overrides))


def test_missing_adapter_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADAPTER_API_KEY", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_defaults_are_secure_and_typed() -> None:
    settings = Settings(**valid_settings())

    assert str(settings.voicebox_base_url) == "https://voicebox.example.test/"
    assert settings.voicebox_default_engine == "qwen"
    assert "qwen" in settings.allowed_engines
    assert settings.max_input_chars == 4096
    assert settings.max_audio_bytes == 104_857_600
    assert settings.log_level == "INFO"


def test_configuration_errors_hide_secret_inputs() -> None:
    secret_canary = "SECRET-CANARY\ninvalid"

    with pytest.raises(ValidationError) as exc_info:
        Settings(**valid_settings(ADAPTER_API_KEY=secret_canary))

    assert secret_canary not in str(exc_info.value)
