import hmac
import hashlib
import json
import os
from fastapi.testclient import TestClient
import types

from app.main import app


def sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def test_metrics_endpoint_exposes_prometheus():
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "service_info" in r.text


def test_webhook_invalid_signature_401(monkeypatch):
    client = TestClient(app)
    payload = {"action": "opened"}
    body = json.dumps(payload).encode("utf-8")
    # Ensure env secret exists
    monkeypatch.setenv("WEBHOOK_SECRET", os.getenv("WEBHOOK_SECRET", "test-secret"))

    r = client.post(
        "/webhook",
        data=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Delivery": "1",
        },
    )
    assert r.status_code == 401


def test_webhook_pull_request_enqueues(monkeypatch):
    from app import main as mainmod

    # No-op drain to avoid background worker in tests
    async def noop_drain(*args, **kwargs):
        return None

    monkeypatch.setattr(mainmod, "_drain_repo", noop_drain)

    # Fake Queue capturing enqueues
    calls = []

    class FakeQueue:
        def enqueue(self, installation_id, owner, repo, number, sender=None):
            calls.append((installation_id, owner, repo, number, sender))
            return True

    monkeypatch.setattr(mainmod, "Queue", lambda: FakeQueue())

    client = TestClient(app)
    payload = {
        "action": "labeled",
        "pull_request": {"number": 42},
        "repository": {"name": "repo", "owner": {"login": "octo"}},
        "installation": {"id": 123},
        "sender": {"login": "octocat"},
    }
    body = json.dumps(payload).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret")
    sig = sign(secret, body)

    r = client.post(
        "/webhook",
        data=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": "2",
        },
    )
    assert r.status_code == 202
    assert calls == [(123, "octo", "repo", 42, "octocat")]


def test_webhook_check_suite_resolves_prs(monkeypatch):
    from app import main as mainmod

    async def noop_drain(*args, **kwargs):
        return None

    monkeypatch.setattr(mainmod, "_drain_repo", noop_drain)

    # Fake Queue
    calls = []

    class FakeQueue:
        def enqueue(self, installation_id, owner, repo, number, sender=None):
            calls.append((installation_id, owner, repo, number, sender))
            return True

    monkeypatch.setattr(mainmod, "Queue", lambda: FakeQueue())

    # Mock GitHubClient.list_prs_for_commit to return a PR
    class FakeGH:
        def __init__(self, inst):
            pass

        def list_prs_for_commit(self, owner, repo, sha):
            assert sha == "abc123"
            return [{"number": 7}]

    monkeypatch.setattr(mainmod, "GitHubClient", FakeGH)

    client = TestClient(app)
    payload = {
        "check_suite": {"head_sha": "abc123"},
        "repository": {"name": "repo", "owner": {"login": "octo"}},
        "installation": {"id": 321},
        "sender": {"login": "ci"},
    }
    body = json.dumps(payload).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret")
    sig = sign(secret, body)

    r = client.post(
        "/webhook",
        data=body,
        headers={
            "X-GitHub-Event": "check_suite",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": "3",
        },
    )
    assert r.status_code == 202
    assert calls == [(321, "octo", "repo", 7, "ci")]
