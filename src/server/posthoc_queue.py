"""Server-side queue for running full post-hoc analyses on multiple pods in
sequence. Jobs survive browser refreshes because the server both triggers the
runs (same WS protocol as the UI) and observes completion via posthoc_state
(fed by the services' reset/completed callbacks). One pod at a time; within a
pod, audio and video run concurrently as usual.
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


def _trigger(url, init, start_type):
    # Fire a run: connect, Initialize, wait for the readiness ack, send start.
    # The socket then closes; the run continues in the background (services are
    # disconnect-resilient) and completion is observed via posthoc_state.
    import websockets

    async def go():
        async with websockets.connect(url, open_timeout=15) as ws:
            await ws.send(json.dumps(init))
            deadline = time.time() + 120
            while time.time() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                t = msg.get("type", "")
                if t == "init posthoc analytics completed":
                    await ws.send(json.dumps({"type": start_type}))
                    return None
                if t == "error":
                    return msg.get("message", "service error")
            return "timed out waiting for init ack"

    return asyncio.run(go())


def _run_job(job):
    import app as A
    with A.app.app_context():
        import database
        session = database.get_sessions(id=job["session_id"])
        device_id = job["device_id"]
        speakers = [{"id": s.id, "alias": s.get_alias()}
                    for s in database.get_speakers(session_device_id=device_id)]
        # The services refuse any pod-naming message without a ticket minted
        # for that pod (common/posthoc_ticket); mint one exactly as the UI does.
        from redis_helper import RedisPosthocTicket
        base = {
            "ticket": RedisPosthocTicket.mint(device_id),
            "sessionid": job["session_id"],
            "sessiondeviceid": device_id,
            "server_start": session.creation_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "keywords": (session.json() or {}).get("keywords") or [],
            "speakers": speakers,
        }
    models = job.get("models") or {}
    err_a = _trigger(AUDIO_WS, dict(base,
                                    type="Initialize_audio_processing_analytics",
                                    asr=models.get("asr", "whisperx"),
                                    scorer=models.get("scorer"),
                                    diarizer=models.get("diarizer", "pyannote"),
                                    embedder=models.get("embedder")),
                     "start_posthoc_audio_processing")
    err_v = _trigger(VIDEO_WS, dict(base, type="Initialize_video_processing_analytics"),
                     "start_posthoc_video_processing")
    if err_a:
        logging.warning("posthoc queue: pod %s audio trigger: %s", job["device_id"], err_a)
    if err_v:
        logging.warning("posthoc queue: pod %s video trigger: %s", job["device_id"], err_v)
    if err_a and err_v:
        raise RuntimeError("audio: %s / video: %s" % (err_a, err_v))
    # Wait for completion: the reset callback marks the device running; the
    # completed callbacks clear it. Require it to have been seen running first.
    deadline = time.time() + _POD_TIMEOUT
    seen_running = False
    while time.time() < deadline:
        running = posthoc_state.is_running(device_id)
        if running:
            seen_running = True
        elif seen_running:
            # Finished — verify the audio leg actually produced output. A silent
            # failure (OOM, empty recording) completes with zero transcripts and
            # must surface as an error, not "Analyzed".
            if not err_a:
                import app as A2
                with A2.app.app_context():
                    import database as db2
                    if db2.get_pod_duration(device_id) is None:
                        # The completion callback already stamped the pod
                        # analyzed; undo that so the failure stays visible
                        # and retries don't skip it.
                        db2.clear_session_device_posthoc(device_id)
                        raise RuntimeError("audio produced no transcripts (empty recording or transcription failure)")
            return  # ran and finished
        time.sleep(10)
    raise RuntimeError("timed out waiting for pod %s to finish" % device_id)


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
            job = next((j for j in _jobs if j["state"] == "queued"), None)
            if job is None:
                # Clear _worker UNDER the lock as we exit. Otherwise a job
                # enqueued in the window between this return and the thread
                # actually dying saw is_alive()==True, no replacement worker
                # was started, and the job sat "queued" forever. Now the exit
                # decision and enqueue's liveness check are serialized.
                _worker = None
                return
            job["state"] = "running"
            job["started_at"] = time.time()
            _persist_locked()
        try:
            _maybe_recycle_audio_service()
            _run_job(job)
            outcome, err = "done", None
        except Exception as e:
            logging.warning("posthoc queue: pod %s failed: %s", job["device_id"], e)
            outcome, err = "error", str(e)
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
