# Voicebox OpenAI Adapter

A small, production-oriented HTTP adapter that presents an
[OpenAI-compatible text-to-speech API](https://platform.openai.com/docs/api-reference/audio/createSpeech)
and translates requests to [Voicebox](https://github.com/jamiepine/voicebox).
Open WebUI is the first supported client, but the API is client-neutral.

The adapter authenticates clients, maps OpenAI model and voice conventions to Voicebox,
downloads the completed generation, and uses `ffmpeg` when the requested audio format or speed
requires conversion. It does not cache, log, or persist speech text or audio.

## API

All `/v1/*` routes require `Authorization: Bearer <ADAPTER_API_KEY>`.

| Route | Purpose |
|---|---|
| `POST /v1/audio/speech` | Generate raw MP3, Opus, AAC, FLAC, WAV, or PCM audio |
| `GET /v1/models` | List `voicebox`, `tts-1`, and `tts-1-hd` aliases |
| `GET /v1/audio/voices` | Return sanitized Voicebox profile IDs, names, and languages |
| `GET /healthz` | Unauthenticated process liveness; never calls Voicebox |
| `GET /readyz` | Unauthenticated, sanitized Voicebox readiness |

Example:

```bash
curl --fail-with-body \
  --request POST http://127.0.0.1:8000/v1/audio/speech \
  --header "Authorization: Bearer ${ADAPTER_API_KEY}" \
  --header "Content-Type: application/json" \
  --data '{
    "model": "tts-1",
    "input": "Hello from the adapter.",
    "voice": "profile-name-or-id",
    "response_format": "mp3",
    "speed": 1.0
  }' \
  --output speech.mp3
```

The optional extensions `engine`, `language`, and `personality` map directly to their documented
Voicebox fields. Unknown JSON fields are rejected. An explicit engine must be in
`VOICEBOX_ALLOWED_ENGINES`; arbitrary model strings are never treated as upstream engine names.

OpenAI stock voices (`alloy`, `echo`, `fable`, `onyx`, `nova`, and `shimmer`) map to
`VOICEBOX_DEFAULT_PROFILE`. If that setting is absent, the adapter omits the profile and lets
Voicebox use its own documented fallback.

PCM responses are mono signed 16-bit little-endian samples at 24 kHz and use
`Content-Type: audio/pcm; rate=24000; channels=1`.

Errors use the OpenAI envelope:

```json
{
  "error": {
    "message": "Upstream synthesis timed out",
    "type": "upstream_error",
    "code": "voicebox_timeout"
  }
}
```

Request validation is a sanitized `422`, unsupported models and engines are `400`, upstream
saturation is `503`, upstream timeouts are `504`, and other upstream/protocol/conversion failures
are `502`. Upstream response bodies are never copied into errors.

## Configuration

Copy [`.env.example`](.env.example) to an untracked `.env` for local use. The process refuses to
start when `ADAPTER_API_KEY` is missing or blank.

| Variable | Required | Default |
|---|---:|---|
| `ADAPTER_API_KEY` | yes | none |
| `VOICEBOX_BASE_URL` | no | `http://voicebox:17493` |
| `VOICEBOX_API_KEY` | no | none |
| `VOICEBOX_CLIENT_ID` | no | `openai-adapter` |
| `VOICEBOX_DEFAULT_PROFILE` | no | none |
| `VOICEBOX_DEFAULT_ENGINE` | no | `qwen` |
| `VOICEBOX_ALLOWED_ENGINES` | no | all documented Voicebox engines |
| `VOICEBOX_REQUEST_TIMEOUT_SECONDS` | no | `600` |
| `VOICEBOX_HEALTH_TIMEOUT_SECONDS` | no | `5` |
| `MAX_INPUT_CHARS` | no | `4096` |
| `MAX_AUDIO_BYTES` | no | `104857600` |
| `FFMPEG_TIMEOUT_SECONDS` | no | `120` |
| `LOG_LEVEL` | no | `INFO` |

The base URL is trusted administrator configuration only. It is never accepted from an API
request. Configure `VOICEBOX_API_KEY` separately when Voicebox requires authentication; the
client-facing adapter key is never forwarded upstream.

## Local development

Requirements are Python 3.12, [`uv`](https://docs.astral.sh/uv/), `ffmpeg`, and optionally Docker.

```bash
uv sync --all-groups --locked
cp .env.example .env
# Replace ADAPTER_API_KEY and set the trusted Voicebox URL in .env.
uv run uvicorn voicebox_openai_adapter.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Run the full local quality suite:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --cov --cov-report=term-missing
```

Tests use HTTPX mock transports and never contact a live Voicebox instance.

## Open WebUI

Configure Open WebUI with the adapter's externally reachable URL:

```text
AUDIO_TTS_ENGINE=openai
AUDIO_TTS_OPENAI_API_BASE_URL=https://voicebox.example.test/v1
AUDIO_TTS_OPENAI_API_KEY=<adapter-api-key>
AUDIO_TTS_MODEL=tts-1
AUDIO_TTS_VOICE=<voicebox-profile-name-or-id>
```

Open WebUI appends `/audio/speech` to the base URL. The mocked contract suite covers this request
shape.

## Container deployment

Build and verify the non-root image:

```bash
docker build -t voicebox-openai-adapter:test .
docker run --rm --entrypoint id voicebox-openai-adapter:test
docker compose -f deploy/docker-compose.example.yml config
```

The runtime image uses numeric UID/GID `10001`, includes only the application environment,
CA certificates, and distribution `ffmpeg`, and is compatible with a read-only root filesystem.
Temporary conversion files live under a bounded `/tmp` tmpfs in the Compose example and are
removed on success, failure, timeout, and cancellation.

Release tags build `linux/amd64` and `linux/arm64` images at:

```text
ghcr.io/michaelschmidle/voicebox-openai-adapter
```

Production deployments should use a semantic version and pin the immutable manifest digest
reported by the release workflow. `latest` is intentionally not published or recommended.

## Security and privacy

- Client secrets are compared in constant time, and all `/v1/*` routes fail closed.
- Input size, upstream reads, network operations, conversion time, and output size are bounded.
- `ffmpeg` is invoked only through an argument array, with no shell.
- Structured request logs contain request IDs, route templates, status, and timing only.
- Speech text, audio, profiles, credentials, upstream bodies, and temporary paths are never logged.
- The service has no analytics, telemetry, database, cache, or arbitrary proxy route.

See [`docs/IMPLEMENTATION_SPEC.md`](docs/IMPLEMENTATION_SPEC.md) for the complete contract and
deliberate non-goals.

## Release validation

Automated CI covers formatting, linting, typing, unit/contract tests, coverage, image build,
non-root execution, Compose validation, vulnerability scanning, CodeQL, multi-architecture
publishing, SBOM, and provenance.

Before declaring a release production-ready, a maintainer must still:

1. Validate WAV and MP3 synthesis against a real, user-supplied Voicebox instance.
2. Confirm the published manifest with `docker buildx imagetools inspect`.
3. Verify the GHCR digest, SBOM, provenance attestation, and generated release notes.

## License

[MIT](LICENSE)
