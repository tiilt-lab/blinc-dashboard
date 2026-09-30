"""Post-hoc ASR worker subprocesses (src/audio_processing/asr_connectors/base_asr.py;
audit C.5): started in their own process group, killed on cancel/timeout
(SIGTERM, then SIGKILL), and a failure is reported to the consumer ahead of the
end-of-stream sentinel (audit C.3).
"""
import os
import queue as queue_module
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src",
                                "audio_processing", "asr_connectors"))

import base_asr  # noqa: E402

SLEEPER = "import time; time.sleep(30)"
STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"


def _argv(code):
    return lambda out: [sys.executable, "-c", code, out]


def test_worker_runs_in_its_own_session_and_process_group():
    data = base_asr.run_json_worker(_argv(
        "import json, os, sys; json.dump({'pid': os.getpid(), 'pgid': os.getpgrp(), "
        "'sid': os.getsid(0)}, open(sys.argv[1], 'w'))"), timeout=30)
    assert data["pid"] == data["pgid"] == data["sid"]
    assert data["pgid"] != os.getpgrp()


def _run_in_thread(handle, code, timeout=30):
    result = {}

    def go():
        try:
            base_asr.run_json_worker(_argv(code), timeout=timeout, handle=handle)
            result["outcome"] = "returned"
        except Exception as e:
            result["outcome"] = str(e)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, result


def _wait_attached(handle):
    deadline = time.time() + 5
    while handle._proc is None and time.time() < deadline:
        time.sleep(0.01)
    assert handle._proc is not None


def test_terminate_kills_the_worker_and_the_run_reports_cancelled():
    handle = base_asr.WorkerHandle()
    t, result = _run_in_thread(handle, SLEEPER)
    _wait_attached(handle)
    pid = handle._proc.pid
    started = time.time()
    handle.terminate(grace=2)
    t.join(10)
    assert not t.is_alive() and time.time() - started < 5
    assert result["outcome"] == "worker cancelled"
    assert handle.killed and handle._proc.returncode is not None
    try:
        os.kill(pid, 0)
        assert False, "worker still alive"
    except ProcessLookupError:
        pass


def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored():
    handle = base_asr.WorkerHandle()
    t, result = _run_in_thread(handle, STUBBORN)
    _wait_attached(handle)
    time.sleep(0.3)  # let the child install its handler
    started = time.time()
    handle.terminate(grace=0.5)
    t.join(10)
    assert not t.is_alive() and time.time() - started < 5
    assert result["outcome"] == "worker cancelled"
    assert handle._proc.returncode == -9


def test_timeout_kills_the_worker_and_raises():
    handle = base_asr.WorkerHandle()
    started = time.time()
    try:
        base_asr.run_json_worker(_argv(SLEEPER), timeout=0.5, handle=handle)
        assert False, "expected a timeout"
    except RuntimeError as e:
        assert "timed out" in str(e)
    assert time.time() - started < 5
    assert handle._proc.returncode is not None


def test_terminate_before_spawn_kills_on_attach():
    handle = base_asr.WorkerHandle()
    handle.terminate()          # cancel raced the spawn
    started = time.time()
    try:
        base_asr.run_json_worker(_argv(SLEEPER), timeout=30, handle=handle)
        assert False, "expected the late attach to kill the worker"
    except RuntimeError as e:
        assert "cancelled" in str(e)
    assert time.time() - started < 5


class _Conn(base_asr.PosthocFileASR):
    DRAIN_NAME = "test"

    def __init__(self, code):
        self.audio_queue = queue_module.Queue()
        self.transcript_queue = queue_module.Queue()
        self.running = False
        self._code = code

    def _transcribe_file(self):
        try:
            base_asr.run_json_worker(_argv(self._code), timeout=30,
                                     handle=getattr(self, "worker", None))
        except Exception as e:
            self.fail(e)
        finally:
            self.running = False
            self.transcript_queue.put(None)


def test_stop_terminates_the_worker_and_the_failure_precedes_the_sentinel():
    conn = _Conn(SLEEPER)
    conn.start()
    _wait_attached(conn.worker)
    conn.stop()
    first = conn.transcript_queue.get(timeout=10)
    assert isinstance(first, base_asr.AsrFailed) and "cancelled" in first.reason
    assert conn.transcript_queue.get(timeout=10) is None
    assert conn.error == first.reason and conn.running is False


def test_a_crashing_worker_is_reported_as_a_failure():
    conn = _Conn("import sys; sys.stderr.write('CUDA out of memory'); sys.exit(1)")
    conn.start()
    first = conn.transcript_queue.get(timeout=10)
    assert isinstance(first, base_asr.AsrFailed) and "CUDA out of memory" in first.reason
    assert conn.transcript_queue.get(timeout=10) is None
