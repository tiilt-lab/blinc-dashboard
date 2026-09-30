"""Post-hoc queue under GPU admission control (src/server/posthoc_queue.py).

Phase 2 item 14 / audit C.1: the queue now runs strictly one leg at a time
(audio then video, never concurrently); stays on the leg's socket so a
``gpu_busy`` reply — at Initialize or MID-RUN when a live class pre-empts
the run — defers the job (front of the queue, sleep retry_after capped at
300 s) instead of failing it; holds the queue while live_pod:* keys exist;
and cancels a leg it gives up on. Services, DB and Redis are fakes; the
worker loop is driven on the test thread.
"""
import json
import logging
import os
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "server"))

sys.modules.setdefault('posthoc_state',
                       types.SimpleNamespace(is_running=lambda d: False))


class _NoStartThread(threading.Thread):
    def start(self):
        pass


def _import_queue():
    real = threading.Thread
    threading.Thread = _NoStartThread
    try:
        import posthoc_queue
        return posthoc_queue
    finally:
        threading.Thread = real


@pytest.fixture
def q(tmp_path, monkeypatch):
    mod = _import_queue()
    # Redirect persistence BEFORE any mutation (the default may be a real file).
    monkeypatch.setattr(mod, "_state_file", lambda: str(tmp_path / "posthoc_queue.json"))
    monkeypatch.setattr(mod, "_maybe_recycle_audio_service", lambda: None)
    monkeypatch.setattr(mod, "_job_base", lambda job: {"sessionid": job["session_id"],
                                                        "sessiondeviceid": job["device_id"]})
    tickets = []
    monkeypatch.setattr(mod, "_mint", lambda d: tickets.append(d) or "t%d" % len(tickets))
    monkeypatch.setattr(mod, "_audio_produced_output", lambda d: True)
    monkeypatch.setattr(mod, "_live_pods", lambda: 0)
    monkeypatch.setattr(mod, "_POLL_SECONDS", 0.01)
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
    cancels = []
    monkeypatch.setattr(mod, "_cancel", lambda url, d: cancels.append((url, d)) or 1)
    mod._jobs[:] = []
    mod._worker = None
    mod._test = types.SimpleNamespace(tickets=tickets, sleeps=sleeps, cancels=cancels)
    return mod


class Services:
    """Fake audio/video services behind _run_leg: one scripted outcome per
    leg attempt — None (ran to completion), a str (trigger refused), or an
    exception to raise (_GpuBusy at Initialize or mid-run, _LegTimeout)."""

    def __init__(self, q, outcomes=None):
        self.q = q
        self.outcomes = list(outcomes or [])
        self.log = []

    def run_leg(self, url, init, start_type, device_id, scope, deadline):
        assert url == (self.q.AUDIO_WS if scope == "audio" else self.q.VIDEO_WS)
        assert init["type"].startswith("Initialize_%s" % scope)
        assert start_type == "start_posthoc_%s_processing" % scope
        assert init["sessiondeviceid"] == device_id
        self.log.append(("trigger", scope, device_id, init["ticket"]))
        out = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(out, Exception):
            self.log.append(("busy" if isinstance(out, self.q._GpuBusy) else "timeout", scope))
            raise out
        if out is None:
            self.log.append(("done", scope, device_id))
        return out

    def install(self, monkeypatch):
        monkeypatch.setattr(self.q, "_run_leg", self.run_leg)
        return self


# ---- deferral -------------------------------------------------------------

