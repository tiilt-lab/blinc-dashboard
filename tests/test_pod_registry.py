"""Tests for PodRegistry (src/common/pod_registry.py).

PodRegistry fuses the cross-process DistributedClaim with the local dict that
holds the live processing object — replacing the repeated
"if key in dict: reject; dict[key]='running'" + separate claim boilerplate that
was copy-pasted across four posthoc claim flows (audio full/P&I/E&T + video).

It is a drop-in for the old dict for the parts the processors use (pop/get/
keys/__contains__), but the CLAIM decision is the explicit atomic try_claim.
An injected fake redis (or None) keeps it unit-testable.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

from pod_registry import PodRegistry  # noqa: E402


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, owner, *args):
        # Owner-checked compare-and-delete / compare-and-expire (Phase 2).
        if self.store.get(key) != owner:
            return 0
        return self.delete(key) if "'del'" in script else 1

    def exists(self, key):
        return 1 if key in self.store else 0


def _reg():
    fake = _FakeRedis()
    return PodRegistry(redis_factory=lambda: fake, prefix="t:")


def test_try_claim_is_atomic_and_stores_object():
    r = _reg()
    assert r.try_claim("pod-1", "running") is True
    assert r.try_claim("pod-1") is False          # already running (any process)
    assert r.get("pod-1") == "running"
    assert "pod-1" in r


def test_pop_releases_claim_and_returns_object():
    r = _reg()
    r.try_claim("pod-1", "obj")
    assert r.pop("pod-1", None) == "obj"
    assert "pod-1" not in r
    assert r.try_claim("pod-1") is True            # reclaimable after pop


def test_pop_missing_returns_default():
    r = _reg()
    assert r.pop("nope", None) is None


def test_setitem_replaces_the_live_object_under_a_held_claim():
    r = _reg()
    r.try_claim("pod-1", "running")
    r["pod-1"] = "the-processor-object"            # store the real object later
    assert r.get("pod-1") == "the-processor-object"


def test_keys_snapshot():
    r = _reg()
    r.try_claim("a")
    r.try_claim("b")
    assert set(r.keys()) == {"a", "b"}


def test_distinct_pods_independent():
    r = _reg()
    assert r.try_claim("a") is True
    assert r.try_claim("b") is True


def test_len_and_truthiness_reflect_active_runs():
    r = _reg()
    assert not r and len(r) == 0          # empty -> falsy (idle-recycle check)
    r.try_claim("a")
    assert r and len(r) == 1
    r.pop("a")
    assert not r


def test_memory_fallback_when_no_redis():
    r = PodRegistry(redis_factory=None, prefix="t:")
    assert r.try_claim("a") is True
    assert r.try_claim("a") is False
    r.pop("a")
    assert r.try_claim("a") is True
