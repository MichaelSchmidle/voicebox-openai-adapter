# Voicebox OpenAI Adapter

A lightweight OpenAI-compatible text-to-speech API adapter for [Voicebox](https://github.com/jamiepine/voicebox).

> **Status:** implementation specification prepared; application code has not been written yet.

## Purpose

Voicebox provides local text-to-speech, voice cloning, profiles, and generation history, but its current API is not directly compatible with clients expecting OpenAI's `POST /v1/audio/speech` contract.

This service is a narrow compatibility boundary:

```text
OpenAI-compatible client
        │  POST /v1/audio/speech
        ▼
Voicebox OpenAI Adapter
        │  POST /speak → GET /audio/{generation_id}
        ▼
Voicebox
```

The first production client is Open WebUI. The adapter remains generic so other OpenAI-compatible clients can use the same endpoint.

## Planned capabilities

- OpenAI-compatible speech requests and raw audio responses.
- Voicebox profile names and IDs through the OpenAI `voice` field.
- OpenAI stock voice aliases mapped to a configurable Voicebox default profile.
- Honest MP3, Opus, AAC, FLAC, WAV, and PCM conversion.
- Speech-speed handling from `0.25` to `4.0`.
- Bearer-token authentication.
- Minimal model and voice-list endpoints.
- Liveness and Voicebox-aware readiness checks.
- Privacy-safe metadata logging with no speech text or audio retention.
- Multi-architecture container images for AMD64 and ARM64.

## Deliberate boundaries

This adapter will not provide a UI, speech-to-text, profile editing, arbitrary Voicebox proxying, audio caching, model management, or realtime streaming. Voicebox remains the system of record for profiles and generations.

## Proposed Open WebUI configuration

```text
AUDIO_TTS_ENGINE=openai
AUDIO_TTS_OPENAI_API_BASE_URL=https://voicebox.example.test/v1
AUDIO_TTS_OPENAI_API_KEY=<adapter-api-key>
AUDIO_TTS_MODEL=tts-1
AUDIO_TTS_VOICE=<voicebox-profile-name-or-id>
```

Open WebUI appends `/audio/speech` to the configured base URL.

## Development brief

- Agent instructions: [`AGENTS.md`](AGENTS.md)
- Authoritative implementation specification: [`docs/IMPLEMENTATION_SPEC.md`](docs/IMPLEMENTATION_SPEC.md)
- Copy-paste coding-agent prompt: [`docs/CODING_AGENT_PROMPT.md`](docs/CODING_AGENT_PROMPT.md)

Implementation should happen on a feature branch and arrive through a pull request.

## Deployment direction

Release images will be published to:

```text
ghcr.io/michaelschmidle/voicebox-openai-adapter
```

Production deployments should pin a semantic version and immutable manifest digest. Stacksmith will consume the image; it will not build application source during deployment.

## License

[MIT](LICENSE)
