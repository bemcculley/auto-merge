import os
import time
from typing import Optional
from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest, Counter, Gauge, Histogram

try:
    from prometheus_client import multiprocess
except Exception:  # pragma: no cover
    multiprocess = None  # type: ignore


def build_registry() -> CollectorRegistry:
    """Build a Prometheus registry, supporting multiprocess if PROMETHEUS_MULTIPROC_DIR is set."""
    registry = CollectorRegistry()
    mp_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if mp_dir and multiprocess is not None:
        multiprocess.MultiProcessCollector(registry)
    return registry


REGISTRY: CollectorRegistry = build_registry()

# Webhook ingress metrics
webhook_requests_total = Counter(
    "webhook_requests_total",
    "Webhook requests received",
    labelnames=("event", "action", "code"),
    registry=REGISTRY,
)
webhook_invalid_signatures_total = Counter(
    "webhook_invalid_signatures_total",
    "Webhook requests with invalid HMAC signatures",
    registry=REGISTRY,
)
webhook_parse_failures_total = Counter(
    "webhook_parse_failures_total",
    "Webhook payload parse failures",
    labelnames=("event",),
    registry=REGISTRY,
)

# Queue metrics
events_enqueued_total = Counter(
    "events_enqueued_total",
    "Events accepted and enqueued (after dedupe)",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
events_deduped_total = Counter(
    "events_deduped_total",
    "Events dropped due to in-queue dedupe",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
queue_push_failures_total = Counter(
    "queue_push_failures_total",
    "Redis push errors",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
queue_pop_total = Counter(
    "queue_pop_total",
    "Successful pops for processing",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
queue_pop_empty_total = Counter(
    "queue_pop_empty_total",
    "Empty pops (no queue items)",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
queue_depth = Gauge(
    "queue_depth",
    "Current queue depth",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
queue_oldest_age_seconds = Gauge(
    "queue_oldest_age_seconds",
    "Age in seconds of the oldest queued item (0 if empty)",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)

# Redis and worker metrics
redis_latency_seconds = Histogram(
    "redis_latency_seconds",
    "Round-trip latency for Redis operations",
    labelnames=("op",),
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
worker_lock_acquired_total = Counter(
    "worker_lock_acquired_total",
    "Worker lock acquisitions",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
worker_lock_failed_total = Counter(
    "worker_lock_failed_total",
    "Worker lock acquisition failures",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
worker_lock_lost_total = Counter(
    "worker_lock_lost_total",
    "Worker lock lost mid-processing",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
worker_active = Gauge(
    "worker_active",
    "1 when worker holds lock and is processing; 0 otherwise",
    labelnames=("owner", "repo"),
    registry=REGISTRY,
)
worker_processing_seconds = Histogram(
    "worker_processing_seconds",
    "Worker phase durations",
    labelnames=("phase", "owner", "repo"),
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
retries_total = Counter(
    "retries_total",
    "Retries by phase and reason",
    labelnames=("phase", "reason"),
    registry=REGISTRY,
)

# GitHub API metrics
github_api_requests_total = Counter(
    "github_api_requests_total",
    "Outbound GitHub API requests",
    labelnames=("endpoint", "status"),
    registry=REGISTRY,
)
github_api_latency_seconds = Histogram(
    "github_api_latency_seconds",
    "Latency of GitHub API requests",
    labelnames=("endpoint",),
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
# Rate limit and backpressure metrics
github_rate_limit_remaining = Gauge(
    "github_rate_limit_remaining",
    "GitHub REST API remaining requests",
    labelnames=("installation",),
    registry=REGISTRY,
)
github_rate_limit_reset = Gauge(
    "github_rate_limit_reset",
    "Epoch seconds when GitHub rate limit resets",
    labelnames=("installation",),
    registry=REGISTRY,
)
throttles_total = Counter(
    "throttles_total",
    "Times the service engaged backpressure due to rate limits",
    labelnames=("scope", "reason"),
    registry=REGISTRY,
)
backpressure_active = Gauge(
    "backpressure_active",
    "1 when backpressure/throttle is active for an installation",
    labelnames=("installation",),
    registry=REGISTRY,
)
config_load_failures_total = Counter(
    "config_load_failures_total",
    "Failures to load repository configuration",
    registry=REGISTRY,
)

# Merge behavior metrics
branch_updates_total = Counter(
    "branch_updates_total",
    "Attempted update-branch outcomes",
    labelnames=("result",),
    registry=REGISTRY,
)
checks_wait_seconds = Histogram(
    "checks_wait_seconds",
    "Time spent waiting for checks to pass after branch update",
    registry=REGISTRY,
    buckets=(5, 10, 20, 30, 60, 120, 300, 600, 1200, 3600),
)
merge_attempts_total = Counter(
    "merge_attempts_total",
    "Merge attempts by method and result",
    labelnames=("method", "result"),
    registry=REGISTRY,
)
merges_success_total = Counter(
    "merges_success_total",
    "Successful merges by method",
    labelnames=("method",),
    registry=REGISTRY,
)
merges_failed_total = Counter(
    "merges_failed_total",
    "Failed merges by reason",
    labelnames=("reason",),
    registry=REGISTRY,
)

# Build info (set from environment)
service_info = Gauge(
    "service_info",
    "Service build/version info labeled on 1",
    labelnames=("version",),
    registry=REGISTRY,
)
service_info.labels(version=os.getenv("SERVICE_VERSION", "dev")).set(1)


def metrics_response():
    data = generate_latest(REGISTRY)
    return CONTENT_TYPE_LATEST, data
