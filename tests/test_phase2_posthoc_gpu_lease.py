"""GPU admission lease, live-class deferral and pre-emption
(src/common/gpu_lease.py; audit C.1, Phase 2 item 14 post-hoc half).

Contract with the live workstream: ``live_pod:<key>`` keys (60 s TTL) exist
while a pod streams; ``gpu_lease:<slot>`` = owner token with a TTL while a
post-hoc run holds the GPU. A fake Redis with a manual clock stands in for the
server (CI has none).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

import gpu_lease as gl  # noqa: E402
from distributed_claim import CAD_SCRIPT, CAE_SCRIPT  # noqa: E402


class FakeRedis:
    """SET NX EX / GET / DEL / EVAL (the two owner scripts) / SCAN, manual clock."""

    def __init__(self):
        self.store = {}   # key -> (value, expires_at | None)
        self.now = 1000.0
        self.fail = False
        self.renews = 0

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def _live(self, key):
        item = self.store.get(key)
        if item is None:
            return None
        if item[1] is not None and self.now >= item[1]:
            del self.store[key]
            return None
        return item

    def set(self, key, val, nx=False, ex=None):
        self._check()
        if nx and self._live(key) is not None:
            return None
        self.store[key] = (val, None if ex is None else self.now + ex)
        return True

    def get(self, key):
        self._check()
        item = self._live(key)
        return None if item is None else item[0]

    def delete(self, key):
        self._check()
        return 1 if self.store.pop(key, None) is not None else 0

    def expires_at(self, key):
        return self.store[key][1]

    def eval(self, script, numkeys, key, owner, *args):
        self._check()
        if self.get(key) != owner:
            return 0
        if script == CAD_SCRIPT:
            return self.delete(key)
        if script == CAE_SCRIPT:
            self.renews += 1
            self.store[key] = (owner, self.now + int(args[0]))
            return 1
        raise AssertionError("unknown script")

    def scan_iter(self, match="*", count=None):
        self._check()
        prefix = match.rstrip("*")
        for k in list(self.store):
            if k.startswith(prefix) and self._live(k) is not None:
                yield k


# ---- the lease --------------------------------------------------------------

def test_acquire_sets_owner_token_with_ttl_on_slot_zero():
    r = FakeRedis()
    lease = gl.GpuLease(r, "audio-posthoc:1:pod-a")
    assert lease.try_acquire() is True
    assert r.store["gpu_lease:0"] == ("audio-posthoc:1:pod-a", r.now + 180)
    assert lease.holder() == "audio-posthoc:1:pod-a"
    assert lease.try_acquire() is True, "re-acquire by the holder is idempotent"


def test_one_slot_by_default_second_run_waits_for_release():
    r = FakeRedis()
    a, b = gl.GpuLease(r, "a"), gl.GpuLease(r, "b")
    assert a.try_acquire() and not b.try_acquire()
    assert a.release() is True
    assert b.try_acquire() is True
    assert a.release() is False, "nothing left to release"


def test_crashed_holder_frees_the_gpu_when_the_ttl_lapses():
    r = FakeRedis()
    a, b = gl.GpuLease(r, "a", ttl=180), gl.GpuLease(r, "b")
    assert a.try_acquire()
    r.now += 181          # a died without releasing
    assert b.try_acquire() is True


def test_heartbeat_renews_only_while_still_the_owner_and_release_never_drops_a_successor():
    r = FakeRedis()
    a, b = gl.GpuLease(r, "a", ttl=180), gl.GpuLease(r, "b")
    assert a.try_acquire()
    r.now += 100
    assert a.heartbeat() is True
    assert r.expires_at("gpu_lease:0") == r.now + 180
    r.now += 181          # a's lease lapsed, b takes the slot
    assert b.try_acquire()
    assert a.heartbeat() is False
    assert a.release() is False
    assert b.holder() == "b", "compare-and-delete left b's lease alone"


def test_multiple_slots():
    r = FakeRedis()
    a, b, c = (gl.GpuLease(r, n, slots=2) for n in "abc")
    assert a.try_acquire() and b.try_acquire() and not c.try_acquire()
    assert (a.slot, b.slot) == (0, 1)
    assert a.holders() == ["a", "b"]


def test_redis_error_on_acquire_is_refused_not_raised():
    r = FakeRedis()
    r.fail = True
    assert gl.GpuLease(r, "a").try_acquire() is False


def test_heartbeat_thread_renews_every_third_of_the_ttl_until_release():
    r = FakeRedis()
    lease = gl.GpuLease(r, "a", ttl=180, heartbeat_period=0.1)
    assert gl.GpuLease(r, "b")._period == 60.0, "default: ttl/3"
    assert lease.try_acquire()
    lease.start_heartbeat()
    time.sleep(0.45)
    assert r.renews >= 2
    lease.release()
    seen = r.renews
    time.sleep(0.25)
    assert r.renews == seen, "no heartbeat after release"
    assert "gpu_lease:0" not in r.store


# ---- admission for a run ----------------------------------------------------

def test_run_is_deferred_while_a_live_pod_streams():
    r = FakeRedis()
    r.set("live_pod:1323-abc", "1", ex=60)
    assert gl.acquire_for_run(r, "audio-posthoc:1:x") is None
    assert not any(k.startswith("gpu_lease:") for k in r.store)
    r.now += 61           # the class ended (presence key expired)
    lease = gl.acquire_for_run(r, "audio-posthoc:1:x")
    assert lease is not None and lease.slot == 0
    assert lease._hb is not None, "heartbeat runs for the life of the run"
    lease.release()


def test_run_is_refused_while_another_run_holds_the_slot():
    r = FakeRedis()
    assert gl.GpuLease(r, "video-posthoc:2:y").try_acquire()
    assert gl.acquire_for_run(r, "audio-posthoc:1:x") is None


def test_admission_fails_closed_when_redis_is_down():
    r = FakeRedis()
    r.fail = True
    assert gl.acquire_for_run(r, "audio-posthoc:1:x") is None


def test_live_pod_count_scans_live_pod_keys_only():
    r = FakeRedis()
    assert gl.live_pod_count(r) == 0
    r.set("live_pod:a", "1", ex=60)
    r.set("live_pod:b", "1", ex=60)
    r.set("posthoc_ticket:zzz", "7", ex=900)
    assert gl.live_pod_count(r) == 2
    r.now += 61
    assert gl.live_pod_count(r) == 0


def test_final_presence_check_is_conservative():
    r = FakeRedis()
    assert gl.live_pods_present(r) is False
    r.set("live_pod:a", "1", ex=60)
    assert gl.live_pods_present(r) is True
    r.fail = True
    assert gl.live_pods_present(r) is True, "cannot tell -> do not swap results in"


def test_reply_contracts():
    assert gl.GPU_BUSY_REPLY == {
        "type": "error", "code": "gpu_busy", "retry_after": 60,
        "message": "The GPU is in use by a live session or another analysis; "
                   "try again later or use the queue."}
    assert gl.PREEMPTED_REPLY == {
        "type": "error", "code": "gpu_busy", "retry_after": 120,
        "message": "Paused: a live session started. The analysis will restart "
                   "from the queue when the class ends."}


# ---- pre-emption watcher ----------------------------------------------------

def test_watcher_fires_once_on_the_first_live_pod():
    r = FakeRedis()
    fired = []
    w = gl.PreemptionWatcher(r, on_preempt=lambda: fired.append(1), interval=999)
    assert w.check_once() is False and w.preempted is False
    r.set("live_pod:a", "1", ex=60)
    assert w.check_once() is True and w.preempted is True and fired == [1]
    assert w.check_once() is True and fired == [1], "pre-empted at most once"


def test_watcher_treats_redis_errors_as_no_class():
    r = FakeRedis()
    fired = []
    w = gl.PreemptionWatcher(r, on_preempt=lambda: fired.append(1), interval=999)
    r.fail = True
    assert w.check_once() is False and w.preempted is False and fired == []


def test_watcher_thread_polls_presence_and_sets_the_flag():
    r = FakeRedis()
    fired = []
    w = gl.PreemptionWatcher(r, on_preempt=lambda: fired.append(1), interval=0.05)
    w.start()
    time.sleep(0.15)
    assert w.preempted is False and fired == []
    r.set("live_pod:a", "1", ex=60)
    deadline = time.time() + 2
    while not w.preempted and time.time() < deadline:
        time.sleep(0.02)
    assert w.preempted is True and fired == [1]
    w.stop()
    w._thread.join(1)
    assert not w._thread.is_alive()


def test_stopped_watcher_never_fires():
    r = FakeRedis()
    fired = []
    w = gl.PreemptionWatcher(r, on_preempt=lambda: fired.append(1), interval=0.05)
    w.start()
    w.stop()
    r.set("live_pod:a", "1", ex=60)
    time.sleep(0.15)
    assert fired == [] and w.preempted is False


def test_watcher_on_preempt_exception_does_not_kill_the_flag():
    r = FakeRedis()

    def boom():
        raise RuntimeError("stop path failed")
    w = gl.PreemptionWatcher(r, on_preempt=boom, interval=999)
    r.set("live_pod:a", "1", ex=60)
    assert w.check_once() is True and w.preempted is True
