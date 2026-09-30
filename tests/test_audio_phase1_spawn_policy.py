"""CrisperWhisper worker slot resilience (audit B.2 / E.3).

A slot (_WorkerSlot) is what the single shared worker used to be; the pool
that composes slots is covered by test_asr_worker_pool.py.

A failed spawn or a dead worker used to be retried on every window of every
pod, each retry a 7s model load (or a 180s timeout) under the shared lock,
with stderr thrown away. Now: SpawnBackoff breaker (30s -> 300s), a free-VRAM
gate before spawning, stderr appended to a log file, and a degraded/ok status
fanned out to every live pod once per state change.
"""
import io
import os
import subprocess
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

from asr_connectors import crisperwhisper_asr as cw  # noqa: E402


def test_backoff_doubles_from_30s_to_300s_cap():
    b = cw.SpawnBackoff()
    delays = [b.record_failure(now=1000.0 + i) for i in range(6)]
    assert delays == [30, 60, 120, 240, 300, 300]


def test_backoff_blocks_until_the_deadline_then_allows():
    b = cw.SpawnBackoff()
    assert b.may_attempt(0.0)
    b.record_failure(100.0)
    assert not b.may_attempt(129.0) and b.retry_in(129.0) == 1.0
    assert b.may_attempt(130.0) and b.retry_in(130.0) == 0.0


def test_success_resets_the_breaker():
    b = cw.SpawnBackoff()
    b.record_failure(0.0)
    b.record_failure(30.0)
    b.record_success()
    assert b.failures == 0 and b.may_attempt(31.0) and b.next_delay() == 30


def test_free_vram_parses_nvidia_smi_and_treats_failure_as_unknown():
    def fake(stdout):
        return lambda *a, **k: types.SimpleNamespace(stdout=stdout)

    assert cw.free_vram_mib(run=fake("1234\n8000\n")) == 8000

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("nvidia-smi", 5)

    assert cw.free_vram_mib(run=boom) is None
    assert cw.free_vram_mib(run=fake("N/A\n")) is None
    assert cw.free_vram_mib(run=fake("")) is None


def test_worker_stderr_goes_to_a_log_file_not_devnull():
    with open(cw.__file__) as f:
        src = f.read()
    assert "stderr=subprocess.DEVNULL" not in src
    # One log per pool slot: audio_processing/crisper_worker.<slot>.stderr.log
    assert (cw._STDERR_LOG_TEMPLATE % 0).endswith(
        os.path.join("audio_processing", "crisper_worker.0.stderr.log"))
    assert cw._WorkerSlot(1).stderr_log.endswith("crisper_worker.1.stderr.log")


class _Proc:
    def __init__(self):
        self.stdin = io.StringIO()
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


def _no_spawn(*a, **k):
    pytest.fail("Popen must not be called")


def test_open_breaker_refuses_at_once_without_spawning(monkeypatch):
    w = cw._WorkerSlot()
    monkeypatch.setattr(cw.subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(cw, "free_vram_mib", lambda run=None: None)
    w.backoff.record_failure(time.time())
    t0 = time.time()
    with pytest.raises(cw.WorkerUnavailable):
        w.transcribe("/nonexistent.wav")
    assert time.time() - t0 < 1.0            # never waits (lock never held across a wait)


def test_low_vram_refuses_spawn_opens_breaker_and_tells_pods_once(monkeypatch):
    w = cw._WorkerSlot()
    got = []
    w.add_listener(lambda s, m: got.append((s, m)))
    monkeypatch.setattr(cw, "free_vram_mib", lambda run=None: 1000)
    monkeypatch.setattr(cw.subprocess, "Popen", _no_spawn)
    with pytest.raises(cw.WorkerUnavailable):
        w.transcribe("x.wav")
    assert got == [("degraded", cw.MSG_LOW_VRAM)]
    assert w.backoff.failures == 1 and not w.backoff.may_attempt(time.time())
    with pytest.raises(cw.WorkerUnavailable):
        w.transcribe("x.wav")                # breaker open: no second message
    assert len(got) == 1
    late = []
    w.add_listener(lambda s, m: late.append(s))
    assert late == ["degraded"]              # a pod joining mid-outage is told now
    w.remove_listener(got.append)            # unknown listener: no error


def test_served_window_recovers_state_and_resets_breaker(monkeypatch):
    w = cw._WorkerSlot()
    got = []
    w.add_listener(lambda s, m: got.append(s))
    w._set_state("degraded", cw.MSG_RESTARTING)
    w.backoff.record_failure(0.0)
    w._proc = _Proc()
    monkeypatch.setattr(w, "_read_reply", lambda timeout: '{"words": []}\n')
    assert w.transcribe("x.wav") == {"words": []}
    assert got == ["degraded", "ok"] and w.backoff.failures == 0
    assert w.transcribe("x.wav") == {"words": []}
    assert got == ["degraded", "ok"]         # ok is sent once, not per window


def test_request_failure_kills_worker_and_degrades(monkeypatch):
    w = cw._WorkerSlot()
    got = []
    w.add_listener(lambda s, m: got.append((s, m)))
    proc = _Proc()
    w._proc = proc

    def unresponsive(timeout):
        raise RuntimeError("CrisperWhisper worker unresponsive for 120s")

    monkeypatch.setattr(w, "_read_reply", unresponsive)
    with pytest.raises(RuntimeError):
        w.transcribe("x.wav")
    assert proc.killed and w._proc is None
    assert got == [("degraded", cw.MSG_UNRESPONSIVE)]
    assert w.backoff.failures == 1
