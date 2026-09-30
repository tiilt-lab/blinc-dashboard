"""Server-side queue for running full post-hoc analyses on multiple pods in
sequence. Jobs survive browser refreshes because the server both triggers the
runs (same WS protocol as the UI) and observes completion via posthoc_state
(fed by the services' reset/completed callbacks). Strictly one run at a time:
one pod, and within it the audio leg then the video leg (they used to run
concurrently and each loads 5-10 GB of models). A service that answers a
trigger with ``{"type":"error","code":"gpu_busy","retry_after":N}`` (no GPU
lease, or a live class is on) is not a failure: the job goes back to the
front of the queue and is retried after N seconds (capped). A leg the queue
gives up on is cancelled at the service so it stops holding the GPU.
"""

import os
import json
import time
import logging
import threading
import asyncio

import posthoc_state

# Loopback on purpose: the services now bind 127.0.0.1 only.
AUDIO_WS = "ws://127.0.0.1:%s" % os.getenv("DC_AUDIO_POSTHOC_WS_PORT", "9015")
VIDEO_WS = "ws://127.0.0.1:%s" % os.getenv("DC_VIDEO_POSTHOC_WS_PORT", "9014")
_POD_TIMEOUT = 150 * 60  # video-bound pods on long recordings can exceed an hour
_POLL_SECONDS = 10
_LIVE_POLL_SECONDS = 30  # while a class streams, re-check presence this often


class _LegTimeout(Exception):
    """A leg was never seen running, or never finished, before the deadline."""


class _GpuBusy(Exception):
    """The service declined the trigger for now (GPU lease held or live pods
    present). Carries the capped wait before the next attempt."""
    MAX_DELAY = 300.0
    DEFAULT_DELAY = 30.0

    def __init__(self, retry_after):
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = self.DEFAULT_DELAY
        self.retry_after = max(1.0, min(delay, self.MAX_DELAY))
        super().__init__("GPU busy, retry in %.0fs" % self.retry_after)


_lock = threading.Lock()
_jobs = []  # {session_id, device_id, state: queued|running|done|error, error}
_worker = None

# The queue survives Flask restarts: every mutation snapshots to disk, and
# import-time restore re-queues whatever was pending (a job caught mid-run
# is re-queued too — post-hoc runs are idempotent, so re-running beats
# guessing whether the interrupted run finished). This retires the manual
# snapshot-restart-re-enqueue routine restarts used to require.
def _state_file():
    try:
        import config as cf
        if not hasattr(cf, "config"):
            cf.initialize()  # idempotent re-read of config.ini
        base = cf.root_dir()
        if base and os.path.isdir(base):
            return os.path.join(base, "posthoc_queue.json")
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "posthoc_queue.json")


def _persist_locked():
    path = _state_file()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_jobs, f)
        os.replace(tmp, path)
    except Exception as e:
        logging.warning("posthoc queue: persist failed: %s", e)


def _restore():
    global _worker
    path = _state_file()
    try:
        with open(path) as f:
            jobs = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        logging.warning("posthoc queue: restore failed: %s", e)
        return
    with _lock:
        for j in jobs:
            if j.get("state") == "running":
                j["state"] = "queued"
        _jobs[:] = jobs
        pending = sum(1 for j in _jobs if j["state"] == "queued")
        if pending and (_worker is None or not _worker.is_alive()):
            _worker = threading.Thread(target=_worker_loop,
                                       name="posthoc-queue", daemon=True)
            _worker.start()
    if pending:
        logging.info("posthoc queue: restored %d pending job(s) after restart",
                     pending)


def _busy_from(msg):
    # The services' admission-control reply, sent at Initialize (no GPU lease
    # / live pods present) or MID-RUN when a live class starts and the run is
    # pre-empted. Either way the job is deferred, never failed.
    if msg.get("type") == "error" and msg.get("code") == "gpu_busy":
        raise _GpuBusy(msg.get("retry_after"))


async def _init_phase(ws, init, start_type):
    # Initialize, wait for the readiness ack, send start. Returns an error
    # string when the service refused, None when the run started.
    await ws.send(json.dumps(init))
    deadline = time.time() + 120
    while time.time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        t = msg.get("type", "")
        if t == "init posthoc analytics completed":
            await ws.send(json.dumps({"type": start_type}))
            return None
        if t == "error":
            _busy_from(msg)
            return msg.get("message", "service error")
    return "timed out waiting for init ack"


