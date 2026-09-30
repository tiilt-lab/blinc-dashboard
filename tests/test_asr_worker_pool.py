"""Live CrisperWhisper worker pool (audit B.2: "a second worker").

One --serve worker spends ~1s of GPU per 12s window, so it saturates at
~10-12 talking pods. WorkerPool runs DC_ASR_WORKERS slots (default 2):
lazy second spawn only under contention, a free-VRAM check at every spawn,
one FIFO across pods, per-pod order kept by the pod's sequential flush
thread, degraded status only when no slot can serve, and a rate-limited
"capacity reached" WARNING for the load generator.

Slots are real _WorkerSlot objects whose subprocess is faked: the fake
worker answers the ready line at once and each request after ``service``
seconds, recording which slot served which window in what order.
"""
import json
import logging
import os
import queue
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

from asr_connectors import crisperwhisper_asr as cw  # noqa: E402


class FakeProc:
    """Stand-in for the --serve subprocess: stdin/stdout are this object."""

    def __init__(self, slot, harness):
        self.slot = slot
        self.h = harness
        self.stdin = self.stdout = self
        self.pid = 1000 + slot.index
        self._job = None
        self._rc = None
        self.killed = False

    def write(self, line):
        self._job = json.loads(line)

    def flush(self):
        pass

    def readline(self):
        job, self._job = self._job, None
        if job is None:                       # the {"ready": true} line
            return json.dumps({"ready": True}) + "\n"
        time.sleep(self.h.service)
        self.h.served.append((job["audio"], self.slot.index))
        return json.dumps({"words": [[job["audio"], 0.0, 1.0]]}) + "\n"

    def poll(self):
        return self._rc

    def die(self, rc=1):
        self._rc = rc

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        pass


class FakeSlot(cw._WorkerSlot):
    def __init__(self, index, harness):
        super().__init__(index)
        self.h = harness

    def _launch(self):
        self.h.spawns.append((self.index, {s.index for s in self.h.pool._busy}))
        if self.index in self.h.spawn_fail:
            raise RuntimeError("fake spawn failure")
        return FakeProc(self, self.h)

    def _read_reply(self, timeout):
        return self._proc.stdout.readline()


class Harness:
    def __init__(self, size, service=0.1, spawn_grace=0.05, **pool_kw):
        self.service = service
        self.spawns = []        # (slot index, slots busy at that moment)
        self.served = []        # (window, slot index) in completion order
        self.spawn_fail = set()
        self.pool = cw.WorkerPool(size=size, slot_factory=lambda i: FakeSlot(i, self),
                                  spawn_grace=spawn_grace, **pool_kw)

    def spawned(self):
        return [i for i, _ in self.spawns]

    def slots_used(self):
        return {i for _, i in self.served}

    def order(self):
        return [w for w, _ in self.served]

    def pod(self, name, windows=1):
        """A pod's flush thread: its windows one after another, in order."""
        def run():
            for k in range(windows):
                self.pool.transcribe("%s-%d" % (name, k))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t


@pytest.fixture
def plenty_of_vram(monkeypatch):
    monkeypatch.setattr(cw, "free_vram_mib", lambda run=None: 8000)


# --- pool size --------------------------------------------------------------

def test_pool_size_comes_from_dc_asr_workers(monkeypatch):
    monkeypatch.delenv("DC_ASR_WORKERS", raising=False)
    assert cw.pool_size() == 2 and cw.WorkerPool().size == 2
    monkeypatch.setenv("DC_ASR_WORKERS", "1")
    assert cw.pool_size() == 1 and cw.WorkerPool().size == 1
    monkeypatch.setenv("DC_ASR_WORKERS", "3")
    assert cw.WorkerPool().size == 3
    monkeypatch.setenv("DC_ASR_WORKERS", "0")
    assert cw.pool_size() == 1                       # never fewer than one worker
    monkeypatch.setenv("DC_ASR_WORKERS", "two")
    assert cw.pool_size() == 2


def test_each_slot_has_its_own_stderr_log_breaker_and_lock():
    pool = cw.WorkerPool(size=2)
    a, b = pool.slots
    assert a.stderr_log.endswith("crisper_worker.0.stderr.log")
    assert b.stderr_log.endswith("crisper_worker.1.stderr.log")
    assert a.backoff is not b.backoff and a._lock is not b._lock
    assert not a.alive and not b.alive               # nothing spawned at construction


# --- lazy spawn + VRAM gate -------------------------------------------------

