import time
import logging
from typing import Tuple

from .github import GitHubClient
from .models import Config
from .metrics import (
    worker_processing_seconds,
    branch_updates_total,
    checks_wait_seconds,
    merge_attempts_total,
    merges_success_total,
    merges_failed_total,
)

logger = logging.getLogger(__name__)


def parse_simple_yaml(text: str) -> dict:
    cfg = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if v.lower() in ("true", "false"):
            cfg[k] = v.lower() == "true"
        else:
            try:
                cfg[k] = int(v)
            except ValueError:
                try:
                    cfg[k] = float(v)
                except ValueError:
                    cfg[k] = v
    return cfg


def load_config(gh: GitHubClient, owner: str, repo: str) -> Config:
    # Read .github/automerge.yml or .yaml
    content = gh.load_repo_file(owner, repo, ".github/automerge.yml") or gh.load_repo_file(owner, repo, ".github/automerge.yaml")
    user = {}
    if content:
        try:
            user = parse_simple_yaml(content)
        except Exception:
            user = {}
    # Build Config with defaults
    cfg = Config(**{k: v for k, v in user.items() if k in Config.model_fields})
    return cfg


def are_checks_green(gh: GitHubClient, owner: str, repo: str, sha: str, cfg: Config) -> bool:
    combined = gh.get_combined_status(owner, repo, sha)
    suites = gh.list_check_suites(owner, repo, sha)
    statuses = combined.get("statuses") or []
    # If there are no statuses and no check suites, allow merge when configured
    if not statuses and not suites:
        logger.debug("No statuses and no check suites for %s/%s@%s; allow_merge_when_no_checks=%s", owner, repo, sha, cfg.allow_merge_when_no_checks)
        return bool(cfg.allow_merge_when_no_checks)
    # Otherwise require green/neutral across combined status and suites
    state = combined.get("state")
    if state not in ("success", "neutral"):
        logger.debug("Combined status not green: state=%s for %s/%s@%s", state, owner, repo, sha)
        return False
    for s in suites:
        concl = s.get("conclusion")
        if concl not in ("success", "neutral"):
            logger.debug("Check suite not green: conclusion=%s for %s/%s@%s", concl, owner, repo, sha)
            return False
    return True


def evaluate_mergeability(gh: GitHubClient, owner: str, repo: str, number: int, cfg: Config) -> Tuple[bool, str, dict]:
    pr = gh.get_pr(owner, repo, number)
    if not pr:
        return False, "failed_to_fetch", {}
    label = cfg.label
    if pr.get("draft"):
        return False, "draft", pr
    if pr.get("locked"):
        return False, "locked", pr
    if not any(l["name"] == label for l in pr.get("labels", [])):
        return False, "missing_label", pr

    mergeable_state = pr.get("mergeable_state")  # clean, unstable, blocked, behind, dirty, unknown
    head_sha = pr.get("head", {}).get("sha")

    # Up-to-date requirement
    if cfg.require_up_to_date and mergeable_state in ("behind", "blocked"):
        return False, f"behind_or_blocked:{mergeable_state}", pr

    # Checks must be green
    if not are_checks_green(gh, owner, repo, head_sha, cfg):
        logger.debug("Checks not green for %s/%s PR #%s (sha=%s)", owner, repo, number, head_sha)
        return False, "checks_not_green", pr

    if pr.get("mergeable") is False:
        return False, f"mergeable_false:{mergeable_state}", pr

    return True, "mergeable", pr


def wait_for_checks(gh: GitHubClient, owner: str, repo: str, sha: str, cfg: Config) -> bool:
    deadline = time.time() + cfg.max_wait_minutes * 60
    while time.time() < deadline:
        if are_checks_green(gh, owner, repo, sha, cfg):
            return True
        time.sleep(max(5, cfg.poll_interval_seconds))
    return False


def process_item(gh: GitHubClient, owner: str, repo: str, number: int) -> Tuple[bool, str]:
    logger.debug("Loading config for %s/%s", owner, repo)
    cfg = load_config(gh, owner, repo)
    logger.debug("Evaluating PR #%s for %s/%s with cfg=%s", number, owner, repo, cfg.model_dump())
    # Evaluate
    with worker_processing_seconds.labels(phase="evaluate", owner=owner, repo=repo).time():
        ok, reason, pr = evaluate_mergeability(gh, owner, repo, number, cfg)
    if not ok:
        logger.debug("PR #%s not mergeable initially: reason=%s", number, reason)
        # If behind and updates allowed, try to update then wait and re-evaluate
        if cfg.update_branch and pr and pr.get("mergeable_state") in ("behind",):
            logger.debug("Attempting update-branch for PR #%s", number)
            with worker_processing_seconds.labels(phase="update_branch", owner=owner, repo=repo).time():
                updated = gh.update_branch(owner, repo, number)
            branch_updates_total.labels(result="success" if updated else "fail").inc()
            logger.debug("Update-branch result for PR #%s: %s", number, updated)
            if not updated:
                return False, f"update_branch_failed:{reason}"
            # Wait for checks
            head_sha = pr.get("head", {}).get("sha")
            with checks_wait_seconds.time():
                ok_checks = wait_for_checks(gh, owner, repo, head_sha, cfg)
            if not ok_checks:
                logger.debug("Checks timeout after update for PR #%s", number)
                return False, "checks_timeout"
            # Re-evaluate
            logger.debug("Re-evaluating PR #%s after branch update", number)
            with worker_processing_seconds.labels(phase="evaluate", owner=owner, repo=repo).time():
                ok, reason, pr = evaluate_mergeability(gh, owner, repo, number, cfg)
                if not ok:
                    logger.debug("PR #%s still not mergeable after update: %s", number, reason)
                    return False, f"not_mergeable_after_update:{reason}"
        else:
            return False, reason

    # Merge now
    method = cfg.merge_method
    title = cfg.title_template.format(
        number=number,
        title=pr.get("title", f"PR #{number}"),
        head=pr.get("head", {}).get("ref"),
        base=pr.get("base", {}).get("ref"),
        user=(pr.get("user") or {}).get("login"),
    )
    body = cfg.body_template.format(
        number=number,
        body=pr.get("body") or "",
        title=pr.get("title", f"PR #{number}"),
        head=pr.get("head", {}).get("ref"),
        base=pr.get("base", {}).get("ref"),
        user=(pr.get("user") or {}).get("login"),
    )
    logger.debug("Merging PR #%s for %s/%s with method=%s", number, owner, repo, method)
    with worker_processing_seconds.labels(phase="merge", owner=owner, repo=repo).time():
        ok, msg = gh.merge_pr(owner, repo, number, method, title, body)
    merge_attempts_total.labels(method=method, result="success" if ok else "error").inc()
    if ok:
        logger.debug("Merge success for PR #%s: %s", number, msg)
        merges_success_total.labels(method=method).inc()
    else:
        logger.debug("Merge failure for PR #%s: %s", number, msg)
        merges_failed_total.labels(reason="merge_api_error").inc()
    return ok, msg
