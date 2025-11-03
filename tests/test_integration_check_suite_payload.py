import hmac
import hashlib
import json
import os
from fastapi.testclient import TestClient

from app.main import app


def sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def test_webhook_check_suite_full_payload_enqueues(monkeypatch):
    # Avoid running background drain loop during the test
    from app import main as mainmod

    async def noop_drain(*args, **kwargs):
        return None

    monkeypatch.setattr(mainmod, "_drain_repo", noop_drain)

    # Capture enqueues
    calls = []

    class FakeQueue:
        def enqueue(self, installation_id, owner, repo, number, sender=None):
            calls.append((installation_id, owner, repo, number, sender))
            return True

    monkeypatch.setattr(mainmod, "Queue", lambda: FakeQueue())

    # Stub GitHubClient to resolve PR number 6 for the given SHA
    class FakeGH:
        def __init__(self, inst):
            self.inst = inst

        def list_prs_for_commit(self, owner, repo, sha):
            # Ensure the inputs match our payload
            assert owner == "bemcculley"
            assert repo == "auto-merge"
            assert sha == "ba819a5e7521c75b47438fe88e4cf22f7110d5ba"
            return [{"number": 6}]

    monkeypatch.setattr(mainmod, "GitHubClient", FakeGH)

    client = TestClient(app)

    payload = {
        "action": "completed",
        "check_suite": {
            "id": 48980747589,
            "node_id": "CS_kwDOQN_-gc8AAAALZ3rlRQ",
            "head_branch": "edgecase-checks",
            "head_sha": "ba819a5e7521c75b47438fe88e4cf22f7110d5ba",
            "status": "completed",
            "conclusion": "success",
            "url": "https://api.github.com/repos/bemcculley/auto-merge/check-suites/48980747589",
            "before": "fd000083207c658154883e1493ec53b7d52ce443",
            "after": "ba819a5e7521c75b47438fe88e4cf22f7110d5ba",
            "pull_requests": [
                {
                    "url": "https://api.github.com/repos/bemcculley/auto-merge/pulls/6",
                    "id": 2971221270,
                    "number": 6,
                    "head": {
                        "ref": "edgecase-checks",
                        "sha": "ba819a5e7521c75b47438fe88e4cf22f7110d5ba",
                        "repo": {
                            "id": 1088421505,
                            "url": "https://api.github.com/repos/bemcculley/auto-merge",
                            "name": "auto-merge",
                        },
                    },
                    "base": {
                        "ref": "main",
                        "sha": "1f208ea6c4ad0593c64335e51e1a1d7fb91f2db4",
                        "repo": {
                            "id": 1088421505,
                            "url": "https://api.github.com/repos/bemcculley/auto-merge",
                            "name": "auto-merge",
                        },
                    },
                }
            ],
            "app": {
                "id": 15368,
                "client_id": "Iv1.05c79e9ad1f6bdfa",
                "slug": "github-actions",
                "node_id": "MDM6QXBwMTUzNjg=",
                "owner": {"login": "github", "id": 9919},
                "name": "GitHub Actions",
                "description": "Automate your workflow from idea to production",
                "external_url": "https://help.github.com/en/actions",
                "html_url": "https://github.com/apps/github-actions",
                "created_at": "2018-07-30T09:30:17Z",
                "updated_at": "2025-03-07T16:35:00Z",
                "permissions": {"checks": "write"},
                "events": [
                    "check_suite",
                    "status",
                    "pull_request",
                ],
            },
            "created_at": "2025-11-03T05:26:35Z",
            "updated_at": "2025-11-03T05:27:05Z",
            "rerequestable": True,
            "runs_rerequestable": False,
            "latest_check_runs_count": 1,
            "check_runs_url": "https://api.github.com/repos/bemcculley/auto-merge/check-suites/48980747589/check-runs",
            "head_commit": {
                "id": "ba819a5e7521c75b47438fe88e4cf22f7110d5ba",
                "tree_id": "b9318418cd2c1e5b1380a5911019511804facd43",
                "message": "fix: move import",
                "timestamp": "2025-11-03T05:26:23Z",
                "author": {"name": "Brian McCulley", "email": "brian.mcculley@gmail.com"},
                "committer": {"name": "Brian McCulley", "email": "brian.mcculley@gmail.com"},
            },
        },
        "repository": {
            "id": 1088421505,
            "node_id": "R_kgDOQN_-gQ",
            "name": "auto-merge",
            "full_name": "bemcculley/auto-merge",
            "private": False,
            "owner": {"login": "bemcculley", "id": 1653908},
            "html_url": "https://github.com/bemcculley/auto-merge",
            "url": "https://api.github.com/repos/bemcculley/auto-merge",
            "default_branch": "main",
        },
        "sender": {
            "login": "bemcculley",
            "id": 1653908,
            "url": "https://api.github.com/users/bemcculley",
        },
        "installation": {"id": 92786189, "node_id": "MDIzOkludGVncmF0aW9uSW5zdGFsbGF0aW9uOTI3ODYxODk="},
    }

    body = json.dumps(payload).encode("utf-8")
    # Use provided secret or default to test-secret
    secret = os.getenv("WEBHOOK_SECRET", "test-secret")
    sig = sign(secret, body)

    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "check_suite",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Delivery": "intg-1",
        },
    )

    assert resp.status_code == 202
    # Validate that the PR was enqueued with the expected identity
    assert calls == [
        (92786189, "bemcculley", "auto-merge", 6, "bemcculley"),
    ]