def test_second_slot_spawns_only_when_the_first_is_busy(plenty_of_vram):
    h = Harness(size=2, service=0.15)
    for k in range(3):                               # one pod, windows back to back
        h.pool.transcribe("A-%d" % k)
    assert h.spawned() == [0] and h.slots_used() == {0}
    a = h.pod("A")                                   # two pods collide...
    time.sleep(0.03)
    h.pool.transcribe("B-0")
    a.join()
    assert h.spawned() == [0, 1]                     # ...and only then slot 1 starts
    assert 0 in h.spawns[1][1]                       # while slot 0 was busy (slot 1 is marked busy for its own spawn)
    assert h.slots_used() == {0, 1}


def test_a_momentary_collision_waits_for_the_live_slot_instead_of_spawning(plenty_of_vram):
    h = Harness(size=2, service=0.1, spawn_grace=0.5)
    a = h.pod("A")
    time.sleep(0.03)
    h.pool.transcribe("B-0")                         # waits ~70 ms, well under the grace
    a.join()
    assert h.spawned() == [0] and h.order() == ["A-0", "B-0"]


def test_every_spawn_is_gated_by_free_vram_at_that_moment(monkeypatch):
    probes = []

    def vram(run=None):
        probes.append(1)
        return 8000 if len(probes) == 1 else 1000    # slot 0 fits; then the card is full

    monkeypatch.setattr(cw, "free_vram_mib", vram)
    h = Harness(size=2, service=0.15)
    told = []
    h.pool.add_listener(lambda s, m: told.append((s, m)))
    h.pool.transcribe("A-0")
    a = h.pod("A")
    time.sleep(0.03)
    h.pool.transcribe("B-0")                         # slot 1 refused -> fails over to slot 0
    a.join()
    assert len(probes) == 2                          # one nvidia-smi probe per spawn attempt
    assert h.spawned() == [0] and h.slots_used() == {0}
    assert h.pool.slots[1].backoff.failures == 1 and h.pool.slots[1].state == "degraded"
    assert told == [] and h.pool.state == "ok"       # a slot serves: pods never told degraded


# --- FIFO across pods, order within a pod ----------------------------------

def test_windows_are_served_fifo_across_pods(plenty_of_vram):
    h = Harness(size=1, service=0.1)
    h.pool.transcribe("warm-0")
    threads = [h.pod("X")]                           # occupies the slot
    time.sleep(0.02)
    for name in ("A", "B", "C"):
        threads.append(h.pod(name))
        time.sleep(0.02)
    for t in threads:
        t.join()
    assert h.order() == ["warm-0", "X-0", "A-0", "B-0", "C-0"]


def test_a_pod_with_a_backlog_cannot_hog_the_slot(plenty_of_vram):
    h = Harness(size=1, service=0.1)
    h.pool.transcribe("warm-0")
    a = h.pod("A", windows=3)
    time.sleep(0.03)
    b = h.pod("B", windows=3)
    a.join()
    b.join()
    assert [w[0] for w in h.order()[1:]] == list("ABABAB")


def test_a_pods_windows_are_transcribed_in_order_by_one_sequential_thread(monkeypatch):
    calls, active, overlapped = [], [0], [False]

    class FakePool:
        def transcribe(self, wav_path):
            active[0] += 1
            overlapped[0] |= active[0] > 1
            time.sleep(0.01)
            calls.append(wav_path)
            active[0] -= 1
            return {"words": [["hi", 0.0, 0.5]]}

    monkeypatch.setattr(cw, "_pool", FakePool())
    transcripts = queue.Queue()
    asr = cw.CrisperWhisperASR(queue.Queue(), transcripts, None, "audio", 0.25)
    for start in (0.0, 12.0, 24.0):
        asr._window_queue.put((start, b"\0\0" * 160))
    asr._window_queue.put(None)
    asr._transcribing()                              # the pod's flush thread body
    starts = []
    while True:
        r = transcripts.get_nowait()
        if r is None:
            break
        starts.append(r.alternatives[0].words[0].start_time.seconds)
    assert starts == [0, 12, 24] and len(calls) == 3 and not overlapped[0]


def test_with_two_slots_each_pod_still_goes_in_order_and_both_slots_serve(plenty_of_vram):
    h = Harness(size=2, service=0.1)
    a = h.pod("A", windows=3)
    time.sleep(0.03)
    b = h.pod("B", windows=3)
    a.join()
    b.join()
    for name in "AB":
        assert [w for w in h.order() if w.startswith(name)] == ["%s-%d" % (name, k) for k in range(3)]
    assert h.spawned() == [0, 1] and h.slots_used() == {0, 1}


# --- status -----------------------------------------------------------------

