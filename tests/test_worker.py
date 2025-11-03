from app.worker import process_item
from app.models import Config


class GHBase:
    def __init__(self):
        self.calls = []

    # stubs required by worker
    def load_repo_file(self, owner, repo, path):
        # no user config; defaults used
        return None

    def get_combined_status(self, owner, repo, sha):
        return {"state": "success", "statuses": []}

    def list_check_suites(self, owner, repo, sha):
        return []


class GHSuccess(GHBase):
    def __init__(self):
        super().__init__()
        self.pr = {
            "number": 10,
            "labels": [{"name": "automerge"}],
            "mergeable_state": "clean",
            "mergeable": True,
            "head": {"sha": "abc", "ref": "feature"},
            "base": {"ref": "main"},
            "title": "feat: change",
            "user": {"login": "dev"},
        }

    def get_pr(self, owner, repo, number):
        self.calls.append(("get_pr", number))
        return self.pr

    def merge_pr(self, owner, repo, number, method, title, body):
        self.calls.append(("merge", method))
        return True, "merged"


def test_worker_process_item_success():
    gh = GHSuccess()
    ok, msg = process_item(gh, "octo", "repo", 10)
    assert ok is True
    assert "merged" in msg
    assert ("merge", "squash") in gh.calls


class GHBehindThenUpdate(GHBase):
    def __init__(self):
        super().__init__()
        self.state = "behind"  # initial
        self.pr = {
            "number": 11,
            "labels": [{"name": "automerge"}],
            "mergeable_state": self.state,
            "mergeable": True,
            "head": {"sha": "def", "ref": "feat2"},
            "base": {"ref": "main"},
            "title": "fix: thing",
            "user": {"login": "dev"},
        }

    def get_pr(self, owner, repo, number):
        # After update, flip to clean
        self.pr["mergeable_state"] = self.state
        self.state = "clean"
        return self.pr

    def update_branch(self, owner, repo, number):
        self.calls.append(("update_branch", number))
        return True

    def merge_pr(self, owner, repo, number, method, title, body):
        self.calls.append(("merge", method))
        return True, "merged"


def test_worker_process_item_behind_then_update_merge(monkeypatch):
    # Speed up wait_for_checks loop by forcing immediate success via base class methods
    gh = GHBehindThenUpdate()
    ok, msg = process_item(gh, "octo", "repo", 11)
    assert ok is True
    assert ("update_branch", 11) in gh.calls
    assert any(c[0] == "merge" for c in gh.calls)
