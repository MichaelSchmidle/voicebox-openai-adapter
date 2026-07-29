# Implementation specification

## 1. Objective

Build a small HTTP service that presents the OpenAI text-to-speech contract to clients and translates requests to Voicebox. The first production client is Open WebUI.

The adapter owns protocol translation, client authentication, output conversion and failure normalisation. Voicebox remains responsible for profiles, synthesis, generation history and model execution.

## 2. Verified Voicebox contract

The target Voicebox deployment exposes these relevant endpoints:

### `POST /speak`

JSON request:

- `text`: required string, 1–10,000 characters.
- `profile`: optional profile name or ID; Voicebox falls back to its client binding and then its default.
- `engine`: optional: `qwen`, `qwen_custom_voice`, `luxtts`, `chatterbox`, `chatterbox_turbo`, `tada`, or `kokoro`.
- `personality`: optional boolean.
- `language`: optional supported language code.

It is asynchronous: a successful response is a `GenerationResponse` containing at least `id`, `profile_id`, `text`, `language`, `status`, and `created_at`. The initial status is normally `generating`.

### `GET /generate/{generation_id}/status`

Returns an SSE stream of JSON status events. Supported active statuses are `loading_model` and `generating`; `completed` is successful and `failed` is terminal. The adapter also treats `error`, `cancelled`, `canceled`, and `not_found` as terminal failures for forward compatibility.

The adapter must validate the `text/event-stream` response, parse events incrementally, bound individual events and the complete stream, and reject malformed or unknown status payloads. A stream that disconnects or ends before a terminal event is an upstream failure.

### `GET /audio/{generation_id}`

Returns the generated audio body after the status stream reports `completed`.

### `GET /profiles`

Returns voice profiles containing at least `id`, `name`, `language`, and metadata.

### `GET /health`

Returns Voicebox health and model/GPU state.

Do not poll repeatedly: consume the documented status SSE stream once. Do not depend on private deployment URLs or undocumented filesystem paths.

## 3. Public API

### `POST /v1/audio/speech`

Accept an OpenAI-compatible JSON body:

```json
{
  "model": "tts-1",
  "input": "Hello",
  "voice": "profile-name-or-id",
  "response_format": "mp3",
  "speed": 1.0
}
```

Validation:

- `input`: required, non-blank, maximum 4,096 characters.
- `model`: required string.
- `voice`: required string.
- `response_format`: `mp3`, `opus`, `aac`, `flac`, `wav`, or `pcm`; default `mp3`.
- `speed`: 0.25–4.0; default `1.0`.

Optional Voicebox extensions may be accepted without breaking OpenAI clients:

- `language`
- `engine`
- `personality`

Unknown fields should be rejected rather than silently ignored.

Successful response:

- raw audio bytes, never JSON or base64;
- accurate `Content-Type`;
- `Content-Disposition: inline` with a safe filename;
- no synthetic streaming claim: it may return after complete generation.

### `GET /v1/models`

Return a minimal OpenAI-style model list containing `voicebox`, `tts-1`, and `tts-1-hd`. All three map to the configured default engine unless an explicit allowed engine is supplied.

### `GET /v1/audio/voices`

Non-standard convenience endpoint. Transform Voicebox `/profiles` into a stable list containing profile `id`, `name`, and `language`. Never expose local paths or unnecessary profile metadata.

### `GET /healthz`

Unauthenticated liveness check. It proves only that the adapter process is alive; it must not contact Voicebox.

### `GET /readyz`

Unauthenticated readiness check. Probe Voicebox `/health` with a short timeout. Return `200` when Voicebox reports a healthy service, even if its model is lazily unloaded; return `503` when the service is unavailable or unhealthy. Do not expose upstream internals.

## 4. Authentication and trust boundary

- Require an `Authorization: Bearer <token>` header matching `ADAPTER_API_KEY` for `/v1/*` routes.
- Compare credentials in constant time.
- Refuse to start if `ADAPTER_API_KEY` is missing or blank.
- Health endpoints remain unauthenticated for container orchestration.
- `VOICEBOX_BASE_URL` comes only from configuration. Never accept an upstream URL from callers.
- Support an optional `VOICEBOX_API_KEY` and send it as a Bearer token when configured.
- Never forward the client-facing adapter key upstream.

