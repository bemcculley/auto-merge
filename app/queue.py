import json
import time
from typing import Optional, Tuple, List
import redis

from .config import SETTINGS
from .metrics import (
    events_enqueued_total,
    events_deduped_total,
    queue_push_failures_total,
    queue_pop_total,
    queue_pop_empty_total,
    queue_depth,
    queue_oldest_age_seconds,
    redis_latency_seconds,
    worker_lock_acquired_total,
    worker_lock_failed_total,
    worker_lock_lost_total,
    worker_active,
    backpressure_active,
)


class Queue:
    def __init__(self):
        self.r = redis.Redis.from_url(SETTINGS.redis_url, decode_responses=True)

    def _keys(self, installation_id: int, owner: str, repo: str) -> Tuple[str, str, str, str]:
        q = SETTINGS.redis_key("queue", str(installation_id), f"{owner}/{repo}")
        de = SETTINGS.redis_key("dedupe", str(installation_id), f"{owner}/{repo}")
        lock = SETTINGS.redis_key("lock", str(installation_id), f"{owner}/{repo}")
        meta = SETTINGS.redis_key("meta", str(installation_id), f"{owner}/{repo}")
        return q, de, lock, meta

    # --- Rate limit backpressure (per installation) ---
    def throttle_key(self, installation_id: int) -> str:
        return SETTINGS.redis_key("throttle", str(installation_id))

    def set_throttle(self, installation_id: int, until_epoch: float, reason: str = "rate_limit") -> None:
        try:
            ttl = max(1, int(until_epoch - time.time()))
            self.r.set(self.throttle_key(installation_id), json.dumps({"until": until_epoch, "reason": reason}), ex=ttl)
            backpressure_active.labels(installation=str(installation_id)).set(1)
        except Exception:
            pass

    def get_throttle(self, installation_id: int) -> Optional[dict]:
        try:
            raw = self.r.get(self.throttle_key(installation_id))
            if not raw:
                backpressure_active.labels(installation=str(installation_id)).set(0)
                return None
            data = json.loads(raw)
            backpressure_active.labels(installation=str(installation_id)).set(1)
            return data
        except Exception:
            return None

    def clear_throttle(self, installation_id: int) -> None:
        try:
            self.r.delete(self.throttle_key(installation_id))
        except Exception:
            pass
        finally:
            backpressure_active.labels(installation=str(installation_id)).set(0)

    def enqueue(self, installation_id: int, owner: str, repo: str, number: int, sender: Optional[str] = None) -> bool:
        q, de, _, _ = self._keys(installation_id, owner, repo)
        item_key = f"{number}"
        # Deduplicate
        if self.r.sismember(de, item_key):
            events_deduped_total.labels(owner=owner, repo=repo).inc()
            return False
        payload = {
            "number": number,
            "sender": sender,
            "ts": time.time(),
        }
        data = json.dumps(payload)
        t0 = time.perf_counter()
        try:
            pipe = self.r.pipeline()
            pipe.rpush(q, data)
            pipe.sadd(de, item_key)
            # Store first enqueue timestamp if queue was empty
            pipe.hsetnx(q + ":meta", "first_ts", str(payload["ts"]))
            pipe.execute()
        except Exception:
            queue_push_failures_total.labels(owner=owner, repo=repo).inc()
            return False
        finally:
            redis_latency_seconds.labels(op="enqueue").observe(time.perf_counter() - t0)
        events_enqueued_total.labels(owner=owner, repo=repo).inc()
        # Update gauges
        self.update_gauges(installation_id, owner, repo)
        return True

    def pop(self, installation_id: int, owner: str, repo: str) -> Optional[dict]:
        q, de, _, _ = self._keys(installation_id, owner, repo)
        t0 = time.perf_counter()
        try:
            data = self.r.lpop(q)
        finally:
            redis_latency_seconds.labels(op="lpop").observe(time.perf_counter() - t0)
        if data is None:
            queue_pop_empty_total.labels(owner=owner, repo=repo).inc()
            # Clear oldest age
            self._maybe_clear_oldest_meta(q)
            self.update_gauges(installation_id, owner, repo)
            return None
        queue_pop_total.labels(owner=owner, repo=repo).inc()
        item = json.loads(data)
        # Remove from dedupe set
        try:
            self.r.srem(SETTINGS.redis_key("dedupe", str(installation_id), f"{owner}/{repo}"), str(item["number"]))
        except Exception:
            pass
        # Update gauges/age
        self.update_gauges(installation_id, owner, repo)
        return item

    def update_gauges(self, installation_id: int, owner: str, repo: str) -> None:
        q, _, _, _ = self._keys(installation_id, owner, repo)
        try:
            depth = self.r.llen(q)
            queue_depth.labels(owner=owner, repo=repo).set(depth)
            if depth > 0:
                # Peek oldest to compute age
                first_raw = self.r.lindex(q, 0)
                first_ts = None
                if first_raw:
                    try:
                        first_ts = json.loads(first_raw).get("ts")
                    except Exception:
                        first_ts = None
                age = max(0.0, time.time() - first_ts) if first_ts else 0.0
                queue_oldest_age_seconds.labels(owner=owner, repo=repo).set(age)
                # Also refresh meta
                self.r.hset(q + ":meta", "first_ts", str(first_ts or time.time()))
            else:
                self._maybe_clear_oldest_meta(q)
                queue_oldest_age_seconds.labels(owner=owner, repo=repo).set(0)
        except Exception:
            # Do not raise on metrics update
            pass

    def _maybe_clear_oldest_meta(self, qkey: str) -> None:
        try:
            self.r.hdel(qkey + ":meta", "first_ts")
        except Exception:
            pass

    # --- Lock management ---
    def acquire_lock(self, installation_id: int, owner: str, repo: str, worker_id: str) -> bool:
        _, _, lock, _ = self._keys(installation_id, owner, repo)
        ttl = SETTINGS.redis_lock_ttl_seconds
        t0 = time.perf_counter()
        try:
            ok = self.r.set(lock, worker_id, nx=True, ex=ttl)
        finally:
            redis_latency_seconds.labels(op="acquire_lock").observe(time.perf_counter() - t0)
        if ok:
            worker_lock_acquired_total.labels(owner=owner, repo=repo).inc()
            worker_active.labels(owner=owner, repo=repo).set(1)
            return True
        worker_lock_failed_total.labels(owner=owner, repo=repo).inc()
        return False

    def refresh_lock(self, installation_id: int, owner: str, repo: str, worker_id: str) -> bool:
        _, _, lock, _ = self._keys(installation_id, owner, repo)
        # Lua script to refresh lock only if owned by worker_id
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('expire', KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        t0 = time.perf_counter()
        try:
            res = self.r.eval(script, 1, lock, worker_id, SETTINGS.redis_lock_ttl_seconds)
        finally:
            redis_latency_seconds.labels(op="refresh_lock").observe(time.perf_counter() - t0)
        return bool(res)

    def release_lock(self, installation_id: int, owner: str, repo: str, worker_id: str) -> None:
        _, _, lock, _ = self._keys(installation_id, owner, repo)
        # Lua: delete only if owned by worker_id
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        try:
            self.r.eval(script, 1, lock, worker_id)
        except Exception:
            pass
        finally:
            worker_active.labels(owner=owner, repo=repo).set(0)

    # Utility to list active repos (best-effort; optional for metrics sweeps)
    def list_active_repos(self) -> List[str]:
        try:
            pattern = SETTINGS.redis_key("queue", "*", "*")
            keys = self.r.keys(pattern)
            return [k.split(SETTINGS.redis_namespace + ":queue:")[-1] for k in keys]
        except Exception:
            return []