async def _run_phase(ws, device_id, scope, deadline):
    # Stay on the socket while the leg runs, polling posthoc_state alongside.
    # The queue used to hang up after start; a run pre-empted by a live class
    # (gpu_busy mid-stream) would then have sat out the whole pod timeout.
    # Heartbeats keep the service's inactivity sweep from closing us; if the
    # socket drops anyway (service restart), polling continues alone. True
    # once the leg was seen running and then cleared; False on the deadline.
    seen_running = False
    attached = True
    while time.time() < deadline:
        if attached:
            until = time.time() + _POLL_SECONDS
            while True:
                remaining = until - time.time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except Exception as e:
                    logging.info("posthoc queue: pod %s %s socket closed mid-run (%s); polling state only",
                                 device_id, scope, e)
                    attached = False
                    break
                try:
                    _busy_from(json.loads(raw))
                except _GpuBusy:
                    raise
                except Exception:
                    pass  # progress / status traffic
            if attached:
                try:
                    await ws.send(json.dumps({"type": "heartbeat_from_posthoc_processing"}))
                except Exception:
                    attached = False
        else:
            await asyncio.sleep(_POLL_SECONDS)
        # The reset callback marks the scope running; the completed callback
        # clears it. Require it to have been seen running first.
        if _scope_running(device_id, scope):
            seen_running = True
        elif seen_running:
            return True
    return False


def _run_leg(url, init, start_type, device_id, scope, deadline):
    """One leg end to end: Initialize, start, then watch it run. Returns an
    error string when the service refused the trigger, None when the leg
    finished; raises _GpuBusy (defer the job) or _LegTimeout (cancel it)."""
    import websockets

    async def go():
        async with websockets.connect(url, open_timeout=15) as ws:
            err = await _init_phase(ws, init, start_type)
            if err:
                return err
            if not await _run_phase(ws, device_id, scope, deadline):
                raise _LegTimeout(scope)
            return None

    return asyncio.run(go())


def _mint(device_id):
    # The services refuse any pod-naming message without a ticket minted for
    # that pod (common/posthoc_ticket); mint one exactly as the UI does.
    from redis_helper import RedisPosthocTicket
    return RedisPosthocTicket.mint(device_id)


def _cancel(url, device_id):
    # Best-effort cancel_posthoc for a leg the queue has given up on (audit
    # C.1: a timed-out run used to keep the GPU for up to 2 h and still post
    # its results). Returns the service's cancelled count, or None.
    import websockets

    async def go():
        async with websockets.connect(url, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "cancel_posthoc",
                                      "ticket": _mint(device_id),
                                      "sessiondeviceid": device_id}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            return msg.get("cancelled")

    try:
        n = asyncio.run(go())
        logging.warning("posthoc queue: cancelled pod %s at %s (%s run(s))", device_id, url, n)
        return n
    except Exception as e:
        logging.warning("posthoc queue: cancel for pod %s at %s failed: %s", device_id, url, e)
        return None


def _job_base(job):
    # The pod-naming fields every trigger carries; the ticket is minted per
    # leg (15 min TTL; an audio leg can outlive it).
    import app as A
    with A.app.app_context():
        import database
        session = database.get_sessions(id=job["session_id"])
        device_id = job["device_id"]
        speakers = [{"id": s.id, "alias": s.get_alias()}
                    for s in database.get_speakers(session_device_id=device_id)]
        return {
            "sessionid": job["session_id"],
            "sessiondeviceid": device_id,
            "server_start": session.creation_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "keywords": (session.json() or {}).get("keywords") or [],
            "speakers": speakers,
        }


def _audio_produced_output(device_id):
    # A silent failure (OOM, empty recording) completes with zero transcripts
    # and must surface as an error, not "Analyzed".
    import app as A
    with A.app.app_context():
        import database as db
        if db.get_pod_duration(device_id) is None:
            # The completion callback already stamped the pod analyzed; undo
            # that so the failure stays visible and retries don't skip it.
            db.clear_session_device_posthoc(device_id)
            return False
    return True


def _leg_url(scope):
    return AUDIO_WS if scope == "audio" else VIDEO_WS


def _scope_running(device_id, scope):
    detail = getattr(posthoc_state, "running_detail", None)
    if detail is None:
        return posthoc_state.is_running(device_id)
    return scope in (detail(device_id).get("scopes") or ())


