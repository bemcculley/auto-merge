# Auto Merge Webhook Service

Auto‑merge pull requests safely and serially via a GitHub App webhook service (FastAPI + Redis). The service receives GitHub webhooks, enqueues PRs in a durable FIFO per repo, and processes them serially until merged; exposes Prometheus metrics at `/metrics`. It ensures PRs are up‑to‑date with the target branch (optionally auto‑update), respects branch protection rules, and supports customizable commit messages and merge strategies.

---

## Features
- Label‑gated merging (default label: `automerge`).
- FIFO queueing for predictable merge order.
- Ensures PR is up‑to‑date with base branch; can auto‑update if behind.
- Honors branch protection rules (required checks/approvals).
- Merge method: `squash`, `rebase`, or `merge`.
- Customizable commit title/body templates that include PR number by default.
- Webhook service includes detailed Prometheus metrics at `/metrics` and health endpoints `/healthz`, `/readyz`.
- Each installation/repo has its own FIFO list and lock; the worker drains one item at a time until the queue is empty. Metrics are exported for observability.

---

## Quick Start

Deploy the GitHub App webhook service to enable durable FIFO auto-merge with metrics.

Prerequisites:
- A GitHub App installed on your org/repos
- Redis 7+
- Container runtime (Docker) or a Kubernetes cluster

1) Create a GitHub App
- App name: Auto Merge Webhook
- Webhook URL: `https://<your-host>/webhook`
- Webhook secret: generate and save as `WEBHOOK_SECRET`
- Permissions (least privilege):
  - Metadata: Read (implicit)
  - Pull requests: Read & Write (required to merge PRs)
  - Contents: Read & Write (read repo config; write required for update-branch)
  - Checks: Read (to evaluate check suites)
  - Commit statuses: Read (to read combined status)
- Subscribe to events:
  - `pull_request` (opened, reopened, synchronize, labeled, unlabeled, ready_for_review)
  - `check_suite` (completed)
  - `status`
- Generate and download a private key (PEM)
- Note the `APP_ID`; install the app on your repos
- Save the private key file locally (e.g., `./private-key.pem`) or in a secret store

2) Run with Docker Compose (local or single‑host)

Create a `.env` file (or export env vars):

```
APP_ID=123456
APP_PRIVATE_KEY=/run/secrets/app_private_key.pem
WEBHOOK_SECRET=your-shared-secret
```

Start services:

```
docker compose up -d --build
```

This will run:
- Redis with persistence (AOF)
- FastAPI server on `http://localhost:8080`

Expose it publicly (for GitHub webhooks) via a tunnel or reverse proxy (e.g., `ngrok http 8080`). Update the GitHub App webhook URL accordingly.

3) Run on Kubernetes

- Apply Redis and the app manifests (review and adapt images/hosts/secrets):

```
kubectl apply -f k8s/redis.yaml
kubectl create secret generic auto-merge-secrets \
  --from-literal=APP_ID=<app_id> \
  --from-literal=WEBHOOK_SECRET=<webhook_secret> \
  --from-file=app-private-key.pem=path/to/private-key.pem
kubectl apply -f k8s/app-deployment.yaml
```

- Add an Ingress/LoadBalancer to expose `/webhook` externally. Ensure TLS where possible.
- Prometheus scraping is enabled via annotations on the Deployment’s Pod template.

---

## Configuration

### Repository config (`.github/automerge.yml`)

In each target repository that will be auto-merged by this app, create `.github/automerge.yml` with the following keys:

```
label: automerge
merge_method: squash  # one of: squash | rebase | merge
require_up_to_date: true
update_branch: true
# Require all branch checks to pass before merging. If your repo has no checks at all,
# the worker will wait up to max_wait_minutes for checks to appear, then proceed only
# when they pass. You can explicitly opt-out (unsafe) by setting allow_merge_when_no_checks: true.
allow_merge_when_no_checks: false
max_wait_minutes: 60
poll_interval_seconds: 10
# Available template vars: {number}, {title}, {body}, {head}, {base}, {user}
title_template: "{title} (#{number})"
body_template: "{body}\n\nAuto-merged by Auto Merge Bot for PR #{number}"
```

