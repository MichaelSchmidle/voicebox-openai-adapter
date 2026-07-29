from __future__ import annotations

import logging
import math
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Protocol

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from voicebox_openai_adapter.audio import (
    CONTENT_TYPES,
    FILE_EXTENSIONS,
    AudioConverter,
    AudioFormat,
    audio_matches_format,
    detect_audio_format,
)
from voicebox_openai_adapter.config import Settings
from voicebox_openai_adapter.errors import AdapterError
from voicebox_openai_adapter.voicebox import VoiceboxClient

LOGGER = logging.getLogger("voicebox_openai_adapter")
MODEL_ALIASES = frozenset({"voicebox", "tts-1", "tts-1-hd"})
STOCK_VOICES = frozenset({"alloy", "echo", "fable", "onyx", "nova", "shimmer"})


class Converter(Protocol):
    async def convert(
        self,
        audio: bytes,
        source_format: AudioFormat | None,
        target_format: AudioFormat,
        speed: float,
    ) -> bytes: ...


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=10_000)
    voice: str = Field(min_length=1, max_length=512)
    response_format: AudioFormat = AudioFormat.mp3
    speed: float = Field(default=1.0, ge=0.25, le=4.0, allow_inf_nan=False)
    language: str | None = Field(default=None, min_length=1, max_length=32)
    engine: str | None = Field(default=None, min_length=1, max_length=64)
    personality: bool | None = None

    @field_validator("input", "model", "voice", "language", "engine")
    @classmethod
    def reject_blank_strings(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


def create_app(
    *,
    settings: Settings,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    converter: Converter | None = None,
) -> FastAPI:
    audio_converter: Converter = converter or AudioConverter(
        timeout_seconds=settings.ffmpeg_timeout_seconds,
        max_output_bytes=settings.max_audio_bytes,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.voicebox = VoiceboxClient(settings, transport=upstream_transport)
        app.state.converter = audio_converter
        yield
        await app.state.voicebox.close()

    app = FastAPI(
        title="Voicebox OpenAI Adapter",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(AdapterError)
    async def adapter_error_handler(_request: Request, exc: AdapterError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.body(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        error = AdapterError(
            422,
            "Request validation failed",
            "invalid_request_error",
            "invalid_request",
        )
        return JSONResponse(status_code=error.status_code, content=error.body())

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        error = AdapterError(
            500,
            "Internal server error",
            "server_error",
            "internal_error",
        )
        return JSONResponse(status_code=error.status_code, content=error.body())

    @app.middleware("http")
    async def request_metadata_logging(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            route_object = request.scope.get("route")
            route = getattr(route_object, "path", "unknown")
            LOGGER.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "route": route,
                    "status": status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )

    @app.get("/healthz")
    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readiness(request: Request) -> JSONResponse:
        healthy = await _voicebox(request).is_healthy()
        if healthy:
            return JSONResponse(status_code=200, content={"status": "ready"})
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    async def authenticate(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        scheme = ""
        candidate = ""
        if authorization is not None:
            scheme, separator, candidate = authorization.partition(" ")
            if not separator:
                candidate = ""
        token_matches = secrets.compare_digest(
            candidate.encode("utf-8"),
            settings.adapter_api_key.encode("utf-8"),
        )
        if scheme.lower() != "bearer" or not candidate or not token_matches:
            raise AdapterError(
                401,
                "Invalid API key",
                "authentication_error",
                "invalid_api_key",
                {"WWW-Authenticate": "Bearer"},
            )

    authenticated = APIRouter(
        prefix="/v1",
        dependencies=[Depends(authenticate)],
    )

    @authenticated.get("/models")
    async def models() -> dict[str, object]:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "voicebox",
                }
                for model_id in ("voicebox", "tts-1", "tts-1-hd")
            ],
        }

    @authenticated.get("/audio/voices")
    async def voices(request: Request) -> dict[str, object]:
        profiles = await _voicebox(request).list_profiles()
        return {
            "object": "list",
            "data": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "language": profile.language,
                }
                for profile in profiles
            ],
        }

    @authenticated.post("/audio/speech")
    async def speech(body: SpeechRequest, request: Request) -> Response:
        if len(body.input) > settings.max_input_chars:
            raise AdapterError(
                422,
                "Request validation failed",
                "invalid_request_error",
                "invalid_request",
            )
        engine = _resolve_engine(body, settings)
        payload: dict[str, object] = {
            "text": body.input,
            "engine": engine,
        }
        profile = _resolve_profile(body.voice, settings)
        if profile is not None:
            payload["profile"] = profile
        if body.language is not None:
            payload["language"] = body.language
        if body.personality is not None:
            payload["personality"] = body.personality

        generation_id = await _voicebox(request).synthesize(payload)
        upstream_audio = await _voicebox(request).fetch_audio(generation_id)
        source_format = detect_audio_format(upstream_audio.content_type, upstream_audio.body)

        if source_format is body.response_format and math.isclose(body.speed, 1.0):
            output = upstream_audio.body
        else:
            output = await _converter(request).convert(
                upstream_audio.body,
                source_format,
                body.response_format,
                body.speed,
            )
        if not audio_matches_format(output, body.response_format):
            raise AdapterError(
                502,
                "Audio conversion returned an unexpected format",
                "upstream_error",
                "ffmpeg_format_mismatch",
            )

        headers = {
            "Content-Disposition": (
                f'inline; filename="speech.{FILE_EXTENSIONS[body.response_format]}"'
            )
        }
        return Response(
            content=output,
            media_type=CONTENT_TYPES[body.response_format],
            headers=headers,
        )

    app.include_router(authenticated)
    return app


def _voicebox(request: Request) -> VoiceboxClient:
    client: VoiceboxClient = request.app.state.voicebox
    return client


def _converter(request: Request) -> Converter:
    converter: Converter = request.app.state.converter
    return converter


def _resolve_engine(body: SpeechRequest, settings: Settings) -> str:
    if body.model not in MODEL_ALIASES:
        raise AdapterError(
            400,
            "Unsupported model",
            "invalid_request_error",
            "unsupported_model",
        )
    if body.engine is None:
        return settings.voicebox_default_engine
    if body.engine not in settings.allowed_engines:
        raise AdapterError(
            400,
            "Engine is not allowed",
            "invalid_request_error",
            "engine_not_allowed",
        )
    return body.engine


def _resolve_profile(voice: str, settings: Settings) -> str | None:
    if voice.lower() in STOCK_VOICES:
        return settings.voicebox_default_profile
    return voice
