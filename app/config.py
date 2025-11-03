import os
from typing import Optional


class Settings:
    # GitHub App config
    app_id: str
    app_private_key: str  # PEM contents (loaded from file path or env)
    webhook_secret: str

    # Server config
    host: str = "0.0.0.0"
    port: int = int(os.getenv("PORT", "8080"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Redis config
    redis_url: str
    redis_namespace: str
    redis_lock_ttl_seconds: int
    redis_heartbeat_seconds: int

    # General
    github_api_url: str
    service_version: str

    # Rate limit/backpressure config
    rate_limit_min_remaining: int
    rate_limit_cooldown_seconds: int
    rate_limit_jitter_seconds: int
    max_backoff_seconds: int

    def __init__(self) -> None:
        # Required secrets
        self.app_id = os.getenv("APP_ID", "").strip()
        # APP_PRIVATE_KEY now expected to be a filesystem path to the PEM file.
        # For backward compatibility, if a PEM string is provided directly, it will be used as-is.
        apk_env = os.getenv("APP_PRIVATE_KEY", "").strip()
        pem_contents = ""
        if apk_env:
            try:
                if os.path.isfile(apk_env):
                    with open(apk_env, "r", encoding="utf-8") as f:
                        pem_contents = f.read().strip()
                else:
                    # Heuristic: if it looks like a PEM string, accept it directly
                    if "-----BEGIN" in apk_env and "PRIVATE KEY-----" in apk_env:
                        pem_contents = apk_env
                    else:
                        # Path not found; leave empty so failures are explicit at use time
                        pem_contents = apk_env  # still set to provide visibility
            except Exception:
                pem_contents = apk_env
        self.app_private_key = pem_contents
        self.webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()

        # Redis
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.redis_namespace = os.getenv("REDIS_NAMESPACE", "automerge")
        self.redis_lock_ttl_seconds = int(os.getenv("REDIS_LOCK_TTL_SECONDS", "60"))
        self.redis_heartbeat_seconds = int(os.getenv("REDIS_HEARTBEAT_SECONDS", "15"))

        # GitHub
        self.github_api_url = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.service_version = os.getenv("SERVICE_VERSION", "dev")

        # Rate limit/backpressure
        self.rate_limit_min_remaining = int(os.getenv("RATE_LIMIT_MIN_REMAINING", "50"))
        self.rate_limit_cooldown_seconds = int(os.getenv("RATE_LIMIT_COOLDOWN_SECONDS", "60"))
        self.rate_limit_jitter_seconds = int(os.getenv("RATE_LIMIT_JITTER_SECONDS", "15"))
        self.max_backoff_seconds = int(os.getenv("MAX_BACKOFF_SECONDS", "120"))

    def redis_key(self, *parts: str) -> str:
        return f"{self.redis_namespace}:" + ":".join(parts)


SETTINGS = Settings()
