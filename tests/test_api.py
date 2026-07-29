from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from conftest import FakeConverter, UpstreamHandler, parse_request_json, wav_bytes
from fastapi.testclient import TestClient

from voicebox_openai_adapter.audio import AudioFormat
from voicebox_openai_adapter.config import Settings
from voicebox_openai_adapter.errors import AdapterError


def speech_request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "tts-1",
        "input": "Synthetic test sentence",
        "voice": "profile-1",
        "response_format": "wav",
        "speed": 1.0,
    }
    body.update(overrides)
    return body


def test_openwebui_shaped_request_translates_and_returns_audio(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    captured: list[httpx.Request] = []
    audio = wav_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/speak":
            return httpx.Response(200, json={"id": "gen-openwebui", "status": "completed"})
        return httpx.Response(200, content=audio, headers={"Content-Type": "audio/wav"})

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(),
        )

    assert response.status_code == 200
    assert response.content == audio
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["content-disposition"] == 'inline; filename="speech.wav"'
    assert response.headers["x-request-id"]
    assert [request.url.path for request in captured] == ["/speak", "/audio/gen-openwebui"]
    assert parse_request_json(captured[0]) == {
        "text": "Synthetic test sentence",
        "profile": "profile-1",
        "engine": "qwen",
    }


@pytest.mark.parametrize("profile", ["Synthetic Voice", "profile-f6b260"])
def test_profile_name_and_id_are_passed_through(
    profile: str,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            captured.update(parse_request_json(request))
        return default_handler(request)

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(voice=profile),
        )

    assert response.status_code == 200
    assert captured["profile"] == profile


@pytest.mark.parametrize("stock_voice", ["alloy", "echo", "fable", "onyx", "nova", "shimmer"])
def test_stock_voice_uses_configured_default_profile(
    stock_voice: str,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    configured = settings.model_copy(update={"voicebox_default_profile": "default-profile"})
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            captured.update(parse_request_json(request))
        return default_handler(request)

    with client_for(handler, settings=configured) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(voice=stock_voice),
        )

    assert response.status_code == 200
    assert captured["profile"] == "default-profile"


def test_stock_voice_omits_profile_without_configured_default(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            captured.update(parse_request_json(request))
        return default_handler(request)

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(voice="alloy"),
        )

    assert response.status_code == 200
    assert "profile" not in captured


@pytest.mark.parametrize("model", ["voicebox", "tts-1", "tts-1-hd"])
def test_model_aliases_map_to_default_engine(
    model: str,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    configured = settings.model_copy(update={"voicebox_default_engine": "kokoro"})
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            captured.update(parse_request_json(request))
        return default_handler(request)

    with client_for(handler, settings=configured) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(model=model),
        )

    assert response.status_code == 200
    assert captured["engine"] == "kokoro"


