# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV GIT_TERMINAL_PROMPT=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=github_token,required=true \
    set -eu; \
    github_token="$(cat /run/secrets/github_token)"; \
    git config --global url."https://x-access-token:${github_token}@github.com/".insteadOf "https://github.com/"; \
    uv sync --locked --no-dev --no-install-project; \
    git config --global --unset-all url."https://x-access-token:${github_token}@github.com/".insteadOf

COPY app ./app

FROM python:3.15-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin skillforge

COPY --from=builder --chown=skillforge:skillforge /app/.venv ./.venv
COPY --from=builder --chown=skillforge:skillforge /app/app ./app

# pyproject.toml stays in the runtime image: the app resolves its version from it at
# startup (skillcore.get_project_version).
COPY --chown=skillforge:skillforge pyproject.toml ./pyproject.toml

# Migrations + Alembic config so the migrate service can run `alembic upgrade head`.
COPY --chown=skillforge:skillforge alembic.ini ./alembic.ini
COPY --chown=skillforge:skillforge migrations ./migrations

RUN mkdir -p /app/logs && chown -R skillforge:skillforge /app

USER skillforge

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
