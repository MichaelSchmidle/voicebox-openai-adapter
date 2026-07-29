# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime

ARG VCS_REF=unknown
ARG VERSION=0.1.0

LABEL org.opencontainers.image.title="Voicebox OpenAI Adapter" \
      org.opencontainers.image.description="OpenAI-compatible text-to-speech adapter for Voicebox" \
      org.opencontainers.image.source="https://github.com/michaelschmidle/voicebox-openai-adapter" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 adapter \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin adapter

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"]

ENTRYPOINT ["uvicorn", "voicebox_openai_adapter.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
