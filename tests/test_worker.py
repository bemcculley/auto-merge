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

    # Ensure checks are considered green under the default config (which requires checks),
    # so the worker does not enter the wait loop in tests.
    def list_check_suites(self, owner, repo, sha):
        return [{"conclusion": "success"}]

    def merge_pr(self, owner, repo, number, method, title, body):
        self.calls.append(("merge", method))
        return True, "merged"


def test_worker_process_item_success(monkeypatch):
    # Avoid any accidental sleeps if the worker path changes in future
    monkeypatch.setattr("time.sleep", lambda s: None)
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

    def get_combined_status(self, owner, repo, sha):
        # Report green combined status with at least one status to avoid the no-checks branch
        return {"state": "success", "statuses": [{"context": "ci", "state": "success"}]}

    def list_check_suites(self, owner, repo, sha):
        # Report successful check suite to satisfy are_checks_green immediately
        return [{"conclusion": "success"}]

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


class GHNoChecks(GHBase):
    def __init__(self):
        super().__init__()
        self.pr = {
            "number": 12,
            "labels": [{"name": "automerge"}],
            "mergeable_state": "clean",
            "mergeable": True,
            "head": {"sha": "nochk", "ref": "feat3"},
            "base": {"ref": "main"},
            "title": "docs: no checks",
            "user": {"login": "dev"},
        }

    def load_repo_file(self, owner, repo, path):
        # Enable override to allow merging when no checks exist
        return "allow_merge_when_no_checks: true"

    def get_pr(self, owner, repo, number):
        return self.pr

    def get_combined_status(self, owner, repo, sha):
        # Simulate no statuses (GitHub returns an array); also state might be 'pending'
        return {"state": "pending", "statuses": []}

    def list_check_suites(self, owner, repo, sha):
        # No suites
        return []

    def merge_pr(self, owner, repo, number, method, title, body):
        self.calls.append(("merge", method))
        return True, "merged"


def test_worker_merges_when_no_checks_allowed():
    gh = GHNoChecks()
    ok, msg = process_item(gh, "octo", "repo", 12)
    assert ok is True
    assert any(c[0] == "merge" for c in gh.calls)


class GHRaceChecks(GHBase):
    def __init__(self):
        super().__init__()
        self.pr = {
            "number": 13,
            "labels": [{"name": "automerge"}],
            "mergeable_state": "clean",
            "mergeable": True,
            "head": {"sha": "race", "ref": "feat4"},
            "base": {"ref": "main"},
            "title": "feat: race handling",
            "user": {"login": "dev"},
        }
        self.calls = []
        self._status_calls = 0

    def load_repo_file(self, owner, repo, path):
        # Default config: require checks; polling fast
        return "poll_interval_seconds: 0\nmax_wait_minutes: 1"

    def get_pr(self, owner, repo, number):
        return self.pr

    def get_combined_status(self, owner, repo, sha):
        # First call: no checks yet (pending + no statuses). Second call: success
        self._status_calls += 1
        if self._status_calls == 1:
            return {"state": "pending", "statuses": []}
        return {"state": "success", "statuses": [{"context": "ci", "state": "success"}]}

    def list_check_suites(self, owner, repo, sha):
        # Assume suites appear successful on the second poll as well
        if self._status_calls >= 2:
            return [{"conclusion": "success"}]
        return []

    def merge_pr(self, owner, repo, number, method, title, body):
        self.calls.append(("merge", method))
        return True, "merged"


def test_worker_waits_for_checks_and_merges(monkeypatch):
    # Avoid real sleeping to keep test fast
    monkeypatch.setattr("time.sleep", lambda s: None)
    gh = GHRaceChecks()
    ok, msg = process_item(gh, "octo", "repo", 13)
    assert ok is True
    assert any(c[0] == "merge" for c in gh.calls)