def test_gpu_busy_at_initialize_defers_to_the_front_and_retries(q, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    svc = Services(q, [q._GpuBusy(5), None, None]).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    job = q._jobs[0]
    assert job["state"] == "done" and job["error"] is None
    assert job["deferrals"] == 1
    assert q._test.sleeps == [5.0]
    assert [e for e in svc.log if e[0] in ("busy", "done")] == \
        [("busy", "audio"), ("done", "audio", 10), ("done", "video", 10)]
    assert job["done_legs"] == ["audio", "video"]
    deferred = [r for r in caplog.records if "deferred" in r.getMessage()]
    assert len(deferred) == 1


def test_gpu_busy_mid_run_is_deferred_the_same_way_and_logged_once(q, monkeypatch, caplog):
    # A live class started mid-audio: the service pre-empted and sent
    # gpu_busy on the open socket. Twice in a row must still log once.
    caplog.set_level(logging.INFO)
    svc = Services(q, [q._GpuBusy(120), q._GpuBusy(900), None, None]).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    job = q._jobs[0]
    assert job["state"] == "done" and job["deferrals"] == 2
    assert q._test.sleeps == [120.0, 300.0]           # capped at 300 s
    assert [e[1] for e in svc.log if e[0] == "trigger"] == ["audio", "audio", "audio", "video"]
    assert len([r for r in caplog.records if "deferred" in r.getMessage()]) == 1
    assert len([r for r in caplog.records if "after 2 deferral(s)" in r.getMessage()]) == 1


def test_deferred_job_keeps_its_place_ahead_of_later_jobs(q, monkeypatch):
    svc = Services(q, [q._GpuBusy(2)]).install(monkeypatch)
    q.enqueue(1, [10, 11])
    q._worker_loop()
    devices = [e[2] for e in svc.log if e[0] == "trigger"]
    assert devices == [10, 10, 10, 11, 11]
    assert [j["state"] for j in q._jobs] == ["done", "done"]
    assert [j["device_id"] for j in q._jobs] == [10, 11]


def test_video_busy_after_audio_done_does_not_rerun_audio(q, monkeypatch):
    svc = Services(q, [None, q._GpuBusy(7), None]).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    scopes = [e[1] for e in svc.log if e[0] == "trigger"]
    assert scopes == ["audio", "video", "video"]
    assert q._jobs[0]["state"] == "done" and q._test.sleeps == [7.0]
    on_disk = json.loads(open(q._state_file()).read())
    assert on_disk[0]["done_legs"] == ["audio", "video"]


def test_retry_after_is_capped_and_defaulted():
    mod = _import_queue()
    assert mod._GpuBusy(900).retry_after == 300
    assert mod._GpuBusy("30").retry_after == 30
    assert mod._GpuBusy(None).retry_after == 30
    assert mod._GpuBusy("junk").retry_after == 30
    assert mod._GpuBusy(0).retry_after == 1


# ---- idle gating on live pods ---------------------------------------------

def test_idle_queue_holds_while_live_pods_stream_and_logs_once(q, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    counts = [2, 2, 1, 0]
    monkeypatch.setattr(q, "_live_pods", lambda: counts.pop(0) if counts else 0)
    svc = Services(q).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    assert q._test.sleeps == [30, 30, 30]             # three 30 s waits
    assert q._jobs[0]["state"] == "done"
    holds = [r for r in caplog.records if "holding queued jobs" in r.getMessage()]
    assert len(holds) == 1 and "2 live pod(s)" in holds[0].getMessage()
    assert len([r for r in caplog.records if "no live pods; resuming" in r.getMessage()]) == 1
    assert svc.log[0][0] == "trigger"


def test_no_live_pods_means_no_wait_and_no_log(q, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    Services(q).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    assert q._test.sleeps == []
    assert not [r for r in caplog.records if "live pod" in r.getMessage()]


def test_class_check_uses_the_shared_presence_helper(monkeypatch):
    mod = _import_queue()
    fake_r = object()
    monkeypatch.setitem(sys.modules, "redis_helper", types.SimpleNamespace(r=fake_r))
    seen = []
    monkeypatch.setitem(sys.modules, "live_presence",
                        types.SimpleNamespace(live_count=lambda r: seen.append(r) or 3))
    assert mod._live_pods() == 3 and seen == [fake_r]
    monkeypatch.setitem(sys.modules, "live_presence",
                        types.SimpleNamespace(live_count=lambda r: 1 / 0))
    assert mod._live_pods() == 0                       # Redis trouble never stalls the queue


def test_jobs_cleared_during_a_hold_are_not_started(q, monkeypatch):
    def gone():
        q.clear_pending()
        return 0

    counts = [1]
    monkeypatch.setattr(q, "_live_pods", lambda: counts.pop(0) if counts else gone())
    svc = Services(q).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    assert svc.log == [] and q._jobs == []


# ---- sequencing, tickets, cancel -------------------------------------------

def test_legs_run_strictly_in_sequence_audio_then_video(q, monkeypatch):
    svc = Services(q).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    kinds = [(e[0], e[1]) for e in svc.log]
    assert kinds == [("trigger", "audio"), ("done", "audio"),
                     ("trigger", "video"), ("done", "video")]
    # a fresh ticket per leg: an audio leg can outlive the 15 min TTL
    assert q._test.tickets == [10, 10]
    assert [e[3] for e in svc.log if e[0] == "trigger"] == ["t1", "t2"]


def test_giving_up_on_a_leg_cancels_it_at_the_service(q, monkeypatch):
    svc = Services(q, [q._LegTimeout("audio")]).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    job = q._jobs[0]
    assert job["state"] == "error" and "timed out" in job["error"]
    assert q._test.cancels == [(q.AUDIO_WS, 10)]
    assert [e[1] for e in svc.log if e[0] == "trigger"] == ["audio"]  # video never started


def test_cancel_message_carries_a_ticket(monkeypatch):
    mod = _import_queue()
    sent = []

    class WS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, m):
            sent.append(json.loads(m))

        async def recv(self):
            return json.dumps({"type": "posthoc_cancelled", "cancelled": 1})

    monkeypatch.setitem(sys.modules, "websockets",
                        types.SimpleNamespace(connect=lambda url, open_timeout=15: WS()))
    monkeypatch.setattr(mod, "_mint", lambda d: "tk")
    assert mod._cancel("ws://x", 10) == 1
    assert sent == [{"type": "cancel_posthoc", "ticket": "tk", "sessiondeviceid": 10}]


def test_one_errored_leg_is_a_warning_both_is_a_failure(q, monkeypatch):
    Services(q, ["audio down", None]).install(monkeypatch)
    q.enqueue(1, [10])
    q._worker_loop()
    assert q._jobs[0]["state"] == "done" and q._jobs[0]["done_legs"] == ["video"]

    q._jobs[:] = []
    Services(q, ["audio down", "video down"]).install(monkeypatch)
    q.enqueue(1, [11])
    q._worker_loop()
    assert q._jobs[0]["state"] == "error"
    assert q._jobs[0]["error"] == "audio: audio down / video: video down"


def test_empty_audio_output_is_an_error_even_when_video_would_follow(q, monkeypatch):
    svc = Services(q).install(monkeypatch)
    monkeypatch.setattr(q, "_audio_produced_output", lambda d: False)
    q.enqueue(1, [10])
    q._worker_loop()
    assert q._jobs[0]["state"] == "error" and "no transcripts" in q._jobs[0]["error"]
    assert [e[1] for e in svc.log if e[0] == "trigger"] == ["audio"]


# ---- the real leg runner against a scripted socket ------------------------

class WS:
    """Scripted service socket: recv() pops messages; a callable entry is
    invoked (to raise a closed-connection error); empty -> waits forever."""

    def __init__(self, msgs):
        self.msgs = list(msgs)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(json.loads(m))

    async def recv(self):
        if not self.msgs:
            import asyncio
            await asyncio.sleep(3600)
        m = self.msgs.pop(0)
        if callable(m):
            return m()
        return json.dumps(m)


ACK = {"type": "init posthoc analytics completed"}


def _socket(q, monkeypatch, msgs):
    ws = WS(msgs)
    monkeypatch.setitem(sys.modules, "websockets",
                        types.SimpleNamespace(connect=lambda url, open_timeout=15: ws))
    return ws


def _scopes(q, monkeypatch, seq):
    seq = list(seq)
    monkeypatch.setattr(q, "_scope_running", lambda d, s: seq.pop(0) if seq else False)


def test_run_leg_busy_at_initialize_raises_gpu_busy(q, monkeypatch):
    _socket(q, monkeypatch, [{"type": "error", "code": "gpu_busy", "retry_after": 42}])
    with pytest.raises(q._GpuBusy) as ei:
        q._run_leg("ws://x", {"type": "Initialize_audio_processing_analytics"}, "start",
                   10, "audio", time.time() + 60)
    assert ei.value.retry_after == 42


def test_run_leg_other_errors_are_returned_not_raised(q, monkeypatch):
    _socket(q, monkeypatch, [{"type": "error", "message": "no recording"}])
    assert q._run_leg("ws://x", {}, "start", 10, "audio", time.time() + 60) == "no recording"


def test_run_leg_busy_mid_run_raises_gpu_busy_after_start(q, monkeypatch):
    ws = _socket(q, monkeypatch, [ACK, {"type": "progress", "percent": 12},
                                  {"type": "error", "code": "gpu_busy", "retry_after": 120,
                                   "message": "live class started"}])
    _scopes(q, monkeypatch, [True, True, True])
    with pytest.raises(q._GpuBusy) as ei:
        q._run_leg("ws://x", {"type": "Initialize_audio_processing_analytics"},
                   "start_posthoc_audio_processing", 10, "audio", time.time() + 60)
    assert ei.value.retry_after == 120
    assert ws.sent[:2] == [{"type": "Initialize_audio_processing_analytics"},
                           {"type": "start_posthoc_audio_processing"}]


def test_run_leg_stays_attached_heartbeats_and_finishes_on_state(q, monkeypatch):
    ws = _socket(q, monkeypatch, [ACK])           # then silence: recv waits
    _scopes(q, monkeypatch, [True, True, False])
    assert q._run_leg("ws://x", {"type": "Initialize_video_processing_analytics"},
                      "start_posthoc_video_processing", 10, "video", time.time() + 60) is None
    assert ws.sent[1] == {"type": "start_posthoc_video_processing"}
    assert ws.sent[2:] == [{"type": "heartbeat_from_posthoc_processing"}] * 3


def test_run_leg_falls_back_to_polling_when_the_socket_drops(q, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def closed():
        raise ConnectionError("1006 going away")

    ws = _socket(q, monkeypatch, [ACK, closed])
    _scopes(q, monkeypatch, [True, False])
    assert q._run_leg("ws://x", {"type": "init"}, "start", 10, "audio", time.time() + 60) is None
    assert ws.sent == [{"type": "init"}, {"type": "start"}]  # no heartbeats after the drop
    assert any("polling state only" in r.getMessage() for r in caplog.records)


def test_run_leg_times_out_when_never_seen_running(q, monkeypatch):
    _socket(q, monkeypatch, [ACK])
    _scopes(q, monkeypatch, [])
    with pytest.raises(q._LegTimeout):
        q._run_leg("ws://x", {}, "start", 10, "audio", time.time() + 0.05)


def test_scope_running_uses_running_detail_when_the_tracker_has_it(q, monkeypatch):
    fake = types.SimpleNamespace(
        is_running=lambda d: True,
        running_detail=lambda d: {"running": True, "scopes": ["video"], "started_at": 1})
    monkeypatch.setattr(q, "posthoc_state", fake)
    assert q._scope_running(10, "video") is True
    assert q._scope_running(10, "audio") is False
    monkeypatch.setattr(q, "posthoc_state", types.SimpleNamespace(is_running=lambda d: True))
    assert q._scope_running(10, "audio") is True   # old tracker: any scope
