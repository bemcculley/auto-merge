import time
import base64
import logging
from typing import Any, Dict, Optional, Tuple, List
import httpx
import jwt
from datetime import datetime, timedelta, timezone

from .config import SETTINGS
from .metrics import (
    github_api_requests_total,
    github_api_latency_seconds,
    config_load_failures_total,
    github_rate_limit_remaining,
    github_rate_limit_reset,
    throttles_total,
)
from .queue import Queue

logger = logging.getLogger(__name__)


def _safe_url(url: str) -> str:
    try:
        u = httpx.URL(url)
        # remove query to avoid leaking params
        return str(u.copy_with(query=None))
    except Exception:
        return url.split("?", 1)[0]


def _param_keys(d: Optional[Dict[str, Any]]) -> List[str]:
    try:
        return sorted(list((d or {}).keys()))
    except Exception:
        return []


class GitHubClient:
    def __init__(self, installation_id: int):
        self.installation_id = installation_id
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self.base_url = SETTINGS.github_api_url
        self.app_id = SETTINGS.app_id
        self.private_key_pem = SETTINGS.app_private_key.encode("utf-8")
        self._queue = Queue()

    def _app_jwt(self) -> str:
        now = datetime.now(tz=timezone.utc)
        payload = {
            "iat": int(now.timestamp()) - 60,
            "exp": int((now + timedelta(minutes=10)).timestamp()),
            "iss": SETTINGS.app_id,
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    def _ensure_token(self) -> None:
        if self._token and time.time() < self._token_expiry - 60:
            return
        jwt_ = self._app_jwt()
        url = f"{self.base_url}/app/installations/{self.installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt_}",
            "Accept": "application/vnd.github+json",
        }
        endpoint = "POST /app/installations/{id}/access_tokens"
        start = time.perf_counter()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "github.request: method=POST path=%s installation=%s phase=token_exchange",
                _safe_url(url),
                self.installation_id,
            )
        resp = httpx.post(url, headers=headers, timeout=30)
        duration = time.perf_counter() - start
        github_api_latency_seconds.labels(endpoint=endpoint).observe(duration)
        github_api_requests_total.labels(endpoint=endpoint, status=str(resp.status_code)).inc()
        if logger.isEnabledFor(logging.DEBUG):
            rl_rem = resp.headers.get("X-RateLimit-Remaining")
            rl_reset = resp.headers.get("X-RateLimit-Reset")
            logger.debug(
                "github.response: method=POST path=%s status=%s duration_ms=%d installation=%s rl_remaining=%s rl_reset=%s phase=token_exchange",
                _safe_url(url),
                resp.status_code,
                int(duration * 1000),
                self.installation_id,
                rl_rem,
                rl_reset,
            )
        resp.raise_for_status()
        data = resp.json()
        self._token = data.get("token")
        expires_at = data.get("expires_at")  # e.g., 2024-01-01T00:00:00Z
        if expires_at:
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            self._token_expiry = dt.timestamp()
        else:
            self._token_expiry = time.time() + 3600

    def _headers(self) -> Dict[str, str]:
        self._ensure_token()
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "auto-merge-app/1.0",
        }

    def request(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None, data: Optional[Any] = None
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        endpoint = f"{method} {path if path.startswith('/') else '/' + path}"

        def should_retry(resp: Optional[httpx.Response], exc: Optional[Exception]) -> bool:
            # Retry on network/timeout errors
            if exc is not None:
                return True
            if resp is None:
                return False
            status = resp.status_code
            # Respect backpressure already set by _handle_rate_limit; still retry a few times
            # Retry on 5xx always; on 429/403 (rate limit/secondary) for idempotent requests only
            idempotent = method.upper() in ("GET", "PUT") and not endpoint.endswith("/merge")
            if status >= 500:
                return True
            if status in (429, 403) and idempotent:
                return True
            return False

        attempts = 0
        last_exc: Optional[Exception] = None
        while True:
            attempts += 1
            start = time.perf_counter()
            exc: Optional[Exception] = None
            resp: Optional[httpx.Response] = None
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "github.request: method=%s path=%s installation=%s params=%s attempt=%s",
                    method.upper(),
                    _safe_url(url),
                    self.installation_id,
                    _param_keys(params),
                    attempts,
                )
            try:
                resp = httpx.request(method, url, headers=self._headers(), params=params, json=data, timeout=60)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                exc = e
            duration = time.perf_counter() - start
            status_label = str(resp.status_code) if resp is not None else "exc"
            github_api_latency_seconds.labels(endpoint=endpoint).observe(duration)
            github_api_requests_total.labels(endpoint=endpoint, status=status_label).inc()
            if resp is not None:
                self._handle_rate_limit(resp)
            # Log response or exception
            if logger.isEnabledFor(logging.DEBUG):
                if resp is not None:
                    rl_rem = resp.headers.get("X-RateLimit-Remaining")
                    rl_reset = resp.headers.get("X-RateLimit-Reset")
                    logger.debug(
                        "github.response: method=%s path=%s status=%s duration_ms=%d installation=%s rl_remaining=%s rl_reset=%s attempt=%s",
                        method.upper(),
                        _safe_url(url),
                        resp.status_code,
                        int(duration * 1000),
                        self.installation_id,
                        rl_rem,
                        rl_reset,
                        attempts,
                    )
                else:
                    logger.debug(
                        "github.response_error: method=%s path=%s error=%s duration_ms=%d installation=%s attempt=%s",
                        method.upper(),
                        _safe_url(url),
                        exc,
                        int(duration * 1000),
                        self.installation_id,
                        attempts,
                    )
            if not should_retry(resp, exc) or attempts >= 3:
                if exc is not None:
                    # Synthesize a Response-like object on exception by raising or return a dummy response
                    # We raise to let callers decide.
                    last_exc = exc
                    break
                return resp  # type: ignore
            # sleep with exponential backoff
            sleep_s = min(
                SETTINGS.backoff_base_seconds * (SETTINGS.backoff_factor ** (attempts - 1)),
                SETTINGS.max_backoff_seconds,
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "github.retry: method=%s path=%s sleep_seconds=%s attempt=%s installation=%s",
                    method.upper(),
                    _safe_url(url),
                    sleep_s,
                    attempts,
                    self.installation_id,
                )
            time.sleep(sleep_s)
        # If we exit loop due to exception after retries, raise it
        if last_exc:
            raise last_exc
        # Fallback (should not happen)
        return resp  # type: ignore

    def _handle_rate_limit(self, resp: httpx.Response) -> None:
        try:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            if remaining is not None:
                try:
                    rem_i = int(remaining)
                    github_rate_limit_remaining.labels(installation=str(self.installation_id)).set(rem_i)
                except ValueError:
                    pass
            if reset is not None:
                try:
                    reset_i = int(reset)
                    github_rate_limit_reset.labels(installation=str(self.installation_id)).set(reset_i)
                except ValueError:
                    pass
            # Decide on throttle
            low_budget = False
            reason = None
            status = resp.status_code
            if remaining is not None:
                try:
                    low_budget = int(remaining) <= SETTINGS.rate_limit_min_remaining
                except ValueError:
                    low_budget = False
            # Primary limit: 403 with header remaining=0 or 429
            if status in (403, 429) or low_budget:
                reason = (
                    "secondary"
                    if status == 403
                    and "secondary"
                    in (
                        resp.json().get("message", "").lower()
                        if (resp.headers.get("content-type") or resp.headers.get("Content-Type") or " ")
                        .lower()
                        .startswith("application/json")
                        else ""
                    )
                    else ("primary" if status != 429 else "retry_after")
                )
                retry_after = resp.headers.get("Retry-After")
                now = time.time()
                until = None
                if retry_after:
                    try:
                        until = now + int(retry_after)
                    except ValueError:
                        until = None
                if until is None and reset is not None:
                    try:
                        until = int(reset)
                    except ValueError:
                        until = None
                if until is None:
                    until = now + SETTINGS.rate_limit_cooldown_seconds
                # Add small jitter to avoid thundering herd
                until = until + min(SETTINGS.rate_limit_jitter_seconds, 15)
                # Persist throttle per installation
                try:
                    q = self._queue
                    q.set_throttle(self.installation_id, until, reason=reason or "rate_limit")
                    throttles_total.labels(scope="installation", reason=reason or "rate_limit").inc()
                except Exception:
                    pass
        except Exception:
            # Never fail request because of metrics/throttle logic
            return

    def list_prs_for_commit(self, owner: str, repo: str, sha: str) -> List[Dict[str, Any]]:
        """List pull requests associated with a commit SHA."""
        # GitHub supports this endpoint without preview headers now
        r = self.request("GET", f"/repos/{owner}/{repo}/commits/{sha}/pulls")
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return []
        return []

    # --- Convenience methods ---
    def get_pr(self, owner: str, repo: str, number: int) -> Optional[Dict[str, Any]]:
        r = self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        if r.status_code == 200:
            return r.json()
        return None

    def list_prs_with_label(self, owner: str, repo: str, label: str) -> List[Dict[str, Any]]:
        prs: List[Dict[str, Any]] = []
        page = 1
        while True:
            params = {"state": "open", "per_page": 100, "page": page}
            r = self.request("GET", f"/repos/{owner}/{repo}/pulls", params=params)
            if r.status_code != 200:
                break
            batch = [p for p in r.json() if any(lbl["name"] == label for lbl in p.get("labels", []))]
            prs.extend(batch)
            if len(r.json()) < 100:
                break
            page += 1
        return prs

    def get_combined_status(self, owner: str, repo: str, sha: str) -> Dict[str, Any]:
        r = self.request("GET", f"/repos/{owner}/{repo}/commits/{sha}/status")
        if r.status_code == 200:
            return r.json()
        return {"state": "pending", "statuses": []}

    def list_check_suites(self, owner: str, repo: str, sha: str) -> List[Dict[str, Any]]:
        r = self.request("GET", f"/repos/{owner}/{repo}/commits/{sha}/check-suites")
        if r.status_code == 200:
            return r.json().get("check_suites", [])
        return []

    def are_checks_green(self, owner: str, repo: str, sha: str) -> bool:
        combined = self.get_combined_status(owner, repo, sha)
        if combined.get("state") not in ("success", "neutral"):
            return False
        suites = self.list_check_suites(owner, repo, sha)
        for s in suites:
            if s.get("conclusion") not in ("success", "neutral"):
                return False
        return True

    def update_branch(self, owner: str, repo: str, number: int) -> bool:
        r = self.request("PUT", f"/repos/{owner}/{repo}/pulls/{number}/update-branch")
        return r.status_code in (200, 202)

    def merge_pr(
        self, owner: str, repo: str, number: int, method: str, commit_title: str, commit_message: str
    ) -> Tuple[bool, str]:
        data = {
            "merge_method": method,
            "commit_title": commit_title,
            "commit_message": commit_message,
        }
        r = self.request("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge", data=data)
        if r.status_code in (200, 201):
            return True, f"Merged PR #{number} via {method}"
        try:
            payload = r.json()
        except Exception:
            payload = {"message": r.text}
        return False, f"Merge failed for PR #{number}: {r.status_code} {payload}"

    def load_repo_file(self, owner: str, repo: str, path: str) -> Optional[str]:
        r = self.request("GET", f"/repos/{owner}/{repo}/contents/{path}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("encoding") == "base64":
                try:
                    return base64.b64decode(data.get("content", "")).decode("utf-8")
                except Exception:
                    config_load_failures_total.inc()
        return None