## 5. Request translation

1. Authenticate the client.
2. Validate the OpenAI request.
3. Resolve the engine:
   - `voicebox`, `tts-1`, and `tts-1-hd` map to `VOICEBOX_DEFAULT_ENGINE`.
   - An explicit `engine` extension is accepted only if present in `VOICEBOX_ALLOWED_ENGINES`.
   - Never treat arbitrary model values as upstream engine names.
4. Resolve the voice:
   - a normal value is passed to `/speak` as `profile`; Voicebox accepts profile name or ID;
   - OpenAI stock names (`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`) map to `VOICEBOX_DEFAULT_PROFILE`;
   - if a stock name is used and no default profile is configured, omit `profile` and let Voicebox apply its documented fallback;
   - do not fetch all profiles on every speech request merely to reimplement Voicebox resolution.
5. Call `POST {VOICEBOX_BASE_URL}/speak` with `text`, resolved `profile`, resolved `engine`, and optional extensions.
6. Require a successful response with a non-empty generation `id` and a recognised status.
7. If the initial status is not `completed`, consume `GET {VOICEBOX_BASE_URL}/generate/{id}/status` until a `completed` event. Bound the SSE event and stream sizes, reject malformed or unknown statuses, and fail on terminal failure statuses or premature disconnect.
8. Apply `VOICEBOX_REQUEST_TIMEOUT_SECONDS` as one wall-clock deadline spanning `/speak`, status completion, and audio download. Individual operations must not reset the full budget.
9. Fetch `GET {VOICEBOX_BASE_URL}/audio/{id}` only after completion.
10. Enforce `MAX_AUDIO_BYTES` while reading the upstream response.
11. Return or transcode the audio.

Send a stable `X-Voicebox-Client-Id` header using `VOICEBOX_CLIENT_ID`.

## 6. Audio conversion

Voicebox may return a format different from the requested OpenAI `response_format`.

- Detect the upstream content type and/or safe container signature.
- If source format and requested format match and speed is `1.0`, pass through without transcoding.
- Otherwise use an `ffmpeg` subprocess with argument arrays, never a shell command.
- Apply `speed` with a valid `atempo` filter chain for the full 0.25–4.0 range.
- Use private temporary files/directories and clean them in success, error, and cancellation paths.
- Bound subprocess runtime and output size.
- PCM means signed 16-bit little-endian raw PCM; document the chosen sample rate in tests and response headers where practical.
- If a format cannot be produced, return a clear `400` or `502`; never label WAV bytes as MP3.

The container may install distribution `ffmpeg`; keep the final image otherwise minimal.

## 7. Errors

Return OpenAI-shaped errors:

```json
{
  "error": {
    "message": "Upstream synthesis timed out",
    "type": "upstream_error",
    "code": "voicebox_timeout"
  }
}
```

Required mapping:

- invalid client input → `400` or FastAPI/Pydantic `422`, consistently tested;
- missing/invalid adapter key → `401` with `WWW-Authenticate: Bearer`;
- unknown/disallowed engine → `400`;
- Voicebox profile not found or rejected → preserve useful `4xx` meaning without returning request text;
- upstream unavailable/protocol-invalid → `502`;
- upstream timeout or saturation → `504` or `503`, consistently documented;
- oversized upstream audio → `502`;
- ffmpeg failure/timeout → `502`.

Do not include full upstream response bodies in public errors. Never include input text or credentials.

## 8. Configuration

Use environment variables with typed settings:

| Variable | Required | Default | Purpose |
|---|---:|---|---|
| `ADAPTER_API_KEY` | yes | — | Client Bearer key |
| `VOICEBOX_BASE_URL` | no | `http://voicebox:17493` | Fixed upstream base URL |
| `VOICEBOX_API_KEY` | no | — | Optional upstream Bearer key |
| `VOICEBOX_CLIENT_ID` | no | `openai-adapter` | Stable Voicebox client identity |
| `VOICEBOX_DEFAULT_PROFILE` | no | — | Mapping for stock OpenAI voice names |
| `VOICEBOX_DEFAULT_ENGINE` | no | `qwen` | Engine for model aliases |
| `VOICEBOX_ALLOWED_ENGINES` | no | documented safe set | Comma-separated allowlist |
| `VOICEBOX_REQUEST_TIMEOUT_SECONDS` | no | `600` | Overall `/speak`, status-stream, and audio-download deadline |
| `VOICEBOX_HEALTH_TIMEOUT_SECONDS` | no | `5` | Readiness timeout |
| `MAX_INPUT_CHARS` | no | `4096` | Hard input limit, never above Voicebox's limit |
| `MAX_AUDIO_BYTES` | no | `104857600` | Upstream audio cap |
| `FFMPEG_TIMEOUT_SECONDS` | no | `120` | Conversion timeout |
| `LOG_LEVEL` | no | `INFO` | Metadata-only logging level |

