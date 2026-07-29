# Coding-agent prompt

You are implementing the first production release of `voicebox-openai-adapter`.

Start by reading, in order:

1. `AGENTS.md`
2. `docs/IMPLEMENTATION_SPEC.md`
3. `README.md`

Work on a new feature branch. Do not push directly to `main`.

## Goal

Implement the complete v0.1 service described in the specification: an authenticated OpenAI-compatible TTS API that translates requests to Voicebox, retrieves the generated audio, performs honest format/speed conversion, and ships as a tested multi-architecture container with CI/CD.

## Execution discipline

- Follow red/green/refactor: write a failing test for each behavior before implementation.
- Keep the service deliberately small. Prefer explicit functions and typed models over framework layers or generic plugin abstractions.
- Treat privacy and authentication requirements as release blockers.
- Never log speech input, profile identifiers, authorization headers, upstream bodies, audio, or local paths.
- Do not invent Voicebox behavior. The verified contract is in the specification; isolate any additional assumptions behind tests.
- Do not contact a real Voicebox service from automated tests.
- Do not weaken validation or silently ignore unsupported fields to make tests pass.
- Do not commit credentials, deployment hostnames, generated audio, local paths, editor state, caches, or agent transcripts.

## Expected deliverables

- Python package under `src/`.
- FastAPI application and typed configuration.
- Voicebox HTTP client with bounded timeouts and response limits.
- OpenAI-compatible speech, models, voices, liveness and readiness routes.
- Safe `ffmpeg` conversion and speed handling.
- OpenAI-shaped errors.
- Comprehensive unit/contract tests, including privacy canaries.
- `pyproject.toml` and lockfile suitable for `uv`.
- Multi-stage non-root Dockerfile with ffmpeg.
- Example Docker Compose and `.env.example` with generic values only.
- GitHub CI, release, dependency-update and security-scanning configuration.
- Updated README with exact local development, API, Open WebUI and release usage.

## Verification before opening the PR

Run every applicable command and include the real results in the PR description:

```bash
uv sync --all-groups --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --cov --cov-report=term-missing

docker build -t voicebox-openai-adapter:test .
docker run --rm --entrypoint id voicebox-openai-adapter:test
docker compose -f deploy/docker-compose.example.yml config
```

Also inspect the final diff for secrets and private context. The PR description must include:

- design summary;
- explicit spec deviations, if any;
- test/build outputs;
- security/privacy considerations;
- manual steps still needed for real Voicebox and GHCR validation.

If a requirement is ambiguous, choose the smallest secure behavior and document the decision in the PR. If a requirement is infeasible, stop and explain the blocker rather than faking compatibility.
