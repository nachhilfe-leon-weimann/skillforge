set shell := ["bash", "-cu"]

# --- Quality ---

lint:
    uv run ruff check

format-check:
    uv run ruff format --check

typecheck:
    uv run ty check

static-checks: lint format-check typecheck openapi-check

check: static-checks test-guard test-without-db

check-all: static-checks test-guard test

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
    uv run pytest -m "not contract"

test-coverage:
    uv run pytest -m "not contract" --cov=app --cov-report=term-missing:skip-covered --cov-report=xml:coverage.xml

test-db:
    uv run pytest -m db

test-without-db:
    uv run pytest -m "not db and not contract"

test-v:
    uv run pytest -v

# The agent guard hook (.claude/hooks/guard.mjs) needs only node.
test-guard:
    node --test .claude/hooks/

test-file file:
    uv run pytest {{ file }}

test-one test:
    uv run pytest -k {{ test }}

# --- Workers ---

# Run the housekeeping worker loop locally (deletes auth sessions and one-time tokens 30 days past expiry; beats for /health).
worker-housekeeping:
    uv run python -m app.workers.housekeeping

# --- Auth ---

# Seed a client with grants in both modes: <client_id> --application "<scopes>" --delegated "<scopes>"
[positional-arguments]
bootstrap-client *args:
    uv run python -m app.cli.bootstrap client "$@"

# Ensure an enabled admin account and print its invitation or reset token: --party-id <uuid> --email <address>
[positional-arguments]
bootstrap-admin *args:
    uv run python -m app.cli.bootstrap admin "$@"

# --- sevDesk ---
# These call sevDesk: only the maintainer runs them, never CI. The trial's settings come from its env file, outside
# the repository (SKILLFORGE_SEVDESK_TRIAL_ENV overrides the path), and never mix with SEVDESK__ variables of the
# shell, which would take precedence over the file.

# Read-only contract tests against the synthetic sevDesk trial account; without a trial env file, nothing runs.
contract-sevdesk:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "${CI:-}" ]; then echo "contract-sevdesk never runs in CI" >&2; exit 1; fi
    source "{{justfile_directory()}}/scripts/sevdesk_trial_env.sh" "{{justfile_directory()}}"
    if [ ! -f "$env_file" ]; then echo "no trial configured: $env_file does not exist"; exit 0; fi
    SKILLFORGE_CONTRACT_RUN=1 uv run --env-file "$env_file" pytest -m contract

# Check the configured sevDesk ids, read-only: `just sevdesk-doctor trial|live [--discover]`.
[positional-arguments]
sevdesk-doctor account *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "${CI:-}" ]; then echo "sevdesk-doctor never runs in CI" >&2; exit 1; fi
    account="$1"
    shift
    case "$*" in
        "" | --discover) ;;
        *) echo "sevdesk-doctor takes trial or live and at most --discover" >&2; exit 1 ;;
    esac
    case "$account" in
        trial)
            source "{{justfile_directory()}}/scripts/sevdesk_trial_env.sh" "{{justfile_directory()}}"
            if [ ! -f "$env_file" ]; then echo "no trial configured: $env_file does not exist" >&2; exit 1; fi
            exec uv run --env-file "$env_file" python -m app.cli.sevdesk_doctor --account trial "$@" ;;
        live)
            exec uv run python -m app.cli.sevdesk_doctor --account live "$@" ;;
        *)
            echo "the account is trial or live, not $account" >&2; exit 1 ;;
    esac

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