Behavior notes:
- The worker evaluates combined commit status and check suites. All must be green/neutral.
- Race-safe: if the `automerge` label is applied immediately after a push and no checks are yet present,
  the worker treats this as pending and waits (polling every `poll_interval_seconds`) up to `max_wait_minutes`.
  It re-evaluates on each poll and merges only when checks turn green. If the timeout elapses, it stops and
  will be retriggered by subsequent webhook events (e.g., `check_suite`, `status`).

### Webhook service environment variables

- `APP_ID` (required): GitHub App ID
- `APP_PRIVATE_KEY` (required): Filesystem path to the PEM private key (e.g., `/run/secrets/app_private_key.pem`)
- `WEBHOOK_SECRET` (required): shared secret to verify webhook HMAC
- `REDIS_URL` (default: `redis://localhost:6379/0`)
- `REDIS_NAMESPACE` (default: `automerge`)
- `REDIS_LOCK_TTL_SECONDS` (default: `60`)
- `REDIS_HEARTBEAT_SECONDS` (default: `15`)
- `GITHUB_API_URL` (default: `https://api.github.com`)
- `SERVICE_VERSION` (default: `dev`)
- `PORT` (default: `8080`) and `HOST` (default: `0.0.0.0`) for server binding
- Rate limit/backpressure:
  - `RATE_LIMIT_MIN_REMAINING` (default: `50`) — when remaining quota falls at or below this, begin backpressure
  - `RATE_LIMIT_COOLDOWN_SECONDS` (default: `60`) — fallback cooldown if reset/Retry-After not provided
  - `RATE_LIMIT_JITTER_SECONDS` (default: `15`) — jitter added to cooldown to avoid thundering herd
  - `MAX_BACKOFF_SECONDS` (default: `120`) — cap for drain loop sleep when throttled
- Optional multi‑process metrics:
  - `PROMETHEUS_MULTIPROC_DIR` (enables multiprocess registry if set)

---

## Endpoints (webhook service)

- `POST /webhook` — GitHub webhook receiver (validates `X‑Hub‑Signature‑256`).
- `GET /metrics` — Prometheus metrics (text format).
- `GET /healthz` — Liveness check.
- `GET /readyz` — Readiness check.

---

## Prometheus Metrics

Key metric families (labels trimmed for brevity):

- Webhook ingress: `webhook_requests_total`, `webhook_invalid_signatures_total`, `webhook_parse_failures_total`
- Queue/Redis: `events_enqueued_total`, `events_deduped_total`, `queue_depth`, `queue_oldest_age_seconds`, `queue_pop_total`, `queue_pop_empty_total`, `redis_latency_seconds`
- Worker/locks: `worker_lock_acquired_total`, `worker_lock_failed_total`, `worker_lock_lost_total`, `worker_active`, `worker_processing_seconds`, `retries_total`
- GitHub API: `github_api_requests_total`, `github_api_latency_seconds`, `config_load_failures_total`
- Rate limit/backpressure: `github_rate_limit_remaining`, `github_rate_limit_reset`, `throttles_total`, `backpressure_active`
- Merge outcomes: `branch_updates_total`, `checks_wait_seconds`, `merge_attempts_total`, `merges_success_total`, `merges_failed_total`

Kubernetes scrape annotations are already present in `k8s/app-deployment.yaml`:

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "8080"
    prometheus.io/path: "/metrics"
```

With Prometheus Operator, create a ServiceMonitor pointing to the app Service.

---

## Running Locally

- Install dependencies (for the webhook service):

```
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

- Or use Docker Compose (preferred):

```
docker compose up -d --build
```

- Send a test request (signature is required in real use):

```
curl -X POST http://localhost:8080/webhook -d '{}' -H 'X-GitHub-Event: ping' -H 'X-Hub-Signature-256: sha256=dummy'
```

---

## Testing

### Pre-commit (local checks aligned with GitHub Actions)
Pre-commit hooks enforce the same checks locally that run in CI (Ruff format/lint, yamllint). A pre-push hook runs the unit tests using uv.

Setup once (using uv):

```
uv sync --extra dev
pre-commit install                       # install default (commit) hooks
pre-commit install --hook-type pre-push  # install pre-push tests hook
```

Run on all files manually (two equivalent options):

```
# Prefer letting pre-commit create hook envs with uv
PRE_COMMIT_USE_UV=1 pre-commit run --all-files

# or run pre-commit itself via uv (uses the project venv)
uv run pre-commit run --all-files
```