def test_allowed_explicit_engine_and_optional_extensions_are_forwarded(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            captured.update(parse_request_json(request))
        return default_handler(request)

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(engine="kokoro", language="de", personality=True),
        )

    assert response.status_code == 200
    assert captured == {
        "text": "Synthetic test sentence",
        "profile": "profile-1",
        "engine": "kokoro",
        "language": "de",
        "personality": True,
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (speech_request(model="arbitrary-upstream-engine"), "unsupported_model"),
        (speech_request(engine="luxtts"), "engine_not_allowed"),
        (speech_request(engine="made-up"), "engine_not_allowed"),
    ],
)
def test_unsupported_models_and_engines_return_400_without_upstream_call(
    body: dict[str, Any],
    code: str,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Upstream called unexpectedly: {request.url}")

    with client_for(handler, settings=settings) as client:
        response = client.post("/v1/audio/speech", headers=auth_headers, json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("target", "content_type", "prefix"),
    [
        ("mp3", "audio/mpeg", b"ID3"),
        ("opus", "audio/ogg", b"OggS"),
        ("aac", "audio/aac", b"\xff\xf1"),
        ("flac", "audio/flac", b"fLaC"),
        ("wav", "audio/wav", b"RIFF"),
        ("pcm", "audio/pcm; rate=24000; channels=1", b"\x00\x00"),
    ],
)
def test_all_response_formats_are_honestly_labeled(
    target: str,
    content_type: str,
    prefix: bytes,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    converter = FakeConverter()
    with client_for(default_handler, settings=settings, converter=converter) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(response_format=target, speed=1.25),
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.content.startswith(prefix)
    assert converter.calls[0][2] == AudioFormat(target)


@pytest.mark.parametrize("speed", [0.25, 4.0])
def test_speed_boundaries_are_accepted(
    speed: float,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    converter = FakeConverter()
    with client_for(default_handler, settings=settings, converter=converter) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(speed=speed),
        )

    assert response.status_code == 200
    assert converter.calls[0][3] == speed


def test_matching_format_and_normal_speed_bypass_ffmpeg(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    class FailingConverter:
        async def convert(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("converter must not be called")

    with client_for(default_handler, settings=settings, converter=FailingConverter()) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(response_format="wav", speed=1.0),
        )

    assert response.status_code == 200
    assert response.content == wav_bytes()


def test_mislabeled_upstream_audio_is_converted_instead_of_passed_through(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    converter = FakeConverter()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            return httpx.Response(200, json={"id": "generation-1", "status": "completed"})
        return httpx.Response(
            200,
            content=b"not-a-wave-container",
            headers={"Content-Type": "audio/wav"},
        )

    with client_for(handler, settings=settings, converter=converter) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(response_format="wav"),
        )

    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")
    assert converter.calls[0][1] is None


def test_converter_output_signature_must_match_claimed_format(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    class MismatchingConverter:
        async def convert(self, *_args: Any, **_kwargs: Any) -> bytes:
            return wav_bytes()

    with client_for(default_handler, settings=settings, converter=MismatchingConverter()) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(response_format="mp3"),
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ffmpeg_format_mismatch"


@pytest.mark.parametrize(
    "body",
    [
        speech_request(input=" "),
        speech_request(input="x" * 4097),
        speech_request(speed=0.24),
        speech_request(speed=4.01),
        speech_request(response_format="ogg"),
        {**speech_request(), "unexpected": True},
    ],
)
def test_invalid_requests_return_sanitized_422(
    body: dict[str, Any],
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Upstream called unexpectedly: {request.url}")

    with client_for(handler, settings=settings) as client:
        response = client.post("/v1/audio/speech", headers=auth_headers, json=body)

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "message": "Request validation failed",
            "type": "invalid_request_error",
            "code": "invalid_request",
        }
    }
    assert "x" * 100 not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic wrong"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer "},
    ],
)
def test_missing_or_invalid_client_key_returns_bearer_challenge(
    headers: dict[str, str],
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Upstream called unexpectedly: {request.url}")

    with client_for(handler, settings=settings) as client:
        response = client.get("/v1/models", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_upstream_key_is_forwarded_and_client_key_is_not(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    configured = settings.model_copy(update={"voicebox_api_key": "upstream-test-secret"})
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return default_handler(request)

    with client_for(handler, settings=configured) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(),
        )

    assert response.status_code == 200
    assert all(
        request.headers["authorization"] == "Bearer upstream-test-secret" for request in captured
    )
    assert all(request.headers["x-voicebox-client-id"] == "openai-adapter" for request in captured)
    assert all("adapter-test-secret" not in str(request.headers) for request in captured)


@pytest.mark.parametrize(
    ("upstream_result", "expected_status", "expected_code"),
    [
        (httpx.ReadTimeout("timed out"), 504, "voicebox_timeout"),
        (httpx.ConnectError("connection failed"), 502, "voicebox_unavailable"),
        (httpx.Response(404, text="private profile details"), 404, "voicebox_profile_not_found"),
        (httpx.Response(429, text="busy"), 503, "voicebox_saturated"),
        (httpx.Response(500, text="private traceback"), 502, "voicebox_error"),
        (httpx.Response(200, text="not json"), 502, "voicebox_protocol_error"),
        (httpx.Response(200, json={"status": "completed"}), 502, "voicebox_protocol_error"),
        (
            httpx.Response(200, json={"id": "gen", "status": "failed", "error": "private"}),
            502,
            "voicebox_generation_failed",
        ),
    ],
)
def test_synthesis_failures_are_normalized(
    upstream_result: Exception | httpx.Response,
    expected_status: int,
    expected_code: str,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(upstream_result, Exception):
            raise upstream_result
        return upstream_result

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(),
        )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert "private" not in response.text


def test_oversized_synthesis_metadata_is_rejected_before_audio_fetch(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "generation-1",
                "status": "completed",
                "padding": "x" * 1_048_576,
            },
        )

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(),
        )

    assert calls == 1
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "voicebox_response_too_large"


def test_generation_identifier_cannot_escape_audio_route(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": "../profiles", "status": "completed"})

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(),
        )

    assert calls == 1
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "voicebox_protocol_error"


