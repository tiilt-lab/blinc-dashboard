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

The queue itself is in Redis (keys below); the API workers enqueue, report
and clear, and only the coordinator process runs jobs (start_runner).
"""

import os
import json
import time
import logging
import threading
import asyncio
import contextlib
import uuid

import posthoc_state
import redis_sync

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


# State lives in Redis so the gunicorn API workers (enqueue/status/clear) and
# the coordinator (the one runner, coordinator.py) see one queue:
#   posthoc_queue:jobs      list of job ids, head = next to run
#   posthoc_queue:job:<id>  hash, one JSON-encoded value per job field
#   posthoc_queue:seq       INCR source of job ids
#   posthoc_queue:lock      SET NX EX mutation lock across processes
#   posthoc_queue:wake      list the runner BRPOPs on while idle; enqueue pushes
# The JSON snapshot the single-process server kept is read once by
# migrate_state_file() at coordinator start and then renamed .migrated.
_KEY_JOBS = "posthoc_queue:jobs"
_KEY_JOB = "posthoc_queue:job:%s"
_KEY_SEQ = "posthoc_queue:seq"
_KEY_LOCK = "posthoc_queue:lock"
_KEY_WAKE = "posthoc_queue:wake"
_LOCK_TTL = 10
_IDLE_WAIT = 30  # the runner re-scans this often even if a wake was lost

_lock = threading.Lock()
_worker = None


def _r():
    return redis_sync.client()


@contextlib.contextmanager
def _mutation():
    # In-process threads serialise on _lock; processes on the Redis lock. A
    # lock held past its TTL (holder died) simply expires.
    with _lock:
        r = _r()
        token = uuid.uuid4().hex
        deadline = time.monotonic() + 2 * _LOCK_TTL
        while not r.set(_KEY_LOCK, token, nx=True, ex=_LOCK_TTL):
            if time.monotonic() > deadline:
                logging.warning("posthoc queue: mutation lock held too long; proceeding")
                break
            time.sleep(0.05)
        try:
            yield
        finally:
            try:
                if r.get(_KEY_LOCK) == token:
                    r.delete(_KEY_LOCK)
            except Exception:
                pass


def _encode(job):
    return {k: json.dumps(v) for k, v in job.items()}


def _decode(raw):
    return {k: json.loads(v) for k, v in raw.items()}


def _save(job):
    # A job dict without an id (a bare _run_job call) is not persisted.
    if job.get("id") is None:
        return
    _r().hset(_KEY_JOB % job["id"], mapping=_encode(job))


def _load(job_id):
    raw = _r().hgetall(_KEY_JOB % job_id)
    return _decode(raw) if raw else None


def _all_jobs():
    # Queue order; an id whose hash is gone (manual cleanup) is dropped.
    r = _r()
    jobs = []
    for job_id in r.lrange(_KEY_JOBS, 0, -1):
        job = _load(job_id)
        if job is None:
            r.lrem(_KEY_JOBS, 0, job_id)
        else:
            jobs.append(job)
    return jobs


def _next_queued():
    return next((j for j in _all_jobs() if j["state"] == "queued"), None)


def _remove(job):
    r = _r()
    r.lrem(_KEY_JOBS, 0, job["id"])
    r.delete(_KEY_JOB % job["id"])


def _append(session_id, device_id, models, extra=None):
    r = _r()
    job = {"id": int(r.incr(_KEY_SEQ)), "session_id": int(session_id),
           "device_id": int(device_id), "state": "queued", "models": models,
           "error": None, "queued_at": time.time(),
           "started_at": None, "finished_at": None}
    job.update(extra or {})
    _save(job)
    r.rpush(_KEY_JOBS, job["id"])
    return job


def _wake():
    try:
        pipe = _r().pipeline()
        pipe.rpush(_KEY_WAKE, "1")
        pipe.ltrim(_KEY_WAKE, -10, -1)  # tokens pile up only while no runner drains them
        pipe.execute()
    except Exception as e:
        logging.warning("posthoc queue: wake failed (%s); the runner re-scans within %ss", e, _IDLE_WAIT)


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


def migrate_state_file():
    """Coordinator start, once: pending jobs from the pre-Redis JSON snapshot
    (a job caught mid-run is re-queued — runs are idempotent) go into Redis,
    then the file is renamed .migrated so it is never replayed."""
    path = _state_file()
    try:
        with open(path) as f:
            jobs = json.load(f)
    except FileNotFoundError:
        return 0
    except Exception as e:
        logging.warning("posthoc queue: could not read %s for migration: %s", path, e)
        return 0
    n = 0
    with _mutation():
        present = {j["device_id"] for j in _all_jobs() if j["state"] in ("queued", "running")}
        for j in jobs:
            if j.get("state") in ("queued", "running") and int(j["device_id"]) not in present:
                _append(j["session_id"], j["device_id"], j.get("models"),
                        {"done_legs": j.get("done_legs") or []})
                present.add(int(j["device_id"]))
                n += 1
    try:
        os.replace(path, path + ".migrated")
    except Exception as e:
        logging.warning("posthoc queue: could not rename %s: %s", path, e)
    if n:
        _wake()
        logging.info("posthoc queue: migrated %d pending job(s) from %s", n, path)
    return n


def _requeue_running():
    # The runner died mid-job (coordinator restart): run it again rather
    # than guess whether the interrupted run finished.
    with _mutation():
        n = 0
        for job in _all_jobs():
            if job["state"] == "running":
                job["state"] = "queued"
                _save(job)
                n += 1
    if n:
        logging.info("posthoc queue: re-queued %d job(s) interrupted by a restart", n)
    return n


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
    # A job may ask for a subset of legs (enqueue(..., legs=("video",)) for a
    # session that recorded video without live analytics); absent = both.
    wanted = job.get("legs") or list(LEGS)
    deadline = time.time() + _POD_TIMEOUT
    attempted, errors = [], {}
    for scope in LEGS:
        if scope in done or scope not in wanted:
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
        _save(job)
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
    """Run queued jobs until none is left, then return (coordinator only;
    _run_forever wraps this and sleeps on the wake list)."""
    while True:
        with _mutation():
            job = _next_queued()
        if job is None:
            return
        # A live class is on: hold here rather than have the service refuse
        # us every retry_after. Re-pick under the lock afterwards — the
        # queue may have been cleared while we waited.
        _wait_for_class_to_end()
        with _mutation():
            job = _next_queued()
            if job is None:
                return
            job["state"] = "running"
            job["started_at"] = time.time()
            _save(job)
        try:
            _maybe_recycle_audio_service()
            _run_job(job)
            outcome, err = "done", None
        except _GpuBusy as busy:
            # Not a failure: back to the FRONT of the queue, then wait out
            # retry_after before the next attempt. Logged once per job.
            with _mutation():
                job["state"] = "queued"
                job["deferrals"] = job.get("deferrals", 0) + 1
                _save(job)
                r = _r()
                r.lrem(_KEY_JOBS, 0, job["id"])
                r.lpush(_KEY_JOBS, job["id"])
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
        # Terminal transition under the lock so a reader can't see a torn
        # view (state without finished_at).
        with _mutation():
            job["state"] = outcome
            job["error"] = err
            job["finished_at"] = time.time()
            _save(job)


def _run_forever():
    while True:
        try:
            _worker_loop()
        except Exception:
            logging.exception("posthoc queue: runner failed; retrying in 10 s")
            time.sleep(10)
            continue
        try:
            _r().brpop(_KEY_WAKE, timeout=_IDLE_WAIT)
        except Exception as e:
            logging.warning("posthoc queue: idle wait failed (%s); sleeping %ss", e, _IDLE_WAIT)
            time.sleep(_IDLE_WAIT)


def start_runner():
    """Coordinator only: the single runner thread. API workers never call
    this; they enqueue, read status and clear."""
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return _worker
    _requeue_running()
    with _lock:
        _worker = threading.Thread(target=_run_forever, name="posthoc-queue", daemon=True)
        _worker.start()
    return _worker


LEGS = ("audio", "video")


def _legs_extra(legs):
    # None = both legs (the historic job); otherwise an ordered subset, e.g.
    # ("video",) for a record-only session whose live ASR already ran.
    if legs is None:
        return None
    chosen = [s for s in LEGS if s in set(legs)]
    if not chosen or len(chosen) != len(set(legs)):
        raise ValueError("legs must be a non-empty subset of %s, got %r" % (LEGS, legs))
    return {"legs": chosen}


def enqueue(session_id, device_ids, models=None, legs=None):
    extra = _legs_extra(legs)
    with _mutation():
        queued_or_running = {j["device_id"] for j in _all_jobs()
                             if j["state"] in ("queued", "running")}
        added = []
        for d in device_ids:
            if int(d) in queued_or_running:
                continue
            _append(session_id, d, models, extra)
            added.append(int(d))
    if added:
        _wake()
    return added


def clear_pending():
    # Drop all queued jobs (running job finishes unless cancelled at the
    # service). Cancelled jobs are REMOVED, not marked error: a stop+requeue
    # cycle used to leave hundreds of phantom "cancelled" errors inflating
    # the queue panel's counts forever.
    with _mutation():
        n = 0
        for job in _all_jobs():
            if job["state"] == "queued":
                _remove(job)
                n += 1
        return n


# Once every job is settled the queue panel has said what it had to say —
# prune the list so the panel (which renders whenever the list is non-empty)
# goes away on its own instead of showing "Finished" until the next enqueue.
# Errors linger longer so a failed overnight batch is still visible in the
# morning-ish window rather than silently vanishing.
_DONE_LINGER = 5 * 60
_ERROR_LINGER = 30 * 60


def _settled_expired(jobs):
    if not jobs or any(j["state"] in ("queued", "running") for j in jobs):
        return False
    last = max((j.get("finished_at") or 0) for j in jobs)
    linger = _ERROR_LINGER if any(j["state"] == "error" for j in jobs) else _DONE_LINGER
    return bool(last) and time.time() - last > linger


def _prune_settled():
    # Cheap read first; the lock is only taken when there is something to drop.
    if not _settled_expired(_all_jobs()):
        return
    with _mutation():
        jobs = _all_jobs()
        if _settled_expired(jobs):
            for job in jobs:
                _remove(job)


def status(session_id=None):
    _prune_settled()
    return [{"session_id": j["session_id"], "device_id": j["device_id"],
             "state": j["state"], "error": j["error"],
             "started_at": j.get("started_at"),
             "finished_at": j.get("finished_at")}
            for j in _all_jobs()
            if session_id is None or j["session_id"] == int(session_id)]
