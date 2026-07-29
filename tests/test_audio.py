from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import wav_bytes

from voicebox_openai_adapter.audio import (
    AudioConverter,
    AudioFormat,
    atempo_filter,
    audio_matches_format,
    build_ffmpeg_args,
    detect_audio_format,
)
from voicebox_openai_adapter.errors import AdapterError


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (0.25, "atempo=0.5,atempo=0.5"),
        (0.5, "atempo=0.5"),
        (1.0, None),
        (2.0, "atempo=2"),
        (4.0, "atempo=2,atempo=2"),
    ],
)
def test_atempo_filter_covers_full_speed_range(speed: float, expected: str | None) -> None:
    assert atempo_filter(speed) == expected


@pytest.mark.parametrize(
    ("content_type", "data", "expected"),
    [
        ("audio/mpeg", b"ID3\x04", AudioFormat.mp3),
        (None, b"\xff\xfb\x90\x64", AudioFormat.mp3),
        ("application/octet-stream", b"OggS\x00OpusHead", AudioFormat.opus),
        ("audio/aac", b"\xff\xf1\x50\x80", AudioFormat.aac),
        (None, b"fLaC\x00", AudioFormat.flac),
        ("audio/mpeg", wav_bytes(), AudioFormat.wav),
        ("audio/pcm; rate=24000", b"\x00\x00", AudioFormat.pcm),
        ("audio/ogg", b"OggS\x00Vorbis", None),
        ("application/octet-stream", b"unknown", None),
    ],
)
def test_audio_detection_prefers_safe_signatures(
    content_type: str | None,
    data: bytes,
    expected: AudioFormat | None,
) -> None:
    assert detect_audio_format(content_type, data) == expected


def test_ffmpeg_command_is_an_argument_array_with_bounded_output(tmp_path: Path) -> None:
    args = build_ffmpeg_args(
        ffmpeg_path="ffmpeg",
        input_path=tmp_path / "source.wav",
        output_path=tmp_path / "result.pcm",
        target_format=AudioFormat.pcm,
        speed=0.25,
        max_output_bytes=1024,
    )

    assert args[0] == "ffmpeg"
    assert "-filter:a" in args
    assert "atempo=0.5,atempo=0.5" in args
    assert args[args.index("-fs") + 1] == "1025"
    assert args[-5:] == ["-codec:a", "pcm_s16le", "-f", "s16le", str(tmp_path / "result.pcm")]
    assert "-ar" in args and args[args.index("-ar") + 1] == "24000"
    assert "-ac" in args and args[args.index("-ac") + 1] == "1"


@pytest.mark.parametrize(
    ("target", "codec", "muxer"),
    [
        (AudioFormat.mp3, "libmp3lame", "mp3"),
        (AudioFormat.opus, "libopus", "opus"),
        (AudioFormat.aac, "aac", "adts"),
        (AudioFormat.flac, "flac", "flac"),
        (AudioFormat.wav, "pcm_s16le", "wav"),
    ],
)
def test_ffmpeg_command_selects_honest_codec_and_muxer(
    target: AudioFormat,
    codec: str,
    muxer: str,
    tmp_path: Path,
) -> None:
    args = build_ffmpeg_args(
        ffmpeg_path="ffmpeg",
        input_path=tmp_path / "source.bin",
        output_path=tmp_path / f"result.{target}",
        target_format=target,
        speed=1.0,
        max_output_bytes=1024,
    )

    assert args[args.index("-codec:a") + 1] == codec
    assert args[args.index("-f") + 1] == muxer
    assert "-filter:a" not in args


@pytest.mark.parametrize(
    ("data", "expected", "matches"),
    [
        (b"ID3\x04", AudioFormat.mp3, True),
        (wav_bytes(), AudioFormat.mp3, False),
        (b"\x00\x00\x01\x00", AudioFormat.pcm, True),
        (b"\x00", AudioFormat.pcm, False),
        (b"", AudioFormat.pcm, False),
    ],
)
def test_audio_format_validation(
    data: bytes,
    expected: AudioFormat,
    matches: bool,
) -> None:
    assert audio_matches_format(data, expected) is matches


class FakeProcess:
    def __init__(self, *, returncode: int = 0, wait_forever: bool = False) -> None:
        self.returncode = returncode
        self.wait_forever = wait_forever
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.wait_forever:
            await asyncio.Future()
        return b"", b"conversion details that must stay private"

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def test_ffmpeg_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = FakeProcess(returncode=1)

    async def fake_subprocess(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    converter = AudioConverter(
        timeout_seconds=1,
        max_output_bytes=1024,
        temp_root=tmp_path,
    )

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(converter.convert(wav_bytes(), AudioFormat.wav, AudioFormat.mp3, 1.0))

    assert exc_info.value.code == "ffmpeg_failed"
    assert "conversion details" not in exc_info.value.message
    assert list(tmp_path.iterdir()) == []


def test_ffmpeg_timeout_kills_process_and_cleans_temp_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = FakeProcess(wait_forever=True)

    async def fake_subprocess(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    converter = AudioConverter(
        timeout_seconds=0.01,
        max_output_bytes=1024,
        temp_root=tmp_path,
    )

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(converter.convert(wav_bytes(), AudioFormat.wav, AudioFormat.mp3, 1.0))

    assert exc_info.value.code == "ffmpeg_timeout"
    assert process.killed
    assert list(tmp_path.iterdir()) == []


def test_matching_format_returns_without_starting_ffmpeg(tmp_path: Path) -> None:
    converter = AudioConverter(
        timeout_seconds=1,
        max_output_bytes=1024,
        temp_root=tmp_path,
    )

    result = asyncio.run(converter.convert(wav_bytes(), AudioFormat.wav, AudioFormat.wav, 1.0))

    assert result == wav_bytes()
    assert list(tmp_path.iterdir()) == []


def test_ffmpeg_start_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_subprocess(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    converter = AudioConverter(
        timeout_seconds=1,
        max_output_bytes=1024,
        temp_root=tmp_path,
    )

    with pytest.raises(AdapterError) as exc_info:
        asyncio.run(converter.convert(wav_bytes(), AudioFormat.wav, AudioFormat.mp3, 1.0))

    assert exc_info.value.code == "ffmpeg_unavailable"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("output", "expected_code"),
    [
        (b"ID3\x04\x00\x00converted", None),
        (b"", "ffmpeg_failed"),
        (b"x" * 1025, "ffmpeg_output_too_large"),
    ],
)
def test_ffmpeg_success_and_output_bounds(
    output: bytes,
    expected_code: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_subprocess(*args: str, **kwargs: Any) -> FakeProcess:
        del kwargs
        Path(args[-1]).write_bytes(output)
        return FakeProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    converter = AudioConverter(
        timeout_seconds=1,
        max_output_bytes=1024,
        temp_root=tmp_path,
    )

    if expected_code is None:
        result = asyncio.run(converter.convert(wav_bytes(), AudioFormat.wav, AudioFormat.mp3, 1.0))
        assert result == output
    else:
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(converter.convert(wav_bytes(), AudioFormat.wav, AudioFormat.mp3, 1.0))
        assert exc_info.value.code == expected_code
    assert list(tmp_path.iterdir()) == []
