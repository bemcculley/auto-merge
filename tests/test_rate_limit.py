import time
import httpx
import pytest

from app.github import GitHubClient


class DummyResponse:
    def __init__(self, status_code: int, headers: dict | None = None, body: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = body or {}
        self._text = None

    def json(self):
        return self._json

    @property
    def text(self):
        if self._text is None:
            try:
                import json as _json
                self._text = _json.dumps(self._json)
            except Exception:
                self._text = ""
        return self._text


@pytest.fixture(autouse=True)
def _bypass_headers(monkeypatch):
    # Avoid real JWT/token fetch
    monkeypatch.setattr(GitHubClient, "_headers", lambda self: {})


def test_429_triggers_throttle(monkeypatch):
    calls = {}

    class FakeQueue:
        def set_throttle(self, installation_id, until, reason="rate_limit"):
            calls["set"] = {"installation_id": installation_id, "until": until, "reason": reason}

    # Replace internal Queue used by GitHubClient
    import app.github as ghmod
    monkeypatch.setattr(ghmod, "Queue", lambda: FakeQueue())

    # Build a 429 response with Retry-After header
    resp = DummyResponse(
        429,
        headers={
            "Retry-After": "5",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(time.time()) + 60),
            "Content-Type": "application/json",
        },
        body={"message": "API rate limit exceeded"},
    )

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        return resp

    monkeypatch.setattr(httpx, "request", fake_request)

    client = GitHubClient(installation_id=42)
    r = client.request("GET", "/rate/limited")
    assert r.status_code == 429
    assert "set" in calls
    assert calls["set"]["installation_id"] == 42
    # reason comes from 429 branch: "retry_after"
    assert calls["set"]["reason"] in ("retry_after", "rate_limit")


def test_403_secondary_triggers_throttle(monkeypatch):
    calls = {}

    class FakeQueue:
        def set_throttle(self, installation_id, until, reason="rate_limit"):
            calls["set"] = {"installation_id": installation_id, "until": until, "reason": reason}

    import app.github as ghmod
    monkeypatch.setattr(ghmod, "Queue", lambda: FakeQueue())

    # 403 with secondary rate limit style message
    resp = DummyResponse(
        403,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(time.time()) + 120),
            "Content-Type": "application/json",
        },
        body={"message": "You have exceeded a secondary rate limit."},
    )

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        return resp

    monkeypatch.setattr(httpx, "request", fake_request)

    client = GitHubClient(installation_id=7)
    r = client.request("GET", "/some/endpoint")
    assert r.status_code == 403
    assert "set" in calls
    assert calls["set"]["installation_id"] == 7
    # reason should be marked secondary per handler logic
    assert calls["set"]["reason"] in ("secondary", "rate_limit")