def _live_pods():
    # live_pod:* keys the audio server holds while a class streams
    # (common/live_presence). Redis trouble counts as none: the services'
    # own gpu_busy reply is the second line of defence, and a dead Redis
    # must not stall the queue forever.
    try:
        from redis_helper import r  # also puts src/common on sys.path
        import live_presence
        return live_presence.live_count(r)
    except Exception as e:
        logging.warning("posthoc queue: live-pod check failed (%s); assuming none", e)
        return 0


def _wait_for_class_to_end():
    # Idle gating: with a class on, don't even start the next job. One log
    # line per wait, then one when it ends.
    n = _live_pods()
    if not n:
        return
    logging.info("posthoc queue: %d live pod(s) streaming; holding queued jobs until the class ends", n)
    while n:
        time.sleep(_LIVE_POLL_SECONDS)
        n = _live_pods()
    logging.info("posthoc queue: no live pods; resuming")


def _run_job(job):
    base = _job_base(job)
    device_id = job["device_id"]
    models = job.get("models") or {}
    # Legs finished on an earlier attempt (before a gpu_busy deferral) are
    # not re-run; the list is persisted with the job.
    done = job.setdefault("done_legs", [])
    deadline = time.time() + _POD_TIMEOUT
    attempted, errors = [], {}
    for scope in ("audio", "video"):
        if scope in done:
            continue
        attempted.append(scope)
        url = _leg_url(scope)
        if scope == "audio":
            init = dict(base, type="Initialize_audio_processing_analytics",
                        asr=models.get("asr", "whisperx"),
                        scorer=models.get("scorer"),
                        diarizer=models.get("diarizer", "pyannote"),
                        embedder=models.get("embedder"))
            start = "start_posthoc_audio_processing"
        else:
            init = dict(base, type="Initialize_video_processing_analytics")
            start = "start_posthoc_video_processing"
        init["ticket"] = _mint(device_id)
        try:
            # _GpuBusy propagates: the worker loop defers the whole job.
            err = _run_leg(url, init, start, device_id, scope, deadline)
        except _LegTimeout:
            # Audit C.1: a leg we give up on must not keep the GPU.
            _cancel(url, device_id)
            raise RuntimeError("timed out waiting for pod %s %s to finish" % (device_id, scope))
        if err:
            logging.warning("posthoc queue: pod %s %s trigger: %s", device_id, scope, err)
            errors[scope] = err
            continue
        if scope == "audio" and not _audio_produced_output(device_id):
            raise RuntimeError("audio produced no transcripts (empty recording or transcription failure)")
        done.append(scope)
        with _lock:
            _persist_locked()
    if attempted and len(errors) == len(attempted):
        raise RuntimeError(" / ".join("%s: %s" % (s, errors[s]) for s in attempted))


# The audio post-hoc service accumulates host RSS across pods (worker threads
# blocked in queue reads survive their run and pin each pod's in-RAM audio
# buffer). Unchecked, the kernel OOM-kills it every ~8-10 pods, erroring the
# in-flight pod. Between jobs the service is guaranteed idle, so the queue
# recycles it there whenever it has grown past the threshold: a ~30s restart
# instead of a crash with casualties.
_AUDIO_UNIT = "blinc-audio-posthoc-processor.service"
_AUDIO_RSS_RECYCLE_BYTES = 14 * 2 ** 30


def _audio_service_rss():
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "show", _AUDIO_UNIT, "-p", "MemoryCurrent", "--value"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out)
    except Exception:
        return 0


