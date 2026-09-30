"""Tests for the cross-process claim primitive
(src/common/distributed_claim.py, architecture fix #6).

"Is this pod already being processed?" was an in-process dict + threading.Lock
— it coordinates threads in ONE process, not a second instance. This primitive
answers it across processes via Redis SET NX EX, with an in-memory fallback
(== today's per-process behavior) when Redis is down, so adopting it can't
regress. Both paths are exercised with an injected fake Redis (CI has none).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

from distributed_claim import DistributedClaim  # noqa: E402


class _FakeRedis:
    """Minimal SET NX EX / DELETE / EXISTS with a raise switch."""
    def __init__(self):
        self.store = {}
        self.fail = False

    def set(self, key, val, nx=False, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return None            # not set (already held) — redis-py returns None
        self.store[key] = val
        return True

    def delete(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return 1 if self.store.pop(key, None) is not None else 0

    def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    def eval(self, script, numkeys, key, owner, *args):
        # Owner-checked compare-and-delete / compare-and-expire (Phase 2).
        if self.fail:
            raise RuntimeError("redis down")
        if self.store.get(key) != owner:
            return 0
        if "'del'" in script:
            return self.delete(key)
        return 1

    def exists(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return 1 if key in self.store else 0


def _redis_claim():
    fake = _FakeRedis()
    return DistributedClaim(redis_factory=lambda: fake), fake


def _mem_claim():
    return DistributedClaim(redis_factory=None)


# ---- Redis-backed path -----------------------------------------------------

def test_first_claim_wins_second_loses():
    c, _ = _redis_claim()
    assert c.try_claim("pod-1") is True
    assert c.try_claim("pod-1") is False       # already held cross-process


def test_release_frees_the_claim():
    c, _ = _redis_claim()
    assert c.try_claim("pod-1") is True
    c.release("pod-1")
    assert c.try_claim("pod-1") is True         # reclaimable after release


def test_is_claimed_reflects_state():
    c, _ = _redis_claim()
    assert c.is_claimed("pod-1") is False
    c.try_claim("pod-1")
    assert c.is_claimed("pod-1") is True
    c.release("pod-1")
    assert c.is_claimed("pod-1") is False


def test_distinct_keys_are_independent():
    c, _ = _redis_claim()
    assert c.try_claim("pod-1") is True
    assert c.try_claim("pod-2") is True


def test_ttl_is_passed_to_redis():
    c, fake = _redis_claim()
    captured = {}
    orig = fake.set
    fake.set = lambda k, v, nx=False, ex=None: (captured.update(ex=ex) or orig(k, v, nx=nx, ex=ex))
    c.try_claim("pod-1", ttl=1234)
    assert captured["ex"] == 1234               # self-healing bound on a missed release


# ---- in-memory fallback ----------------------------------------------------

def test_memory_fallback_same_semantics():
    c = _mem_claim()
    assert c.try_claim("pod-1") is True
    assert c.try_claim("pod-1") is False
    c.release("pod-1")
    assert c.try_claim("pod-1") is True


def test_redis_failure_degrades_to_memory_not_crash():
    fake = _FakeRedis()
    fake.fail = True
    c = DistributedClaim(redis_factory=lambda: fake)
    # Must not raise; falls back to in-memory and still enforces the claim.
    assert c.try_claim("pod-1") is True
    assert c.try_claim("pod-1") is False


def test_redis_factory_raising_is_tolerated():
    def boom():
        raise RuntimeError("no redis configured")
    c = DistributedClaim(redis_factory=boom)
    assert c.try_claim("pod-1") is True         # memory fallback
    assert c.try_claim("pod-1") is False
