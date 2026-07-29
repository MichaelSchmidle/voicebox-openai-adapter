from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from voicebox_openai_adapter.config import Settings
from voicebox_openai_adapter.errors import AdapterError

MAX_UPSTREAM_JSON_BYTES = 1_048_576
MAX_UPSTREAM_SSE_BYTES = 1_048_576
MAX_SSE_EVENT_BYTES = 65_536
ACTIVE_GENERATION_STATUSES = frozenset({"generating", "loading_model"})
FAILED_GENERATION_STATUSES = frozenset({"failed", "error", "cancelled", "canceled", "not_found"})


@dataclass(frozen=True, slots=True)
class UpstreamAudio:
    body: bytes
    content_type: str | None


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    id: str
    name: str
    language: str


@dataclass(frozen=True, slots=True)
class Generation:
    id: str
    status: str


class VoiceboxClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/json",
            "X-Voicebox-Client-Id": settings.voicebox_client_id,
        }
        if settings.voicebox_api_key is not None:
            headers["Authorization"] = f"Bearer {settings.voicebox_api_key}"

        base_url = f"{str(settings.voicebox_base_url).rstrip('/')}/"
        timeout = httpx.Timeout(settings.voicebox_request_timeout_seconds)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self._health_timeout = settings.voicebox_health_timeout_seconds
        self._max_audio_bytes = settings.max_audio_bytes
        self._request_timeout = settings.voicebox_request_timeout_seconds

    async def close(self) -> None:
        await self._client.aclose()

    async def synthesize(self, payload: dict[str, object]) -> UpstreamAudio:
        try:
            async with asyncio.timeout(self._request_timeout):
                generation = await self._start_synthesis(payload)
                if generation.status != "completed":
                    await self._wait_for_completion(generation.id)
                return await self._fetch_audio(generation.id)
        except AdapterError:
            raise
        except TimeoutError as exc:
            raise AdapterError(
                504,
                "Upstream synthesis timed out",
                "upstream_error",
                "voicebox_timeout",
            ) from exc

    async def _start_synthesis(self, payload: dict[str, object]) -> Generation:
        try:
            async with self._client.stream("POST", "speak", json=payload) as response:
                self._raise_for_synthesis_status(response)
                raw_response = await self._read_bounded_json(response)
        except AdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise AdapterError(
                504,
                "Upstream synthesis timed out",
                "upstream_error",
                "voicebox_timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                502,
                "Voicebox is unavailable",
                "upstream_error",
                "voicebox_unavailable",
            ) from exc

        try:
            data = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(
                502,
                "Voicebox returned an invalid synthesis response",
                "upstream_error",
                "voicebox_protocol_error",
            ) from exc
        if not isinstance(data, dict):
            raise AdapterError(
                502,
                "Voicebox returned an invalid synthesis response",
                "upstream_error",
                "voicebox_protocol_error",
            )

        generation_id = data.get("id")
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise AdapterError(
                502,
                "Voicebox did not return a generation identifier",
                "upstream_error",
                "voicebox_protocol_error",
            )
        if (
            len(generation_id) > 512
            or generation_id in {".", ".."}
            or any(
                character in {"/", "\\"} or ord(character) < 32 or ord(character) == 127
                for character in generation_id
            )
        ):
            raise AdapterError(
                502,
                "Voicebox returned an invalid generation identifier",
                "upstream_error",
                "voicebox_protocol_error",
            )
        status = self._parse_generation_status(data)
        return Generation(id=generation_id, status=status)

    async def _wait_for_completion(self, generation_id: str) -> None:
        safe_generation_id = quote(generation_id, safe="")
        try:
            async with self._client.stream(
                "GET",
                f"generate/{safe_generation_id}/status",
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code >= 400:
                    raise self._status_stream_error()
                content_type = response.headers.get("Content-Type", "")
                if content_type.partition(";")[0].strip().lower() != "text/event-stream":
                    raise self._protocol_error()

                async for event_data in self._iter_sse_data(response):
                    status = self._parse_sse_status(event_data)
                    if status == "completed":
                        return
        except AdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise AdapterError(
                504,
                "Upstream synthesis timed out",
                "upstream_error",
                "voicebox_timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise self._status_stream_error() from exc

        raise self._status_stream_error()

    async def _fetch_audio(self, generation_id: str) -> UpstreamAudio:
        safe_generation_id = quote(generation_id, safe="")
        try:
            async with self._client.stream(
                "GET",
                f"audio/{safe_generation_id}",
            ) as response:
                if response.status_code >= 400:
                    raise AdapterError(
                        502,
                        "Voicebox audio download failed",
                        "upstream_error",
                        "voicebox_audio_error",
                    )

                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > self._max_audio_bytes:
                            raise self._audio_too_large()
                    except ValueError:
                        pass

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self._max_audio_bytes:
                        raise self._audio_too_large()
                    body.extend(chunk)
        except AdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise AdapterError(
                504,
                "Voicebox audio download timed out",
                "upstream_error",
                "voicebox_timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                502,
                "Voicebox audio download failed",
                "upstream_error",
                "voicebox_audio_error",
            ) from exc

        if not body:
            raise AdapterError(
                502,
                "Voicebox returned empty audio",
                "upstream_error",
                "voicebox_audio_error",
            )
        return UpstreamAudio(
            body=bytes(body),
            content_type=response.headers.get("Content-Type"),
        )

    @classmethod
    def _parse_sse_status(cls, event_data: bytes) -> str:
        try:
            data = json.loads(event_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise cls._protocol_error() from exc
        if not isinstance(data, dict):
            raise cls._protocol_error()
        return cls._parse_generation_status(data)

    @staticmethod
    def _parse_generation_status(data: dict[str, Any]) -> str:
        status = data.get("status")
        if not isinstance(status, str) or not status.strip():
            raise VoiceboxClient._protocol_error()
        normalized = status.strip().lower()
        if normalized in FAILED_GENERATION_STATUSES:
            raise AdapterError(
                502,
                "Voicebox generation failed",
                "upstream_error",
                "voicebox_generation_failed",
            )
        if normalized != "completed" and normalized not in ACTIVE_GENERATION_STATUSES:
            raise VoiceboxClient._protocol_error()
        return normalized

    @staticmethod
    async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[bytes]:
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None:
            try:
                if int(declared_length) > MAX_UPSTREAM_SSE_BYTES:
                    raise VoiceboxClient._status_too_large()
            except ValueError:
                pass

        total_bytes = 0
        event_bytes = 0
        buffer = bytearray()
        data_lines: list[bytes] = []

        async for chunk in response.aiter_bytes():
            total_bytes += len(chunk)
            if total_bytes > MAX_UPSTREAM_SSE_BYTES:
                raise VoiceboxClient._status_too_large()
            buffer.extend(chunk)

            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    if event_bytes + len(buffer) > MAX_SSE_EVENT_BYTES:
                        raise VoiceboxClient._status_too_large()
                    break

                raw_line = bytes(buffer[:newline_index])
                del buffer[: newline_index + 1]
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]

                event_bytes += newline_index + 1
                if event_bytes > MAX_SSE_EVENT_BYTES:
                    raise VoiceboxClient._status_too_large()

                if not raw_line:
                    if data_lines:
                        yield b"\n".join(data_lines)
                    data_lines.clear()
                    event_bytes = 0
                    continue
                if raw_line.startswith(b":"):
                    continue

                field, separator, value = raw_line.partition(b":")
                if field == b"data":
                    if separator and value.startswith(b" "):
                        value = value[1:]
                    data_lines.append(value)

    async def list_profiles(self) -> list[VoiceProfile]:
        try:
            async with self._client.stream("GET", "profiles") as response:
                if response.status_code >= 400:
                    raise AdapterError(
                        502,
                        "Voicebox profile request failed",
                        "upstream_error",
                        "voicebox_profiles_error",
                    )
                raw_response = await self._read_bounded_json(response)
        except AdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise AdapterError(
                504,
                "Voicebox profile request timed out",
                "upstream_error",
                "voicebox_timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                502,
                "Voicebox is unavailable",
                "upstream_error",
                "voicebox_unavailable",
            ) from exc
        try:
            data: Any = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._profiles_protocol_error() from exc
        if isinstance(data, dict):
            data = data.get("profiles")
        if not isinstance(data, list):
            raise self._profiles_protocol_error()

        profiles: list[VoiceProfile] = []
        for item in data:
            if not isinstance(item, dict):
                raise self._profiles_protocol_error()
            profile_id = item.get("id")
            name = item.get("name")
            language = item.get("language")
            if (
                not isinstance(profile_id, str)
                or not profile_id
                or not isinstance(name, str)
                or not name
                or not isinstance(language, str)
                or not language
            ):
                raise self._profiles_protocol_error()
            profiles.append(VoiceProfile(id=profile_id, name=name, language=language))
        return profiles

    async def is_healthy(self) -> bool:
        try:
            async with self._client.stream(
                "GET",
                "health",
                timeout=self._health_timeout,
            ) as response:
                if response.status_code >= 400:
                    return False
                raw_response = await self._read_bounded_json(response)
            data = json.loads(raw_response)
        except (AdapterError, httpx.HTTPError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        status = data.get("status")
        if isinstance(status, str):
            return status.lower() in {"healthy", "ok", "ready"}
        return data.get("healthy") is True

    @staticmethod
    def _raise_for_synthesis_status(response: httpx.Response) -> None:
        if response.status_code in {429, 503}:
            raise AdapterError(
                503,
                "Voicebox is saturated",
                "upstream_error",
                "voicebox_saturated",
            )
        if response.status_code == 404:
            raise AdapterError(
                404,
                "Voicebox did not find the requested voice profile",
                "invalid_request_error",
                "voicebox_profile_not_found",
            )
        if 400 <= response.status_code < 500:
            raise AdapterError(
                400,
                "Voicebox rejected the synthesis request",
                "invalid_request_error",
                "voicebox_request_rejected",
            )
        if response.status_code >= 500:
            raise AdapterError(
                502,
                "Voicebox synthesis failed",
                "upstream_error",
                "voicebox_error",
            )

    @staticmethod
    def _audio_too_large() -> AdapterError:
        return AdapterError(
            502,
            "Voicebox audio exceeded the size limit",
            "upstream_error",
            "voicebox_audio_too_large",
        )

    @staticmethod
    def _profiles_protocol_error() -> AdapterError:
        return AdapterError(
            502,
            "Voicebox returned an invalid profile response",
            "upstream_error",
            "voicebox_protocol_error",
        )

    @staticmethod
    def _protocol_error() -> AdapterError:
        return AdapterError(
            502,
            "Voicebox returned invalid generation status data",
            "upstream_error",
            "voicebox_protocol_error",
        )

    @staticmethod
    def _status_stream_error() -> AdapterError:
        return AdapterError(
            502,
            "Voicebox generation status stream failed",
            "upstream_error",
            "voicebox_status_error",
        )

    @staticmethod
    def _status_too_large() -> AdapterError:
        return AdapterError(
            502,
            "Voicebox response exceeded the size limit",
            "upstream_error",
            "voicebox_response_too_large",
        )

    @staticmethod
    async def _read_bounded_json(response: httpx.Response) -> bytes:
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None:
            try:
                if int(declared_length) > MAX_UPSTREAM_JSON_BYTES:
                    raise VoiceboxClient._json_too_large()
            except ValueError:
                pass

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MAX_UPSTREAM_JSON_BYTES:
                raise VoiceboxClient._json_too_large()
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _json_too_large() -> AdapterError:
        return AdapterError(
            502,
            "Voicebox response exceeded the size limit",
            "upstream_error",
            "voicebox_response_too_large",
        )
