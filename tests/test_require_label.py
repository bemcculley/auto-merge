from app.worker import process_item


class GHBase:
    def __init__(self):
        self.calls = []

    # stubs required by worker
    def load_repo_file(self, owner, repo, path):
        return None

    def get_combined_status(self, owner, repo, sha):
        return {"state": "success", "statuses": []}

    def list_check_suites(self, owner, repo, sha):
        return []


class GHNoLabelButAllowed(GHBase):
    def __init__(self):
        super().__init__()
        self.pr = {
            "number": 20,
            "labels": [],  # no automerge label
            "mergeable_state": "clean",
            "mergeable": True,
            "head": {"sha": "xyz", "ref": "feature"},
            "base": {"ref": "main"},
            "title": "feat: merge without label",
            "user": {"login": "dev"},
        }

    def load_repo_file(self, owner, repo, path):
        # Disable label requirement explicitly
        return "require_label: false"

    def get_pr(self, owner, repo, number):
        return self.pr

    def list_check_suites(self, owner, repo, sha):
        return [{"conclusion": "success"}]

    def merge_pr(self, owner, repo, number, method, title, body):
        self.calls.append(("merge", method))
        return True, "merged"


def test_merges_when_label_not_required(monkeypatch):
    # Ensure no sleeping if logic changes
    monkeypatch.setattr("time.sleep", lambda s: None)
    gh = GHNoLabelButAllowed()
    ok, msg = process_item(gh, "octo", "repo", 20)
    assert ok is True
    assert any(c[0] == "merge" for c in gh.calls)
