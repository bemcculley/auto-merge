import json
import time
from datetime import datetime, timedelta, timezone


import app.github as ghmod


class FakeResp:
    def __init__(self, status_code=201, headers=None, body=None):
        self.status_code = status_code
        self._headers = headers or {}
        self._body = body or {}
        # provide sensible defaults for rate limit headers
        self._headers.setdefault("X-RateLimit-Remaining", "4999")
        self._headers.setdefault("X-RateLimit-Reset", str(int(time.time()) + 3600))

    @property
    def headers(self):
        return self._headers

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


def test_shared_installation_token_cache(monkeypatch):
    calls = {"post": 0}

    # Patch the token exchange POST
    def fake_post(url, headers=None, timeout=None):
        calls["post"] += 1
        exp = (datetime.now(tz=timezone.utc) + timedelta(minutes=60)).isoformat()
        return FakeResp(status_code=201, body={"token": "t1", "expires_at": exp})

    monkeypatch.setattr("httpx.post", fake_post)

    # Avoid needing a real RSA key
    monkeypatch.setattr(ghmod.GitHubClient, "_app_jwt", lambda self: "dummy")

    inst = 12345

    # Ensure clean cache between test runs
    ghmod.GitHubClient._tok_cache.pop(inst, None)

    c1 = ghmod.GitHubClient(inst)
    c2 = ghmod.GitHubClient(inst)

    # First client triggers a token exchange
    h1 = c1._headers()
    assert "Authorization" in h1

    # Second client should reuse the shared cached token without another POST
    h2 = c2._headers()
    assert "Authorization" in h2

    assert calls["post"] == 1


def test_token_refresh_after_expiry(monkeypatch):
    calls = {"post": 0}

    # Two sequential POSTs: first returns a nearly-expired token, second returns long-lived
    def fake_post(url, headers=None, timeout=None):
        calls["post"] += 1
        if calls["post"] == 1:
            exp = (datetime.now(tz=timezone.utc) + timedelta(seconds=30)).isoformat()
            tok = "short"
        else:
            exp = (datetime.now(tz=timezone.utc) + timedelta(minutes=60)).isoformat()
            tok = "long"
        return FakeResp(status_code=201, body={"token": tok, "expires_at": exp})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(ghmod.GitHubClient, "_app_jwt", lambda self: "dummy")

    inst = 22222
    ghmod.GitHubClient._tok_cache.pop(inst, None)

    c1 = ghmod.GitHubClient(inst)
    # First headers call mints short token (expires in 30s; safety margin is 120s → considered expiring)
    _ = c1._headers()
    # Immediately create a second client; because the cached token is within safety margin, it should refresh again
    c2 = ghmod.GitHubClient(inst)
    _ = c2._headers()

    assert calls["post"] == 2
