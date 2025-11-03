# Auto-Merge Webhook Service — Project Guidelines
You are an expert in Python, FastAPI, async, and scalable web application development. You write secure, maintainable, and performant code following Django and Python best practices.


## Core Purpose
- Provide a GitHub App–backed webhook service that auto-merges PRs using a durable FIFO queue per repo.
- Enforce branch protection by deferring to GitHub’s APIs; never bypass protections.
- Process events serially per repo; update branches when needed; wait for checks to pass; merge with configurable method and commit message templates.

## Architecture Highlights
- FastAPI service exposing:
  - `POST /webhook` (HMAC-verified webhook ingestion)
  - `GET /metrics` (Prometheus)
  - `GET /healthz`, `GET /readyz`
- Redis-backed FIFO queue with de-duplication, per-repo lock, and optional backpressure throttle per installation.
- Metrics via `prometheus_client` with reasonable label cardinality (toggle available).
- Robust processing: retries with exponential backoff for idempotent calls, DLQ after max retries, starvation control (requeue tail once after a window), and lock refresh (heartbeat) while waiting.

## Configuration (env-driven; via `os.getenv`)
- GitHub App: `APP_ID`, `APP_PRIVATE_KEY` (filesystem path to PEM; PEM string accepted for backward compatibility), `WEBHOOK_SECRET`.
- GitHub API base: `GITHUB_API_URL` (default `https://api.github.com`).
- Redis: `REDIS_URL` (default `redis://localhost:6379/0`), `REDIS_NAMESPACE` (default `automerge`), `REDIS_LOCK_TTL_SECONDS` (default `60`), `REDIS_HEARTBEAT_SECONDS` (default `15`).
- Service: `PORT` (default `8080`), `LOG_LEVEL` (default `INFO`), optional `LOG_FORMAT` (planned: `text|json`).
- Rate limiting/backpressure: `RATE_LIMIT_MIN_REMAINING`, `RATE_LIMIT_COOLDOWN_SECONDS`, `RATE_LIMIT_JITTER_SECONDS`, `MAX_BACKOFF_SECONDS`.
- Retry/backoff + processing window: `MAX_RETRIES` (default `5`), `BACKOFF_BASE_SECONDS` (default `5`), `BACKOFF_FACTOR` (default `2`), `MAX_ITEM_WINDOW_SECONDS` (default `900`).
- Metrics: optional `PROMETHEUS_MULTIPROC_DIR` for multi-process collection.

## Repository Policy (.github/automerge.yml in target repos)
- Flat YAML k/v only. Recommended safe defaults:
  - `label: automerge`
  - `merge_method: squash`
  - `require_up_to_date: true`
  - `update_branch: true`
  - `allow_merge_when_no_checks: false` (checks required by default)
  - `max_wait_minutes: 60`
  - `poll_interval_seconds: 10`
  - `title_template: "{title} (#{number})"`
  - `body_template: "{body}\n\nAuto-merged by Auto Merge Bot for PR #{number}"`

## Development Workflow
- Always use the project virtual environment and uv:
  - macOS/Linux:
    - `python -m venv .venv`
    - `source .venv/bin/activate`
    - `uv sync --extra dev` (or `uv pip install -e .[dev]`)
- Run tests with uv:
  - Quick: `uv run -m pytest -q`
  - Verbose: `uv run -m pytest -q -v`
  - Coverage: `uv run -m pytest --cov=app --cov-report=term-missing`
- Lint/format:
  - Ruff format check: `ruff format --check --diff .`
  - Apply format: `ruff format .`
  - Lint: `ruff check --output-format=github .`
- YAML lint (line length 120): `yamllint -f github .`
- Pre-commit (uses uv environments):
  - Install: `pre-commit install && pre-commit install --hook-type pre-push`
  - Run all: `PRE_COMMIT_USE_UV=1 pre-commit run --all-files`
  - Pre-push runs tests via `uv run -m pytest -q`.

## Running the Service Locally
- With uvicorn:
  - `uv run uvicorn app.main:app --host 0.0.0.0 --port 8080`
  - Debug: add `--log-level debug --access-log --proxy-headers --forwarded-allow-ips "*"`
- Docker Compose: `docker compose up -d` (ensure `APP_ID`, `APP_PRIVATE_KEY` path, `WEBHOOK_SECRET`, `REDIS_URL` are set).
- TLS: terminate in a reverse proxy or use a tunnel (ngrok/cloudflared). Point the GitHub App webhook to the HTTPS URL and forward to `http://app:8080/webhook`.

