from __future__ import annotations

import asyncio
import math
import tempfile
from enum import StrEnum
from pathlib import Path

from voicebox_openai_adapter.errors import AdapterError

PCM_SAMPLE_RATE = 24_000
PCM_CHANNELS = 1


class AudioFormat(StrEnum):
    mp3 = "mp3"
    opus = "opus"
    aac = "aac"
    flac = "flac"
    wav = "wav"
    pcm = "pcm"


CONTENT_TYPES: dict[AudioFormat, str] = {
    AudioFormat.mp3: "audio/mpeg",
    AudioFormat.opus: "audio/ogg",
    AudioFormat.aac: "audio/aac",
    AudioFormat.flac: "audio/flac",
    AudioFormat.wav: "audio/wav",
    AudioFormat.pcm: f"audio/pcm; rate={PCM_SAMPLE_RATE}; channels={PCM_CHANNELS}",
}

FILE_EXTENSIONS: dict[AudioFormat, str] = {
    AudioFormat.mp3: "mp3",
    AudioFormat.opus: "opus",
    AudioFormat.aac: "aac",
    AudioFormat.flac: "flac",
    AudioFormat.wav: "wav",
    AudioFormat.pcm: "pcm",
}

_CONTENT_TYPE_FORMATS = {
    "audio/mpeg": AudioFormat.mp3,
    "audio/mp3": AudioFormat.mp3,
    "audio/ogg": AudioFormat.opus,
    "audio/opus": AudioFormat.opus,
    "audio/aac": AudioFormat.aac,
    "audio/x-aac": AudioFormat.aac,
    "audio/flac": AudioFormat.flac,
    "audio/x-flac": AudioFormat.flac,
    "audio/wav": AudioFormat.wav,
    "audio/wave": AudioFormat.wav,
    "audio/x-wav": AudioFormat.wav,
    "audio/pcm": AudioFormat.pcm,
    "audio/l16": AudioFormat.pcm,
}


def detect_audio_format(content_type: str | None, data: bytes) -> AudioFormat | None:
    """Identify a supported input, preferring a recognizable byte signature."""
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return AudioFormat.wav
    if data.startswith(b"fLaC"):
        return AudioFormat.flac
    if data.startswith(b"OggS") and b"OpusHead" in data[:128]:
        return AudioFormat.opus
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0 and data[1] & 0x16 != 0x10
    ):
        return AudioFormat.mp3
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0:
        return AudioFormat.aac

    if content_type is None:
        return None
    media_type = content_type.partition(";")[0].strip().lower()
    declared_format = _CONTENT_TYPE_FORMATS.get(media_type)
    # Raw PCM has no container signature. Container formats must be recognizable
    # before they are eligible for pass-through.
    return declared_format if declared_format is AudioFormat.pcm else None


def audio_matches_format(data: bytes, expected: AudioFormat) -> bool:
    if expected is AudioFormat.pcm:
        return bool(data) and len(data) % 2 == 0
    return detect_audio_format(None, data) is expected


def _format_atempo_value(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def atempo_filter(speed: float) -> str | None:
    """Build an ffmpeg atempo chain with every factor inside ffmpeg's 0.5-2 range."""
    if math.isclose(speed, 1.0):
        return None

    factors: list[float] = []
    remaining = speed
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    if not math.isclose(remaining, 1.0):
        factors.append(remaining)
    return ",".join(f"atempo={_format_atempo_value(factor)}" for factor in factors)


def build_ffmpeg_args(
    *,
    ffmpeg_path: str,
    input_path: Path,
    output_path: Path,
    target_format: AudioFormat,
    speed: float,
    max_output_bytes: int,
) -> list[str]:
    args = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-vn",
    ]
    speed_filter = atempo_filter(speed)
    if speed_filter is not None:
        args.extend(["-filter:a", speed_filter])

    args.extend(["-fs", str(max_output_bytes + 1)])
    if target_format is AudioFormat.mp3:
        args.extend(["-codec:a", "libmp3lame", "-f", "mp3"])
    elif target_format is AudioFormat.opus:
        args.extend(["-codec:a", "libopus", "-f", "opus"])
    elif target_format is AudioFormat.aac:
        args.extend(["-codec:a", "aac", "-f", "adts"])
    elif target_format is AudioFormat.flac:
        args.extend(["-codec:a", "flac", "-f", "flac"])
    elif target_format is AudioFormat.wav:
        args.extend(["-codec:a", "pcm_s16le", "-f", "wav"])
    elif target_format is AudioFormat.pcm:
        args.extend(
            [
                "-ar",
                str(PCM_SAMPLE_RATE),
                "-ac",
                str(PCM_CHANNELS),
                "-codec:a",
                "pcm_s16le",
                "-f",
                "s16le",
            ]
        )
    args.append(str(output_path))
    return args


class AudioConverter:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_output_bytes: int,
        ffmpeg_path: str = "ffmpeg",
        temp_root: Path | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._ffmpeg_path = ffmpeg_path
        self._temp_root = temp_root

    async def convert(
        self,
        audio: bytes,
        source_format: AudioFormat | None,
        target_format: AudioFormat,
        speed: float,
    ) -> bytes:
        if source_format is target_format and math.isclose(speed, 1.0):
            return audio

        source_extension = FILE_EXTENSIONS[source_format] if source_format is not None else "bin"
        with tempfile.TemporaryDirectory(
            prefix="voicebox-adapter-",
            dir=self._temp_root,
        ) as temporary_directory:
            private_directory = Path(temporary_directory)
            input_path = private_directory / f"input.{source_extension}"
            output_path = private_directory / f"output.{FILE_EXTENSIONS[target_format]}"
            await asyncio.to_thread(input_path.write_bytes, audio)
            args = build_ffmpeg_args(
                ffmpeg_path=self._ffmpeg_path,
                input_path=input_path,
                output_path=output_path,
                target_format=target_format,
                speed=speed,
                max_output_bytes=self._max_output_bytes,
            )

            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                raise AdapterError(
                    502,
                    "Audio conversion could not be started",
                    "upstream_error",
                    "ffmpeg_unavailable",
                ) from exc

            try:
                await asyncio.wait_for(process.communicate(), timeout=self._timeout_seconds)
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise AdapterError(
                    502,
                    "Audio conversion timed out",
                    "upstream_error",
                    "ffmpeg_timeout",
                ) from exc
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise

            if process.returncode != 0 or not output_path.is_file():
                raise AdapterError(
                    502,
                    "Audio conversion failed",
                    "upstream_error",
                    "ffmpeg_failed",
                )

            output_size = output_path.stat().st_size
            if output_size <= 0:
                raise AdapterError(
                    502,
                    "Audio conversion produced no audio",
                    "upstream_error",
                    "ffmpeg_failed",
                )
            if output_size > self._max_output_bytes:
                raise AdapterError(
                    502,
                    "Converted audio exceeded the size limit",
                    "upstream_error",
                    "ffmpeg_output_too_large",
                )
            return await asyncio.to_thread(output_path.read_bytes)
