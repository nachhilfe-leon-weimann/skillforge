set shell := ["bash", "-cu"]

# --- Quality ---

lint:
    uv run ruff check

format-check:
    uv run ruff format --check

typecheck:
    uv run ty check

static-checks: lint format-check typecheck openapi-check

check: static-checks test-without-db

check-all: static-checks test

# --- Contract ---

# Regenerate openapi.json (the source of truth API consumers generate clients from).
openapi:
    uv run python scripts/dump_openapi.py

# Fail if the committed openapi.json is stale relative to the current API.
openapi-check: openapi
    #!/usr/bin/env bash
    if git diff --exit-code -- openapi.json; then
        printf '\033[1;32mopenapi.json is up to date!\033[0m\n'
    else
        printf '\033[1;31mopenapi.json is stale: run `just openapi` and commit.\033[0m\n' >&2
        exit 1
    fi

# --- Clients ---

python_client_dir := "build/clients/python/skillforge-client"
python_client_templates := "clients/python/templates"

# Generate the clients from the openapi.json contract.
generate-clients: generate-python-client

# Generate only the Python client.
generate-python-client:
    @mkdir -p "{{ python_client_dir }}"
    uvx \
        --from "openapi-python-client==0.29.0" \
        --with "ruff==0.16.2" \
        openapi-python-client generate \
        --path openapi.json \
        --custom-template-path "{{ python_client_templates }}" \
        --meta uv \
        --fail-on-warning \
        --overwrite \
        --output-path "{{ python_client_dir }}"

# Build the clients from the generated source code.
build-clients: build-python-client

# Build only the Python client.
build-python-client: generate-python-client
    uv build "{{ python_client_dir }}" --no-sources --clear --out-dir dist/python

# Build and smoke-test the generated clients as installable distributions.
test-clients: test-python-client

# Verify both Python distribution formats can be installed and imported.
test-python-client: build-python-client
    uv run --isolated --no-project --with dist/python/*.whl python scripts/smoke_test_python_client.py
    uv run --isolated --no-project --with dist/python/*.tar.gz python scripts/smoke_test_python_client.py

# --- FastAPI ---

skillcore := "../skillcore"

dev:
    @echo "Starting the SkillForge API server..."
    @uv run fastapi dev --reload-dir app

dev-local-core:
    @echo "Starting the SkillForge API server with editable SkillCore..."
    @uv run --with-editable {{ skillcore }} fastapi dev --reload-dir app --reload-dir {{ skillcore }}/src

# --- Testing ---

test:
    uv run pytest

test-coverage:
    uv run pytest --cov=app --cov-report=term-missing:skip-covered --cov-report=xml:coverage.xml

test-db:
    uv run pytest -m db

test-without-db:
    uv run pytest -m "not db"

test-v:
    uv run pytest -v

test-file file:
    uv run pytest {{ file }}

test-one test:
    uv run pytest -k {{ test }}

# --- Workers ---

# Run the housekeeping worker loop locally (deletes auth sessions and one-time tokens 30 days past expiry; beats for /health).
worker-housekeeping:
    uv run python -m app.workers.housekeeping

# --- Auth ---

bootstrap-skillbot:
    uv run python -m app.cli.bootstrap skillbot

# Seed a client with grants in both modes: <client_id> --application "<scopes>" --delegated "<scopes>"
[positional-arguments]
bootstrap-client *args:
    uv run python -m app.cli.bootstrap client "$@"

# Ensure an enabled admin account and print its invitation or reset token: --party-id <uuid> --email <address>
[positional-arguments]
bootstrap-admin *args:
    uv run python -m app.cli.bootstrap admin "$@"

# --- Docker ---

docker-build image="skillforge:local":
    @test -n "${SKILLPLATFORM_READ_TOKEN:-}" || (echo 'Set SKILLPLATFORM_READ_TOKEN, for example: export SKILLPLATFORM_READ_TOKEN=$(gh auth token)' >&2; exit 1)
    docker build --secret id=github_token,env=SKILLPLATFORM_READ_TOKEN -f dockerfile -t ghcr.io/skillforge:sha-$(git rev-parse --short=12 HEAD) .
    docker tag ghcr.io/skillforge:sha-$(git rev-parse --short=12 HEAD) {{ image }}

docker-run image="skillforge:local":
    docker run --rm --env-file .env -p 8000:8000 {{ image }}

# --- Local Postgres ---

postgres := "../infra/postgres/justfile"

pg-up:
    just --justfile {{ postgres }} up

pg-down:
    just --justfile {{ postgres }} down

pg-restart:
    just --justfile {{ postgres }} restart

pg-logs:
    just --justfile {{ postgres }} logs

pg-psql:
    just --justfile {{ postgres }} psql

pg-bash:
    just --justfile {{ postgres }} bash

pg-reset:
    just --justfile {{ postgres }} reset

pg-ps:
    just --justfile {{ postgres }} ps

pg-create-db name:
    just --justfile {{ postgres }} create-db {{ name }}

pg-drop-db name:
    just --justfile {{ postgres }} drop-db {{ name }}
