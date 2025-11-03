from app.worker import process_item


class GHBase:
    def __init__(self):
        self.calls = []

    # Config: default requires checks
    def load_repo_file(self, owner, repo, path):
        return None

    def merge_pr(self, owner, repo, number, method, title, body):
        self.calls.append(("merge", method))
        return True, "merged"


class GHSkippedOnly(GHBase):
    def __init__(self):
        super().__init__()
        self.pr = {
            "number": 21,
            "labels": [{"name": "automerge"}],
            "mergeable_state": "clean",
            "mergeable": True,
            "head": {"sha": "skipped", "ref": "feat-skipped"},
            "base": {"ref": "main"},
            "title": "feat: handle skipped checks",
            "user": {"login": "dev"},
        }

    def get_pr(self, owner, repo, number):
        return self.pr

    def get_combined_status(self, owner, repo, sha):
        # Combined status is success (e.g., legacy statuses all green)
        return {"state": "success", "statuses": [{"context": "ci", "state": "success"}]}

    def list_check_suites(self, owner, repo, sha):
        # All suites are skipped (should be treated as neutral/ignored)
        return [
            {"conclusion": "skipped"},
            {"conclusion": "skipped"},
        ]


def test_merge_allows_when_check_suites_are_skipped():
    gh = GHSkippedOnly()
    ok, msg = process_item(gh, "octo", "repo", 21)
    assert ok is True
    assert any(c[0] == "merge" for c in gh.calls)


class GHSkippedAndFailure(GHBase):
    def __init__(self):
        super().__init__()
        self.pr = {
            "number": 22,
            "labels": [{"name": "automerge"}],
            "mergeable_state": "clean",
            "mergeable": True,
            "head": {"sha": "mix", "ref": "feat-mix"},
            "base": {"ref": "main"},
            "title": "feat: mixed results",
            "user": {"login": "dev"},
        }

    def load_repo_file(self, owner, repo, path):
        # Speed up wait loop if entered
        return "max_wait_minutes: 0\npoll_interval_seconds: 0"

    def get_pr(self, owner, repo, number):
        return self.pr

    def get_combined_status(self, owner, repo, sha):
        # Combined status reports success, but a suite has failed → must block
        return {"state": "success", "statuses": [{"context": "ci", "state": "success"}]}

    def list_check_suites(self, owner, repo, sha):
        return [
            {"conclusion": "skipped"},
            {"conclusion": "failure"},
        ]


def test_merge_blocks_when_any_suite_failed_even_if_some_skipped():
    gh = GHSkippedAndFailure()
    ok, msg = process_item(gh, "octo", "repo", 22)
    assert ok is False
    # Reason will include checks_not_green or a follow-on not_mergeable_after_checks
    assert isinstance(msg, str)