Troubleshooting (Homebrew pre-commit):
- If you see an error like `No module named virtualenv` from pre-commit trying to build hook envs, enable uv-backed env creation:

  `PRE_COMMIT_USE_UV=1 pre-commit run --all-files`

  or upgrade pre-commit to >= 3.6.0 and ensure `uv` is installed: `pip install uv`.

Notes:
- The commit hooks run:
  - `ruff format` (apply formatting)
  - `ruff check` (lint)
  - `yamllint` (per .yamllint.yaml)
  - basic hygiene checks (trailing whitespace, EOF fixer, merge conflicts, JSON/YAML syntax)
- The pre-push hook runs: `uv run -m pytest -q`. Ensure dev deps are installed (via `uv sync --extra dev`).
- CI mirrors these: see `.github/workflows/ruff.yml`, `.github/workflows/yamllint.yml`, `.github/workflows/ci.yml`, and `.github/workflows/pre-commit.yml`.

Run unit tests:

```
pip install -e .[dev]
pytest -q
```

Code coverage reports (requires `pytest-cov`, included in `[dev]` extras):

- Terminal summary with missing lines:
  ```
  pytest -q --cov=app --cov-report=term-missing
  ```
- HTML report in `htmlcov/` (open `htmlcov/index.html` in a browser):
  ```
  pytest --cov=app --cov-report=html
  ```

Tests cover:
- Webhook signature checks and enqueue logic
- FIFO queue behavior, dedupe, and gauges
- Worker merge pipeline (evaluate → update branch → wait checks → merge)
- Metrics endpoint availability
- Rate limit backpressure (429 and 403 secondary) leading to throttling

---

## Security Considerations
- Webhook HMAC (`X‑Hub‑Signature‑256`) is validated against `WEBHOOK_SECRET`.
- Use least‑privilege GitHub App permissions (see below).
- Store secrets in a secret manager or Kubernetes Secret.
- Expose `/webhook` over HTTPS; prefer using an Ingress with TLS termination.

### Least‑Privilege GitHub App permissions (recommended)
Grant only what is required for this service to function. At minimum:
- Pull requests: Read & Write — needed to update branches and merge PRs.
- Contents: Read & Write — read repo config under `.github/automerge.yml` and call `update-branch` (writes via PR API).
- Checks: Read — to read check suites/conclusions.
- Commit statuses: Read — to read the combined commit status.
- Metadata: Read — implied and always safe.

Subscribed events:
- `pull_request` (opened, reopened, synchronize, labeled, unlabeled, ready_for_review)
- `check_suite` (completed)
- `status`

Notes:
- Do not grant Administration, Issues, or Workflows permissions to this App.
- If you don’t want the app to write repo contents beyond PR updates, ensure it is only installed on repos where PR branch updates are acceptable per your policies.

---

## Troubleshooting
- Invalid signature (401): Check `WEBHOOK_SECRET` matches the GitHub App’s secret.
- Merge blocked: Ensure branch protection checks are green and required approvals are present.
- PR never merges: Verify the PR has the configured label (default `automerge`) and is not draft/locked.
- Redis errors: Confirm `REDIS_URL` and network connectivity; check Redis logs.
- Stuck worker: The per‑repo lock auto‑expires; ensure the pod/container clock is correct.

---

## Files of Interest
- Webhook service: `app/main.py`, `app/worker.py`, `app/queue.py`, `app/github.py`, `app/config.py`, `app/metrics.py`
- Containerization: `Dockerfile`, `docker-compose.yml`
- Kubernetes: `k8s/*.yaml`
- Tests: `tests/*.py`

---

## Roadmap / Ideas
- Dead‑letter queue with retry budgets and backoff.
- Admin endpoints (e.g., clear lock, requeue PR, list queues).
- Multi‑process deployment with `PROMETHEUS_MULTIPROC_DIR` baked into manifests.
- Optional rate‑limit aware backpressure.

---

## Contributing
- Issues and PRs are welcome.
- Run the test suite locally before submitting: `pip install -e .[dev] && pytest -q`.
- For larger changes, open an issue first to discuss the approach (especially around queue semantics and GitHub API usage).

---

## License
This project is provided as‑is. Add a license file of your choice (e.g., MIT) if you plan to distribute.
