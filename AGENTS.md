# AGENTS.md

## Mission

Build a small, production-grade OpenAI-compatible text-to-speech adapter for [Voicebox](https://github.com/jamiepine/voicebox). Open WebUI is the first client, but the API must remain generic.

Read `docs/IMPLEMENTATION_SPEC.md` completely before changing code. It is the authoritative product and security specification. `docs/CODING_AGENT_PROMPT.md` defines the initial implementation task.

## Engineering approach

- Use Python 3.12, FastAPI, Pydantic settings, HTTPX, pytest, Ruff, and mypy.
- Use a `src/` package layout and `uv` with a committed lockfile.
- Follow red/green/refactor: add a failing behavioral test, implement minimally, then refactor while green.
- Prefer explicit functions and typed models. This service does not need plugins, a service container, a generic proxy abstraction, or a database.
- Keep Voicebox-specific assumptions inside the upstream client and cover them with contract tests.
- Automated tests must use mock/fake upstream responses; never require a live Voicebox instance.
- Use `ffmpeg` through argument arrays only—never `shell=True` or interpolated shell commands.

## Non-negotiable security and privacy rules

- Fail closed when the client API key is absent or invalid.
- Compare secrets in constant time.
- The Voicebox base URL comes only from trusted configuration; callers can never select an upstream URL.
- Never log or persist input text, generated audio, profile names/IDs, authorization headers, API keys, upstream response bodies, or local paths.
- Bound input length, upstream response size, network timeouts, conversion time, and temporary storage.
- Run the container as a non-root numeric user and support a read-only root filesystem.
- Never claim an output format that does not match the returned bytes.
- Do not add analytics or telemetry.

## Public-repository hygiene

Do not commit:

- credentials or `.env` files;
- private hostnames, network addresses, personal paths, or deployment observations;
- generated speech/audio;
- model files, caches, build artifacts, IDE state, or agent transcripts.

Examples and tests must use reserved names such as `voicebox.example.test` and synthetic credentials.

## Expected project shape

```text
src/voicebox_openai_adapter/
tests/
deploy/
.github/workflows/
Dockerfile
pyproject.toml
uv.lock
```

Keep production dependencies narrow. Development-only tooling belongs in dependency groups.

## Required verification

Before opening or updating a pull request, run all applicable checks:

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

Report real output. Do not substitute plausible-looking results when a tool or dependency is unavailable.

## Git and pull requests

- Work on a feature branch; never push implementation directly to `main`.
- Keep commits reviewable and use conventional commit subjects.
- The PR must state design decisions, tests run, security/privacy implications, and any specification deviations.
- Stop and document a blocker if true compatibility cannot be implemented; do not silently weaken the contract.