def _maybe_recycle_audio_service():
    import socket
    import subprocess
    rss = _audio_service_rss()
    if rss < _AUDIO_RSS_RECYCLE_BYTES:
        return
    logging.info("posthoc queue: audio service at %.1f GiB — recycling between pods",
                 rss / 2 ** 30)
    try:
        # sudo is pinned to exactly this command in /etc/sudoers.d/blinc-posthoc.
        # Inspect the result: a sudoers/unit misconfig otherwise fails silently
        # and we'd waste the 240s port-poll below before continuing.
        proc = subprocess.run(["sudo", "-n", "systemctl", "restart", _AUDIO_UNIT],
                              capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            logging.warning("posthoc queue: audio service recycle exited %s: %s",
                            proc.returncode, (proc.stderr or "").strip())
            return
    except Exception as e:
        logging.warning("posthoc queue: audio service recycle failed: %s", e)
        return
    # The service binds its WS port after the models load, so an accepting
    # port means it is ready for the next trigger.
    port = int(os.getenv("DC_AUDIO_POSTHOC_WS_PORT", "9015"))
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.close()
            logging.info("posthoc queue: audio service recycled and ready")
            return
        except OSError:
            time.sleep(3)
    logging.warning("posthoc queue: audio service not accepting after recycle; "
                    "continuing anyway")


def _worker_loop():
    global _worker
    while True:
        with _lock:
            if not any(j["state"] == "queued" for j in _jobs):
                # Clear _worker UNDER the lock as we exit. Otherwise a job
                # enqueued in the window between this return and the thread
                # actually dying saw is_alive()==True, no replacement worker
                # was started, and the job sat "queued" forever. Now the exit
                # decision and enqueue's liveness check are serialized.
                _worker = None
                return
        # A live class is on: hold here rather than have the service refuse
        # us every retry_after. Re-pick under the lock afterwards — the
        # queue may have been cleared while we waited.
        _wait_for_class_to_end()
        with _lock:
            job = next((j for j in _jobs if j["state"] == "queued"), None)
            if job is None:
                _worker = None
                return
            job["state"] = "running"
            job["started_at"] = time.time()
            _persist_locked()
        try:
            _maybe_recycle_audio_service()
            _run_job(job)
            outcome, err = "done", None
        except _GpuBusy as busy:
            # Not a failure: back to the FRONT of the queue, then wait out
            # retry_after before the next attempt. Logged once per job.
            with _lock:
                job["state"] = "queued"
                job["deferrals"] = job.get("deferrals", 0) + 1
                if job in _jobs:
                    _jobs.remove(job)
                _jobs.insert(0, job)
                _persist_locked()
            if job["deferrals"] == 1:
                logging.info("posthoc queue: pod %s deferred (%s); retrying until the GPU is free",
                             job["device_id"], busy)
            time.sleep(busy.retry_after)
            continue
        except Exception as e:
            logging.warning("posthoc queue: pod %s failed: %s", job["device_id"], e)
            outcome, err = "error", str(e)
        if job.get("deferrals"):
            logging.info("posthoc queue: pod %s %s after %d deferral(s)",
                         job["device_id"], outcome, job["deferrals"])
        # Terminal transition under the lock so a persisted snapshot can't
        # capture a torn view (state without finished_at).
        with _lock:
            job["state"] = outcome
            job["error"] = err
            job["finished_at"] = time.time()
            _persist_locked()


def enqueue(session_id, device_ids, models=None):
    global _worker
    with _lock:
        queued_or_running = {j["device_id"] for j in _jobs
                             if j["state"] in ("queued", "running")}
        added = []
        for d in device_ids:
            if int(d) in queued_or_running:
                continue
            _jobs.append({"session_id": int(session_id), "device_id": int(d),
                          "state": "queued", "models": models, "error": None,
                          "queued_at": time.time(),
                          "started_at": None, "finished_at": None})
            added.append(int(d))
        _persist_locked()
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop,
                                       name="posthoc-queue", daemon=True)
            _worker.start()
    return added


def clear_pending():
    # Drop all queued jobs (running job finishes unless cancelled at the
    # service). Cancelled jobs are REMOVED, not marked error: a stop+requeue
    # cycle used to leave hundreds of phantom "cancelled" errors inflating
    # the queue panel's counts forever.
    with _lock:
        n = sum(1 for j in _jobs if j["state"] == "queued")
        _jobs[:] = [j for j in _jobs if j["state"] != "queued"]
        _persist_locked()
        return n


# Once every job is settled the queue panel has said what it had to say —
# prune the list so the panel (which renders whenever the list is non-empty)
# goes away on its own instead of showing "Finished" until the next enqueue.
# Errors linger longer so a failed overnight batch is still visible in the
# morning-ish window rather than silently vanishing.
_DONE_LINGER = 5 * 60
_ERROR_LINGER = 30 * 60


def _prune_settled_locked():
    if not _jobs or any(j["state"] in ("queued", "running") for j in _jobs):
        return
    last = max((j.get("finished_at") or 0) for j in _jobs)
    linger = _ERROR_LINGER if any(j["state"] == "error" for j in _jobs) else _DONE_LINGER
    if last and time.time() - last > linger:
        del _jobs[:]
        _persist_locked()


def status(session_id=None):
    with _lock:
        _prune_settled_locked()
        return [{"session_id": j["session_id"], "device_id": j["device_id"],
                 "state": j["state"], "error": j["error"],
                 "started_at": j.get("started_at"),
                 "finished_at": j.get("finished_at")}
                for j in _jobs
                if session_id is None or j["session_id"] == int(session_id)]


_restore()