@pytest.mark.parametrize(
    ("audio_response", "expected_code"),
    [
        (httpx.Response(500, text="private"), "voicebox_audio_error"),
        (
            httpx.Response(200, content=b"x" * 1025, headers={"Content-Type": "audio/mpeg"}),
            "voicebox_audio_too_large",
        ),
    ],
)
def test_audio_download_failures_are_normalized(
    audio_response: httpx.Response,
    expected_code: str,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/speak":
            return httpx.Response(200, json={"id": "generation-1", "status": "completed"})
        return audio_response

    with client_for(handler, settings=settings) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(),
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == expected_code
    assert "private" not in response.text


@pytest.mark.parametrize("code", ["ffmpeg_failed", "ffmpeg_timeout"])
def test_converter_failures_are_openai_shaped(
    code: str,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    class FailingConverter:
        async def convert(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise AdapterError(502, "Audio conversion failed", "upstream_error", code)

    with client_for(default_handler, settings=settings, converter=FailingConverter()) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(response_format="mp3"),
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == code


def test_unexpected_internal_failure_is_sanitized(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    class UnexpectedConverter:
        async def convert(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise RuntimeError("private internal detail")

    with client_for(default_handler, settings=settings, converter=UnexpectedConverter()) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(response_format="mp3"),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private internal detail" not in response.text


def test_privacy_canaries_never_reach_logs_or_errors(
    caplog: pytest.LogCaptureFixture,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    canary = "SPEECH-CANARY-DO-NOT-LOG-7b41"
    secret = "UPSTREAM-SECRET-DO-NOT-LOG-4a22"
    configured = settings.model_copy(update={"voicebox_api_key": secret})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"private body {canary} {secret}")

    caplog.set_level(logging.INFO)
    with client_for(handler, settings=configured) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=auth_headers,
            json=speech_request(input=canary),
        )

    combined = response.text + " ".join(record.getMessage() for record in caplog.records)
    assert response.status_code == 502
    assert canary not in combined
    assert secret not in combined


def test_models_and_sanitized_voices_endpoints(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
    default_handler: UpstreamHandler,
) -> None:
    with client_for(default_handler, settings=settings) as client:
        models = client.get("/v1/models", headers=auth_headers)
        voices = client.get("/v1/audio/voices", headers=auth_headers)

    assert models.status_code == 200
    assert [item["id"] for item in models.json()["data"]] == ["voicebox", "tts-1", "tts-1-hd"]
    assert voices.status_code == 200
    assert voices.json() == {
        "object": "list",
        "data": [{"id": "profile-1", "name": "Synthetic", "language": "en"}],
    }
    assert "reference_audio_path" not in voices.text


@pytest.mark.parametrize(
    "profiles_response",
    [
        httpx.Response(500, text="private"),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"unexpected": []}),
        httpx.Response(200, json=[{"id": "id", "name": "name"}]),
        httpx.Response(200, json=["not-an-object"]),
    ],
)
def test_profile_failures_are_sanitized(
    profiles_response: httpx.Response,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return profiles_response

    with client_for(handler, settings=settings) as client:
        response = client.get("/v1/audio/voices", headers=auth_headers)

    assert response.status_code == 502
    assert "private" not in response.text


def test_profile_object_envelope_is_supported(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "profiles": [
                    {"id": "profile-1", "name": "Synthetic", "language": "de"},
                ]
            },
        )

    with client_for(handler, settings=settings) as client:
        response = client.get("/v1/audio/voices", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["data"][0]["language"] == "de"


def test_liveness_never_calls_upstream(
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Liveness called upstream: {request.url}")

    with client_for(handler, settings=settings) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


@pytest.mark.parametrize(
    ("health_result", "expected"),
    [
        (httpx.Response(200, json={"status": "healthy", "model_loaded": False}), 200),
        (httpx.Response(200, json={"healthy": True, "model_loaded": False}), 200),
        (httpx.Response(200, json={"status": "unhealthy", "details": "private"}), 503),
        (httpx.Response(200, json=["not-an-object"]), 503),
        (httpx.Response(200, text="not-json"), 503),
        (httpx.Response(500, text="private"), 503),
        (httpx.ConnectError("private network"), 503),
    ],
)
def test_readiness_probes_upstream_and_sanitizes_result(
    health_result: httpx.Response | Exception,
    expected: int,
    client_for: Callable[..., Iterator[TestClient]],
    settings: Settings,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/health"
        if isinstance(health_result, Exception):
            raise health_result
        return health_result

    with client_for(handler, settings=settings) as client:
        response = client.get("/readyz")

    assert calls == 1
    assert response.status_code == expected
    assert response.json() in ({"status": "ready"}, {"status": "unavailable"})
    assert "private" not in response.text