Reject malformed URLs, non-positive limits, empty keys, and invalid engine configuration at startup.

## 9. Observability and privacy

Structured logs may contain:

- request ID;
- route and status;
- timing;
- requested output format;
- engine alias;
- upstream status category;
- audio byte count.

Logs must never contain:

- input text or snippets;
- generated audio;
- profile names/IDs unless irreversibly redacted;
- API keys or Authorization headers;
- upstream bodies;
- local paths.

Do not add analytics or telemetry.

## 10. Packaging

- Python 3.12, `src/` package layout.
- Multi-stage Dockerfile based on an official slim Python image.
- Install `ffmpeg` and CA certificates in the runtime image.
- Run as a numeric non-root user.
- Read-only root filesystem compatible; use `/tmp` for bounded temporary work.
- OCI labels for source, revision, version, and license.
- Include a Docker `HEALTHCHECK` against `/healthz`.
- Build for `linux/amd64` and `linux/arm64`.

## 11. CI/CD

PR and main CI:

- dependency sync/install;
- Ruff formatting and lint;
- mypy;
- pytest with coverage;
- Docker image build;
- vulnerability scan;
- `docker compose config` for the example deployment.

Release workflow on `v*` tags:

- Rerun the quality suite, container checks, and vulnerability scan before publishing.
- Make the publish job depend on those successful checks.
- Buildx multi-platform build for `linux/amd64,linux/arm64`.
- Push `ghcr.io/michaelschmidle/voicebox-openai-adapter`.
- Publish semver tags plus the immutable manifest digest.
- Generate SBOM and provenance.
- Never publish `latest` as the deployment recommendation. An `edge` tag from `main` is optional and must be clearly non-production.

## 12. Required tests

At minimum:

- valid OpenAI request → correct `/speak` request → audio response;
- profile name and ID passthrough;
- stock OpenAI voice alias with and without configured default profile;
- model/engine allowlist mapping;
- all response formats and `speed` boundaries;
- pass-through path avoids ffmpeg;
- malformed/oversized input;
- missing/invalid client key;
- optional upstream key is forwarded while client key is not;
- upstream timeout, connection failure, `4xx`, `5xx`, malformed JSON, missing generation ID, failed status;
- asynchronous `/speak` completion via bounded SSE, including completed, failed, malformed, oversized, disconnected, prematurely ended, and timed-out streams;
- one overall synthesis deadline spanning `/speak`, status completion, and audio download;
- audio download failure and size overflow;
- ffmpeg error and timeout;
- logs do not contain a unique canary input or secret;
- liveness does not call upstream;
- readiness does call upstream and sanitises failures;
- container runs as non-root;
- Open WebUI-shaped request contract.

Unit and contract tests must use fake/mock Voicebox responses. A manual smoke script may target a user-supplied base URL and key, but it must never contain deployment-specific defaults.

## 13. Deliberate non-goals for v0.1

- speech-to-text;
- realtime or chunked audio streaming;
- caching generated audio;
- profile creation/editing;
- a browser UI;
- arbitrary Voicebox API proxying;
- persisting requests or audio;
- claiming cancellation of a `/speak` request after the upstream synthesis has begun;
- replacing Voicebox history or model management.

## 14. Definition of done

- The full test/lint/type suite passes.
- The container builds and runs locally as non-root.
- A mocked contract test proves Open WebUI's request shape receives playable audio bytes.
- Manual validation against a real Voicebox instance succeeds for at least WAV and MP3.
- `docker buildx imagetools inspect` confirms AMD64 and ARM64 release manifests.
- GHCR image, SBOM, provenance, and release notes exist.
- No secret, private hostname, personal path, prompt text, or generated audio is committed.
