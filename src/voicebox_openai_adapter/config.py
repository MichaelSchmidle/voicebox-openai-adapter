from __future__ import annotations

from typing import Literal

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_ENGINES = frozenset(
    {
        "qwen",
        "qwen_custom_voice",
        "luxtts",
        "chatterbox",
        "chatterbox_turbo",
        "tada",
        "kokoro",
    }
)
DEFAULT_ALLOWED_ENGINES = ",".join(sorted(SUPPORTED_ENGINES))


class Settings(BaseSettings):
    """Environment-only service configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        populate_by_name=True,
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    adapter_api_key: str = Field(alias="ADAPTER_API_KEY", min_length=1, repr=False)
    voicebox_base_url: HttpUrl = Field(
        default=HttpUrl("http://voicebox:17493"),
        alias="VOICEBOX_BASE_URL",
    )
    voicebox_api_key: str | None = Field(
        default=None,
        alias="VOICEBOX_API_KEY",
        repr=False,
    )
    voicebox_client_id: str = Field(
        default="openai-adapter",
        alias="VOICEBOX_CLIENT_ID",
        min_length=1,
        max_length=128,
    )
    voicebox_default_profile: str | None = Field(
        default=None,
        alias="VOICEBOX_DEFAULT_PROFILE",
        repr=False,
    )
    voicebox_default_engine: str = Field(
        default="qwen",
        alias="VOICEBOX_DEFAULT_ENGINE",
    )
    voicebox_allowed_engines: str = Field(
        default=DEFAULT_ALLOWED_ENGINES,
        alias="VOICEBOX_ALLOWED_ENGINES",
    )
    voicebox_request_timeout_seconds: float = Field(
        default=600,
        alias="VOICEBOX_REQUEST_TIMEOUT_SECONDS",
        gt=0,
    )
    voicebox_health_timeout_seconds: float = Field(
        default=5,
        alias="VOICEBOX_HEALTH_TIMEOUT_SECONDS",
        gt=0,
    )
    max_input_chars: int = Field(
        default=4096,
        alias="MAX_INPUT_CHARS",
        ge=1,
        le=10_000,
    )
    max_audio_bytes: int = Field(
        default=104_857_600,
        alias="MAX_AUDIO_BYTES",
        gt=0,
    )
    ffmpeg_timeout_seconds: float = Field(
        default=120,
        alias="FFMPEG_TIMEOUT_SECONDS",
        gt=0,
    )
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = Field(
        default="INFO",
        alias="LOG_LEVEL",
    )

    @field_validator("adapter_api_key", "voicebox_client_id")
    @classmethod
    def reject_blank_required_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("adapter_api_key", "voicebox_api_key", "voicebox_client_id")
    @classmethod
    def validate_header_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.isascii() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("must contain printable ASCII header characters only")
        return value

    @field_validator("voicebox_api_key", "voicebox_default_profile")
    @classmethod
    def reject_blank_optional_strings(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank when configured")
        return value

    @field_validator("voicebox_base_url")
    @classmethod
    def validate_base_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("must not include credentials, a query, or a fragment")
        return value

    @field_validator("voicebox_allowed_engines")
    @classmethod
    def validate_allowed_engine_string(cls, value: str) -> str:
        engines = [item.strip() for item in value.split(",")]
        if not engines or any(not item for item in engines):
            raise ValueError("must contain one or more comma-separated engines")
        unknown = set(engines) - SUPPORTED_ENGINES
        if unknown:
            raise ValueError("contains unsupported engines")
        return ",".join(dict.fromkeys(engines))

    @field_validator("voicebox_default_engine")
    @classmethod
    def validate_default_engine(cls, value: str) -> str:
        if value not in SUPPORTED_ENGINES:
            raise ValueError("is not a supported Voicebox engine")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_engine_relationship(self) -> Settings:
        if self.voicebox_default_engine not in self.allowed_engines:
            raise ValueError("VOICEBOX_DEFAULT_ENGINE must be in VOICEBOX_ALLOWED_ENGINES")
        return self

    @property
    def allowed_engines(self) -> frozenset[str]:
        return frozenset(self.voicebox_allowed_engines.split(","))
