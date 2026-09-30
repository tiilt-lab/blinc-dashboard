"""Persistence tests for the posthoc queue: a coordinator (re)start must pick
up pending jobs — from Redis (re-queuing a job that was mid-run when the
process died) and, once, from the JSON snapshot the single-process server
kept before the queue moved to Redis."""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# posthoc_state has no heavy deps, but stub it so this test stays hermetic.
sys.modules.setdefault('posthoc_state',
                       types.SimpleNamespace(is_running=lambda d: False))

from fake_redis import FakeRedis  # noqa: E402
import redis_sync  # noqa: E402
import posthoc_queue as q  # noqa: E402


def test_migration_requeues_running_and_retires_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(redis_sync, "_client", FakeRedis())
    state = tmp_path / "posthoc_queue.json"
    monkeypatch.setattr(q, "_state_file", lambda: str(state))

    jobs = [
        {"session_id": 1, "device_id": 10, "state": "done", "models": None,
         "error": None, "queued_at": 1.0, "started_at": 2.0, "finished_at": 3.0},
        {"session_id": 1, "device_id": 11, "state": "running", "models": None,
         "error": None, "queued_at": 1.0, "started_at": 2.0, "finished_at": None},
    ]
    state.write_text(json.dumps(jobs))

    assert q.migrate_state_file() == 1
    st = {j["device_id"]: j["state"] for j in q.status()}
    assert 10 not in st, "a settled job is not carried over"
    assert st[11] == "queued", "job caught mid-run must be re-queued"
    assert not state.exists() and (tmp_path / "posthoc_queue.json.migrated").exists()

    assert q.clear_pending() == 1
    assert q.status() == []


def test_restart_requeues_a_job_the_runner_was_on(monkeypatch):
    monkeypatch.setattr(redis_sync, "_client", FakeRedis())
    q.enqueue(1, [12])
    job = q._all_jobs()[0]
    job["state"] = "running"
    q._save(job)
    assert q._requeue_running() == 1
    assert q.status() == [{"session_id": 1, "device_id": 12, "state": "queued",
                           "error": None, "started_at": None, "finished_at": None}]
