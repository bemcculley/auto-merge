import json
import fakeredis
import pytest

from app.queue import Queue


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("REDIS_NAMESPACE", "test-automerge")


def test_enqueue_dedupe_and_fifo(monkeypatch):
    fr = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("redis.Redis.from_url", lambda url, decode_responses=True: fr)

    q = Queue()
    inst, owner, repo = 1, "octo", "repo"

    # Enqueue same PR twice -> second is deduped
    assert q.enqueue(inst, owner, repo, 5, sender="u1") is True
    assert q.enqueue(inst, owner, repo, 5, sender="u1") is False
    # Enqueue another -> should be at tail
    assert q.enqueue(inst, owner, repo, 7, sender="u2") is True

    # Pop should return 5 first, then 7
    item1 = q.pop(inst, owner, repo)
    item2 = q.pop(inst, owner, repo)
    assert item1["number"] == 5
    assert item2["number"] == 7

    # Queue now empty
    assert q.pop(inst, owner, repo) is None


def test_queue_gauges_update(monkeypatch):
    fr = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("redis.Redis.from_url", lambda url, decode_responses=True: fr)

    q = Queue()
    inst, owner, repo = 2, "octo", "repo"

    q.enqueue(inst, owner, repo, 1)
    # check meta/gauges manipulated without raising exceptions
    q.update_gauges(inst, owner, repo)

    # Ensure the stored data is valid JSON
    data = fr.lindex(f"test-automerge:queue:{inst}:{owner}/{repo}", 0)
    json.loads(data)