## Logging & Redaction
- Default `LOG_LEVEL=INFO`; debug logs can be enabled via uvicorn flags.
- Do not log webhook payloads, secrets, tokens, or commit titles/bodies.
- Structured logging with contextual fields (installation, owner, repo, pr, phase) is preferred; JSON format may be enabled via `LOG_FORMAT=json` (when implemented).

## Metrics
- Prometheus at `/metrics`.
- Key families: webhook ingress, queue depth/age, worker/locks, GitHub API, rate limits/backpressure, merge attempts/outcomes, retries/DLQ/starvation.
- Multiprocess: set `PROMETHEUS_MULTIPROC_DIR` if running multiple workers; otherwise prefer a single-process app for simplicity.
- Cardinality controls: keep owner/repo labels; consider toggling/hashing in large-scale deployments (feature available/underway).

## Queueing, Retries, DLQ, and Starvation Controls
- FIFO per repo using Redis list + dedupe set.
- Retries: transient failures requeued with exponential backoff; after `MAX_RETRIES`, moved to DLQ.
- DLQ is visible via Redis (e.g., key prefix `…:dlq:`); requeue manually with `redis-cli` (documented in README runbook).
- Starvation control: if a PR exceeds `MAX_ITEM_WINDOW_SECONDS`, it is requeued to the tail once to allow progress; tracked via metrics.
- Lock refresh: long waits (e.g., for checks) refresh the per-repo lock to avoid dual workers.

## GitHub API Semantics
- Idempotent `GET`/`PUT` (non-merge) requests retried on 5xx/429/403 with exponential backoff and rate-limit aware cooldown.
- Merge API is non-idempotent; do not retry merges automatically.
- Update-branch 422/conflict is treated as terminal (metric: `branch_updates_total{result="fail"}`) — requires manual resolution.
- Reviews visibility: if mergeable state indicates `blocked`, increment `merge_blocked_total{reason="reviews_or_protection"}`.
- Re-fetch PR just before merge; abort if draft/locked/missing label/not mergeable.

## Security & Permissions
- GitHub App least-privilege:
  - Repository permissions: Contents (Read/Write), Pull requests (Read/Write), Checks (Read), Commit statuses (Read). Metadata: Read.
  - Events: `pull_request`, `check_suite`, `status`.
- Webhook HMAC validation is mandatory.
- Private key: mount as read-only, correct permissions; `APP_PRIVATE_KEY` is a path; never print PEM contents.
- Avoid logging sensitive data; redact where necessary.

## CI/CD (GitHub Actions)
- Unit tests on PRs (pytest + coverage). Prefer uv for dependency install and test execution.
- Ruff lint and formatting checks on PRs.
- YAML linting on PRs (yamllint, line length 120).
- Pre-commit CI job runs `pre-commit run --all-files` with uv-backed envs.
- Trivy workflow present but disabled (can be re-enabled later).

## Troubleshooting
- 404 on external IP but 200 on `127.0.0.1`: likely router/NAT loopback; verify port-forward, hairpin NAT, or use a tunnel.
- `WARNING: Invalid HTTP request received.`: indicates TLS/HTTP mismatch or proxy sending non-HTTP bytes; ensure TLS terminates before uvicorn.
- Rate-limit backpressure: check `backpressure_active{installation=…}`; wait until reset time or adjust env thresholds.
- Webhook accepted (202) but no merges: inspect metrics (`events_enqueued_total`, `worker_lock_acquired_total`, `merge_attempts_total`, `merges_*`), and Redis queue/lock/throttle keys.

## Code Style & Conventions
- Python 3.11+; Ruff for linting and formatting; line length 120 (YAML as well).
- Keep functions small and log key decisions at DEBUG without leaking secrets.
- Tests should be accurate and fast: patch `time.sleep` only within tests that exercise backoff/waits and assert that sleep was invoked to prove the path executed.
- Follow PEP 8 with 120 character line limit
- Use double quotes for Python strings
- Sort imports with `isort`
- Use f-strings for string formatting

## Contribution Checklist
1. `source .venv/bin/activate` and `uv sync --extra dev`
2. `ruff format . && ruff check .`
3. `yamllint -f github .`
4. `uv run -m pytest -q` (or with coverage)
5. `PRE_COMMIT_USE_UV=1 pre-commit run --all-files`
6. Ensure CI passes (tests, Ruff, yamllint); keep Trivy disabled unless re-enabled intentionally

---
These guidelines evolve with the project. When in doubt, prefer safety (respect protections), observability (metrics + logs), and operational simplicity (clear retries, DLQ, and runbooks).