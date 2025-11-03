import hmac
import hashlib
import json
import os
import uuid
import asyncio
import time
import logging
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request, Response, Header, HTTPException

from .config import SETTINGS
from .metrics import (
    metrics_response,
    webhook_requests_total,
    webhook_invalid_signatures_total,
    webhook_parse_failures_total,
)
from .queue import Queue
from .github import GitHubClient
from .worker import process_item

logger = logging.getLogger(__name__)

app = FastAPI(title="Auto Merge Webhook Service", version=os.getenv("SERVICE_VERSION", "dev"))


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": SETTINGS.service_version}


@app.get("/readyz")
async def readyz():
    return {"status": "ready"}


@app.get("/metrics")
async def metrics():
    content_type, data = metrics_response()
    return Response(content=data, media_type=content_type)


def verify_signature(secret: str, body: bytes, signature256: Optional[str]) -> bool:
    if not signature256:
        return False
    try:
        algo, sig = signature256.split("=", 1)
        if algo != "sha256":
            return False
    except ValueError:
        return False
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    expected = mac.hexdigest()
    return hmac.compare_digest(expected, sig)


# Utility to extract PR identities from various events

def extract_pr_identities(event: str, payload: Dict[str, Any]) -> Optional[list[Dict[str, Any]]]:
    # pull_request events carry number & repo directly
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        repo = payload.get("repository") or {}
        inst = (payload.get("installation") or {}).get("id")
        if pr and repo and inst:
            owner = (repo.get("owner") or {}).get("login")
            return [{
                "installation_id": inst,
                "owner": owner,
                "repo": repo.get("name"),
                "number": pr.get("number"),
                "sender": (payload.get("sender") or {}).get("login"),
            }]
    # check_suite and status events: resolve PRs by commit SHA
    if event in ("check_suite", "status"):
        repo = payload.get("repository") or {}
        inst = (payload.get("installation") or {}).get("id")
        if not (repo and inst):
            return None
        owner = (repo.get("owner") or {}).get("login")
        reponame = repo.get("name")
        sha = None
        if event == "check_suite":
            sha = (payload.get("check_suite") or {}).get("head_sha")
        elif event == "status":
            sha = payload.get("sha")
        if not sha:
            return None
        # Query GitHub for PRs associated with this commit
        gh = GitHubClient(int(inst))
        prs = gh.list_prs_for_commit(owner, reponame, sha)
        results = []
        for pr in prs or []:
            num = pr.get("number") or (pr.get("pull_request") or {}).get("number")
            if not num:
                continue
            results.append({
                "installation_id": int(inst),
                "owner": owner,
                "repo": reponame,
                "number": int(num),
                "sender": (payload.get("sender") or {}).get("login"),
            })
        return results or None
    return None


async def _drain_repo(q: Queue, installation_id: int, owner: str, repo: str):
    worker_id = str(uuid.uuid4())
    logger.debug("Drain start for %s/%s (installation=%s, worker_id=%s)", owner, repo, installation_id, worker_id)
    if not q.acquire_lock(installation_id, owner, repo, worker_id):
        logger.debug("Drain skipped: failed to acquire lock for %s/%s", owner, repo)
        return
    try:
        # Respect rate-limit backpressure per installation
        throttle = q.get_throttle(installation_id)
        if throttle:
            try:
                until = float(throttle.get("until", 0))
            except Exception:
                until = 0.0
            now = time.time()
            if until > now:
                # Schedule a resume after cooldown, up to max_backoff_seconds
                delay = min(max(0.0, until - now), SETTINGS.max_backoff_seconds)
                if delay > 0:
                    logger.debug("Backpressure active; deferring drain for %ss (installation=%s)", delay, installation_id)
                    asyncio.create_task(asyncio.sleep(delay))
                    # Release lock and exit; a subsequent webhook or scheduled re-run will resume
                return
        # Drain until empty
        while True:
            item = q.pop(installation_id, owner, repo)
            if not item:
                logger.debug("Queue empty for %s/%s; stopping drain", owner, repo)
                break
            number = int(item.get("number"))
            logger.debug("Processing queued PR #%s for %s/%s", number, owner, repo)
            gh = GitHubClient(installation_id)
            ok, msg = process_item(gh, owner, repo, number)
            logger.debug("Result for PR #%s: ok=%s msg=%s", number, ok, msg)
            # Continue regardless; processing result is logged via metrics
            # Sleep briefly to avoid hot looping
            await asyncio.sleep(0)
            # Refresh lock periodically
            if not q.refresh_lock(installation_id, owner, repo, worker_id):
                # Lost the lock; stop to avoid double processing
                logger.debug("Lost lock while draining %s/%s; stopping", owner, repo)
                break
    finally:
        q.release_lock(installation_id, owner, repo, worker_id)
        logger.debug("Drain finished for %s/%s (worker_id=%s)", owner, repo, worker_id)


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    x_github_delivery: Optional[str] = Header(None, alias="X-GitHub-Delivery"),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    event = x_github_event or "unknown"
    action = "unknown"
    code = 200
    body = await request.body()

    # Verify signature
    if not verify_signature(SETTINGS.webhook_secret, body, x_hub_signature_256):
        webhook_invalid_signatures_total.inc()
        webhook_requests_total.labels(event=event, action=action, code=str(401)).inc()
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        webhook_parse_failures_total.labels(event=event).inc()
        webhook_requests_total.labels(event=event, action=action, code=str(400)).inc()
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = payload.get("action", "unknown")

    # Extract PR identities and enqueue
    identities = extract_pr_identities(event, payload)
    if not identities:
        # For now, ignore non-PR webhook types; 202 Accepted
        code = 202
        webhook_requests_total.labels(event=event, action=action, code=str(code)).inc()
        return Response(status_code=code)

    q = Queue()
    # Enqueue all identities (likely one for pull_request; possibly many for check_suite/status)
    # Track repos we touched to trigger one drain per repo
    touched = set()
    for identity in identities:
        installation_id = int(identity["installation_id"])  # type: ignore
        owner = identity["owner"]
        repo = identity["repo"]
        number = int(identity["number"])  # type: ignore
        sender = identity.get("sender")
        q.enqueue(installation_id, owner, repo, number, sender)
        touched.add((installation_id, owner, repo))

    # Trigger background drain per repo
    for installation_id, owner, repo in touched:
        asyncio.create_task(_drain_repo(q, installation_id, owner, repo))

    code = 202
    webhook_requests_total.labels(event=event, action=action, code=str(code)).inc()
    return Response(status_code=code)