def test_degraded_only_when_no_slot_can_serve_then_ok_once_on_recovery(monkeypatch):
    monkeypatch.setattr(cw, "free_vram_mib", lambda run=None: 1000)
    h = Harness(size=2)
    told = []
    h.pool.add_listener(lambda s, m: told.append((s, m)))
    with pytest.raises(cw.WorkerUnavailable):
        h.pool.transcribe("A-0")                     # slot 0 refused, slot 1 tried and refused
    assert [s.backoff.failures for s in h.pool.slots] == [1, 1]
    assert told == [("degraded", cw.MSG_LOW_VRAM)]
    t0 = time.monotonic()
    with pytest.raises(cw.WorkerUnavailable):
        h.pool.transcribe("A-1")                     # breakers open: refused at once
    assert time.monotonic() - t0 < 0.5 and len(told) == 1
    late = []
    h.pool.add_listener(lambda s, m: late.append(s))
    assert late == ["degraded"]                      # a pod joining mid-outage is told now
    monkeypatch.setattr(cw, "free_vram_mib", lambda run=None: 8000)
    for s in h.pool.slots:
        s.backoff.not_before = 0.0                   # breakers due
    h.pool.transcribe("A-2")
    h.pool.transcribe("A-3")
    assert told == [("degraded", cw.MSG_LOW_VRAM), ("ok", None)]
    assert h.spawned() == [0]                        # one worker is enough again


def test_one_failed_slot_fails_over_within_the_window_and_status_stays_ok(plenty_of_vram):
    h = Harness(size=2)
    h.spawn_fail = {0}
    told = []
    h.pool.add_listener(lambda s, m: told.append((s, m)))
    assert h.pool.transcribe("A-0")["words"][0][0] == "A-0"
    assert h.spawned() == [0, 1] and h.slots_used() == {1}
    assert h.pool.slots[0].state == "degraded" and h.pool.slots[0].backoff.failures == 1
    assert told == [] and h.pool.state == "ok"


def test_one_worker_behaves_like_the_old_single_shared_worker(plenty_of_vram):
    h = Harness(size=1, service=0.1)
    a = h.pod("A", windows=2)
    time.sleep(0.03)
    b = h.pod("B", windows=2)
    a.join()
    b.join()
    assert h.spawned() == [0] and h.slots_used() == {0}
    told = []
    h.pool.add_listener(lambda s, m: told.append((s, m)))
    h.pool.slots[0]._proc.die(rc=137)                # OOM-killed between windows
    t0 = time.monotonic()
    with pytest.raises(cw.WorkerUnavailable):
        h.pool.transcribe("A-9")
    assert time.monotonic() - t0 < 0.5               # breaker: refused at once, no wait
    assert told == [("degraded", cw.MSG_RESTARTING)]
    assert h.spawned() == [0]                        # no respawn while the breaker is open


# --- capacity signal --------------------------------------------------------

def test_capacity_warning_text_is_a_module_constant():
    assert cw.CAPACITY_WARNING == "CrisperWhisper: ASR capacity reached"
    assert cw.CAPACITY_WAIT_SECONDS == 5.0


def test_capacity_warning_when_a_window_waits_too_long_rate_limited(plenty_of_vram, caplog):
    h = Harness(size=1, service=0.2, capacity_wait=0.05, warning_interval=60.0)
    h.pool.transcribe("warm-0")
    with caplog.at_level(logging.WARNING):
        threads = []
        for name in ("A", "B", "C"):
            threads.append(h.pod(name))
            time.sleep(0.02)
        for t in threads:
            t.join()
    hits = [r for r in caplog.records if r.getMessage().startswith(cw.CAPACITY_WARNING)]
    assert len(hits) == 1 and hits[0].levelno == logging.WARNING   # B and C both waited; one line
    assert "waited" in hits[0].getMessage() and "1 slots busy" in hits[0].getMessage()


def test_no_capacity_warning_for_short_waits(plenty_of_vram, caplog):
    h = Harness(size=1, service=0.05)                # default 5 s threshold
    with caplog.at_level(logging.WARNING):
        a = h.pod("A")
        time.sleep(0.01)
        h.pool.transcribe("B-0")
        a.join()
    assert not [r for r in caplog.records if cw.CAPACITY_WARNING in r.getMessage()]


def test_fifo_wait_is_bounded_by_the_request_timeout(plenty_of_vram):
    h = Harness(size=1, service=0.4, request_timeout=0.1)
    h.pool.transcribe("warm-0")
    a = h.pod("A")
    time.sleep(0.02)
    t0 = time.monotonic()
    with pytest.raises(cw.WorkerUnavailable):
        h.pool.transcribe("B-0")
    assert 0.05 < time.monotonic() - t0 < 0.35
    a.join()
    assert h.order() == ["warm-0", "A-0"] and h.pool.waiting == 0
