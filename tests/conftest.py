from __future__ import annotations

import json
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from voicebox_openai_adapter.app import create_app
from voicebox_openai_adapter.audio import AudioFormat
from voicebox_openai_adapter.config import Settings

UpstreamHandler = Callable[[httpx.Request], httpx.Response]


def wav_bytes() -> bytes:
    payload = struct.pack("<hhhh", 0, 100, -100, 0)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(payload))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 24_000, 48_000, 2, 16)
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )


class FakeConverter:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, AudioFormat | None, AudioFormat, float]] = []

    async def convert(
        self,
        audio: bytes,
        source_format: AudioFormat | None,
        target_format: AudioFormat,
        speed: float,
    ) -> bytes:
        self.calls.append((audio, source_format, target_format, speed))
        samples = {
            AudioFormat.mp3: b"ID3\x04\x00\x00converted",
            AudioFormat.opus: b"OggS\x00\x02converted-OpusHead",
            AudioFormat.aac: b"\xff\xf1\x50\x80converted",
            AudioFormat.flac: b"fLaCconverted",
            AudioFormat.wav: wav_bytes(),
            AudioFormat.pcm: struct.pack("<hhhh", 0, 100, -100, 0),
        }
        return samples[target_format]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        ADAPTER_API_KEY="adapter-test-secret",
        VOICEBOX_BASE_URL="https://voicebox.example.test",
        VOICEBOX_DEFAULT_ENGINE="qwen",
        VOICEBOX_ALLOWED_ENGINES="qwen,kokoro,chatterbox",
        MAX_AUDIO_BYTES=1024,
    )


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer adapter-test-secret"}


@pytest.fixture
def default_handler() -> UpstreamHandler:
    audio = wav_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            return httpx.Response(
                200,
                json={
                    "id": "generation-1",
                    "profile_id": "synthetic-profile",
                    "text": "not-used-by-adapter",
                    "language": "en",
                    "status": "completed",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            )
        if request.url.path == "/audio/generation-1":
            return httpx.Response(200, content=audio, headers={"Content-Type": "audio/wav"})
        if request.url.path == "/profiles":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "profile-1",
                        "name": "Synthetic",
                        "language": "en",
                        "reference_audio_path": "/must/not/escape",
                    }
                ],
            )
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "model_loaded": False})
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url.path}")

    return handler


@pytest.fixture
def client_for() -> Callable[..., Iterator[TestClient]]:
    @contextmanager
    def factory(
        handler: UpstreamHandler,
        *,
        settings: Settings,
        converter: Any | None = None,
    ) -> Iterator[TestClient]:
        app = create_app(
            settings=settings,
            upstream_transport=httpx.MockTransport(handler),
            converter=converter,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client

    return factory


def parse_request_json(request: httpx.Request) -> dict[str, Any]:
    value = json.loads(request.content)
    assert isinstance(value, dict)
    return value
