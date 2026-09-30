"""CrisperWhisper 2.0 connectors (verbatim ASR with crisp word timestamps).

CrisperWhisper (nyra health) is a Whisper fine-tune that transcribes
*verbatim* — fillers, stutters, false starts — with word timestamps accurate
enough to measure pauses, which regular Whisper glues onto neighboring
words. Weights are non-commercial-research licensed; inference code is MIT.

The package needs its own CTranslate2 fork, so it lives in src/venv-crisper
and all inference goes through crisper_worker.py subprocesses:
  - CrisperWhisperASR (live): every pod's windows go through one process-
    wide WorkerPool of DC_ASR_WORKERS (default 2) persistent --serve workers,
    each a ~3-4GB model copy on the GPU. A window transcribes in ~1s and a
    pod produces one per 12s, so one worker saturates at ~10-12 talking
    pods; the second slot is spawned lazily — only once a window has waited
    on a busy first slot — and only while the card still has
    MIN_FREE_VRAM_MIB free. Windows are dispatched FIFO across pods (no pod
    can hog a slot); a pod's own windows stay in order because its flush
    thread is sequential. A slot is reaped after 15 idle minutes to give
    the GPU memory back. A failed spawn or a dead worker opens that slot's
    circuit breaker (SpawnBackoff, 30s -> 300s); a window fails over to
    another slot at once, and the pods are told ASR is degraded only when
    no slot can serve, ok again as soon as a window succeeds.
    Two throughput levers sit in front of the pool: a voice-activity gate
    (asr_ingest.SpeechGate, DC_ASR_VAD) drops windows with no speech before
    they cost a GPU second, and when several windows are waiting for a busy
    slot the FIFO head takes up to DC_ASR_BATCH of them to the next free
    worker as ONE request (the worker runs them back to back; per-pod order
    is untouched because a pod never has two windows in flight).
    DC_ASR_MODEL picks the worker's model (see live_model).
  - CrisperWhisperPosthocASR: --oneshot on the whole recording in its own
    process (so a long file can never head-of-line-block live captions),
    then emits gap-segmented Google-shaped AsrResults (same contract as
    Qwen3ASR; speaker attribution happens downstream via fingerprints).
"""
import functools
import json
import logging
import os
import queue as queue_module
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque

from .base_asr import (BaseASR, AsrResult, PosthocFileASR, worker_python,
                       run_json_worker, POSTHOC_WORKER_TIMEOUT)
from asr_ingest import WindowAssembler, make_speech_gate  # audio_processing/ is on sys.path

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crisper_worker.py")
# Per-slot worker stderr, appended for the life of the service:
# audio_processing/crisper_worker.<slot>.stderr.log. Under audio_processing/
# so the existing logrotate rule (*.log, copytruncate) covers it; a death
# used to be unrecordable because stderr was DEVNULL.
_STDERR_LOG_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "crisper_worker.%d.stderr.log")

DEFAULT_MODEL = "nyralabs/CrisperWhisper2.0_large"

# Live worker model: DC_ASR_MODEL overrides config.ini [crisperwhisper] model.
# The candidate is nyralabs/CrisperWhisper2.0_turbo, a Whisper-turbo-style
# distillation: the same large encoder with a much shallower decoder, so it
# trades some accuracy for speed. Expected: several times faster decode per
# window (the decoder is the serial part) and roughly half the VRAM, against
# a somewhat higher WER — the verbatim details this model exists for
# (fillers, stutters, false starts) and word-timestamp precision (fewer
# cross-attention alignment heads) are what a shallow decoder loses first,
# and noisy far-field classroom audio suffers most. Measure pause metrics
# on a real class before switching one over; nothing changes by default.
MODEL_ENV = "DC_ASR_MODEL"

# Batch: how many waiting windows the FIFO head may take to a free worker as
# one request. 1 = one window per request (the pre-batching behaviour).
BATCH_ENV = "DC_ASR_BATCH"
DEFAULT_BATCH = 4

# Refuse to spawn when the GPU has less than this free: the model needs ~4GB
# and a spawn that OOMs takes the 180s start timeout under the slot's lock.
# Checked at every spawn, so a second slot never lands on a full card.
MIN_FREE_VRAM_MIB = 3500

# Live worker pool size. 1 is exactly the pre-pool single shared worker.
WORKERS_ENV = "DC_ASR_WORKERS"
DEFAULT_WORKERS = 2

# One window's budget: the worker's reply deadline, and also the longest a
# window waits in the pool's FIFO for a free slot (past it the window is
# dropped rather than served minutes stale). A batched request gets
# BATCH_EXTRA_SECONDS more per additional window.
REQUEST_TIMEOUT = 120.0
BATCH_EXTRA_SECONDS = 30.0
SPAWN_TIMEOUT = 180.0

# "Capacity reached" signal: a window waited longer than CAPACITY_WAIT_SECONDS
# for a free slot. Logged at WARNING, at most once per CAPACITY_WARNING_INTERVAL.
# The load generator greps for this exact text.
CAPACITY_WARNING = "CrisperWhisper: ASR capacity reached"
CAPACITY_WAIT_SECONDS = 5.0
CAPACITY_WARNING_INTERVAL = 60.0

# Short human text for the pod client's asr_status banner.
MSG_RESTARTING = "Live captions paused: the speech recognizer is restarting."
MSG_UNRESPONSIVE = "Live captions paused: the speech recognizer stopped responding."
MSG_LOW_VRAM = "Live captions paused: the server's GPU memory is low."


def _worker_python():
    # venv-crisper lives in src/ (3 dirs above asr_connectors/); shared resolver.
    return worker_python("venv-crisper", 3)


def pool_size(environ=os.environ):
    """DC_ASR_WORKERS (default 2, minimum 1)."""
    raw = environ.get(WORKERS_ENV, str(DEFAULT_WORKERS))
    try:
        return max(1, int(raw))
    except ValueError:
        logging.warning("%s=%r is not an integer; using %d", WORKERS_ENV, raw, DEFAULT_WORKERS)
        return DEFAULT_WORKERS


def batch_size(environ=os.environ):
    """DC_ASR_BATCH (default 4, minimum 1)."""
    raw = environ.get(BATCH_ENV, str(DEFAULT_BATCH))
    try:
        return max(1, int(raw))
    except ValueError:
        logging.warning("%s=%r is not an integer; using %d", BATCH_ENV, raw, DEFAULT_BATCH)
        return DEFAULT_BATCH


def live_model(environ=os.environ):
    """The live worker's model id: DC_ASR_MODEL, else config.ini, else DEFAULT_MODEL."""
    override = (environ.get(MODEL_ENV) or "").strip()
    if override:
        return override
    try:
        import config as cf
        return cf.crisperwhisper_model()
    except Exception:
        return DEFAULT_MODEL


def free_vram_mib(run=subprocess.run):
    """Free GPU memory in MiB per nvidia-smi, or None when unknown (no tool,
    timeout, unparsable) — unknown must not block spawning."""
    try:
        out = run(["nvidia-smi", "--query-gpu=memory.free",
                   "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=5)
        values = [int(line.strip()) for line in out.stdout.splitlines() if line.strip()]
        return max(values) if values else None
    except Exception:
        return None


class SpawnBackoff:
    """Circuit breaker for (re)spawning one worker slot.

    Each failure doubles the wait before the next attempt (base -> cap); a
    successful transcription resets it. Pure state so it is unit-testable:
    the caller never sleeps on it — while ``may_attempt`` is False it skips
    the window and reports ASR as degraded.
    """

    def __init__(self, base=30.0, cap=300.0):
        self.base = base
        self.cap = cap
        self.failures = 0
        self.not_before = 0.0

    def next_delay(self):
        return min(self.cap, self.base * (2 ** self.failures))

    def record_failure(self, now):
        delay = self.next_delay()
        self.failures += 1
        self.not_before = now + delay
        return delay

    def record_success(self):
        self.failures = 0
        self.not_before = 0.0

    def may_attempt(self, now):
        return now >= self.not_before

    def retry_in(self, now):
        return max(0.0, self.not_before - now)


class WorkerUnavailable(RuntimeError):
    """Raised at once (never after a wait) while the spawn breaker is open,
    and by the pool when no slot can serve or none frees up within
    REQUEST_TIMEOUT."""


def _split_segments(words, max_gap=1.0, max_len=15.0):
    """Group a flat word list into utterance-ish segments on silent gaps.

    CrisperWhisper's word ends are actual speech ends (pauses aren't glued
    onto words), so gap-splitting gives natural utterance boundaries.
    words: [[text, start_s, end_s], ...] -> list of word-lists.
    """
    segments = []
    current = []
    for w in words:
        if current and (w[1] - current[-1][2] > max_gap or w[2] - current[0][1] > max_len):
            segments.append(current)
            current = []
        current.append(w)
    if current:
        segments.append(current)
    return segments


def _emit_segments(transcript_queue, words, offset=0.0):
    for seg in _split_segments(words):
        text = " ".join(w[0].strip() for w in seg).strip()
        triples = [(w[0].strip(), offset + w[1], offset + w[2]) for w in seg]
        if text and triples:
            transcript_queue.put(AsrResult(text, triples))


class _StatusSource:
    """ok/degraded state with listener fan-out; base of a slot and the pool.

    Own lock: listeners are cheap (reactor-marshalled sends) but must never
    need a worker lock. ``_set_state`` fires the listeners once per change
    (one message per pod, however many windows fail meanwhile).
    """

    def __init__(self):
        self._listeners = []
        self._listeners_lock = threading.Lock()
        self._state = ("ok", None)

    @property
    def state(self):
        return self._state[0]

    def add_listener(self, callback):
        """callback(state, message) on every change; a pod that joins during
        an outage is told 'degraded' immediately."""
        with self._listeners_lock:
            self._listeners.append(callback)
            state = self._state
        if state[0] != "ok":
            callback(*state)

    def remove_listener(self, callback):
        with self._listeners_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _set_state(self, state, message=None):
        with self._listeners_lock:
            if (state, message) == self._state:
                return
            changed = state != self._state[0]
            self._state = (state, message)
            listeners = list(self._listeners)
        self._log_state(state, message, changed)
        for callback in listeners:
            try:
                callback(state, message)
            except Exception as e:
                logging.debug("asr_status listener failed: %s", e)

    def _log_state(self, state, message, changed):
        pass


class _WorkerSlot(_StatusSource):
    """One persistent CrisperWhisper --serve subprocess: a pool slot.

    The model is identical and read-only across pods, so one subprocess
    (one ~3-4GB GPU copy, one ~7s load) serves any of them. Requests are
    serialized under the slot lock — the worker is single-threaded anyway,
    and doing send+readline as one unit keeps the pipe protocol in sync
    with no request-ID bookkeeping. A dead or unspawnable worker opens the
    slot's SpawnBackoff breaker: windows are refused (WorkerUnavailable,
    raised at once — the lock is never held across a wait) and the pool
    fails over to another slot or tells the pods ASR is degraded. After
    IDLE_SHUTDOWN_SECONDS without a window the worker is shut down to give
    the GPU memory back; the next window relaunches it transparently.
    With DC_ASR_WORKERS=1 this is exactly the old single shared worker.
    """

    IDLE_SHUTDOWN_SECONDS = 900

    def __init__(self, index=0):
        super().__init__()
        self.index = index
        self.name = "CrisperWhisper worker %d" % index
        self.stderr_log = _STDERR_LOG_TEMPLATE % index
        self._stderr_fh = None
        self._lock = threading.Lock()
        self._proc = None
        self._last_used = 0.0
        self._reaper_started = False
        self.backoff = SpawnBackoff()
        # From the worker's ready line: may this worker take the list form
        # (several windows per request)? False until a worker says so, so an
        # older worker script keeps getting one window per request.
        self.batch_capable = False

    @property
    def alive(self):
        """A worker process exists (it may have died unnoticed; the next
        request's _ensure_proc finds out and opens the breaker)."""
        return self._proc is not None

    def _log_state(self, state, message, changed):
        # A failure is already WARNed with its cause by _failed().
        if state == "ok":
            logging.info("%s: serving again", self.name)

    # -- stderr log ---------------------------------------------------------

    def _stderr_file(self):
        if self._stderr_fh is None:
            self._stderr_fh = open(self.stderr_log, "ab", buffering=0)
        return self._stderr_fh

    def _stderr_tail(self, nbytes=600):
        """Last lines the worker wrote, for the failure WARNING."""
        try:
            with open(self.stderr_log, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - nbytes))
                return f.read().decode("utf8", "replace").strip()
        except OSError:
            return ""

    def _failed(self, reason, client_message):
        # Breaker + one WARNING carrying the worker's last stderr lines (a
        # death finally has a recorded cause) + degraded status to the pool.
        delay = self.backoff.record_failure(time.time())
        tail = self._stderr_tail()
        logging.warning("%s: %s; next spawn attempt in %.0fs%s",
                        self.name, reason, delay,
                        "\n--- worker stderr tail ---\n" + tail if tail else "")
        self._set_state("degraded", client_message)

    # -- lifecycle (all callers hold self._lock) ---------------------------

    def _read_reply(self, timeout):
        # Bounded readline. A wedged worker (hung model load, CUDA stall)
        # used to block readline forever WHILE HOLDING THE SHARED LOCK,
        # deadlocking live transcription for every pod at once. On timeout
        # the caller kills the worker and the retry path takes over. The
        # worker writes whole JSON lines, so select-then-readline won't
        # block on a partial line in practice.
        import select
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError(
                    "CrisperWhisper worker unresponsive for %.0fs" % timeout)
            ready, _, _ = select.select([self._proc.stdout], [], [],
                                        min(remaining, 5))
            if ready:
                return self._proc.stdout.readline()

    def _launch(self):
        """Popen the --serve worker with its stderr appended to this slot's log."""
        import config as cf
        model = live_model()
        mode = cf.crisperwhisper_mode()
        logging.info("Starting %s (model=%s, mode=%s)", self.name, model, mode)
        err = self._stderr_file()
        err.write(("---- %s spawn model=%s mode=%s ----\n"
                   % (time.strftime("%Y-%m-%d %H:%M:%S"), model, mode)).encode())
        return subprocess.Popen(
            [_worker_python(), _WORKER, "--serve", "--model", model, "--mode", mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err, text=True, bufsize=1)

    def _ensure_proc(self):
        if self._proc is not None:
            rc = self._proc.poll()
            if rc is None:
                return
            # Died between requests (OOM-kill, CUDA fault): same breaker as
            # a failed spawn, so a crash loop can't cost a 7s model load
            # under the slot lock for every window of every pod.
            self._proc = None
            self._failed("worker exited unexpectedly (rc=%s)" % rc, MSG_RESTARTING)
        now = time.time()
        if not self.backoff.may_attempt(now):
            raise WorkerUnavailable("%s: restart backoff, %.0fs left"
                                    % (self.name, self.backoff.retry_in(now)))
        free = free_vram_mib()
        if free is not None and free < MIN_FREE_VRAM_MIB:
            self._failed("only %d MiB of GPU memory free (need %d)"
                         % (free, MIN_FREE_VRAM_MIB), MSG_LOW_VRAM)
            raise WorkerUnavailable("%s: GPU memory low (%d MiB free)" % (self.name, free))
        ok = False
        hello = {}
        try:
            self._proc = self._launch()
            ready = self._read_reply(timeout=SPAWN_TIMEOUT)
            hello = json.loads(ready) if ready else {}
            ok = isinstance(hello, dict) and bool(hello.get("ready"))
            reason = "worker exited during model load"
        except Exception as e:
            reason = "spawn failed: %s" % e
        if not ok:
            self._kill()
            self._failed(reason, MSG_RESTARTING)
            raise WorkerUnavailable("%s: %s" % (self.name, reason))
        self.batch_capable = bool(hello.get("batch"))
        logging.info("%s ready (pid %s, protocol %s, batch %s)", self.name,
                     getattr(self._proc, "pid", "?"), hello.get("protocol", 1),
                     "yes" if self.batch_capable else "no")
        if not self._reaper_started:
            self._reaper_started = True
            threading.Thread(target=self._reap_idle, daemon=True,
                             name="crisper-idle-reaper-%d" % self.index).start()

    def _kill(self):
        proc, self._proc = self._proc, None
        self.batch_capable = False
        if proc:
            try:
                proc.kill()
            except Exception:
                pass

    def _shutdown(self):
        if self._proc is None:
            return
        try:
            self._proc.stdin.write(json.dumps({"exit": True}) + "\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=5)
            self._proc = None
            logging.info("%s exited", self.name)
        except Exception:
            self._kill()

    def _reap_idle(self):
        while True:
            time.sleep(60)
            with self._lock:
                if (self._proc is not None
                        and time.time() - self._last_used > self.IDLE_SHUTDOWN_SECONDS):
                    logging.info("%s idle >%ds — shutting down to free GPU memory",
                                 self.name, self.IDLE_SHUTDOWN_SECONDS)
                    self._shutdown()

    # -- API ----------------------------------------------------------------

    def warm(self):
        """Preload the model so a session's first window isn't slow."""
        try:
            with self._lock:
                self._ensure_proc()
        except WorkerUnavailable as e:
            logging.info("CrisperWhisper warm-up skipped: %s", e)  # already WARNed by _failed
        except Exception as e:
            logging.warning("CrisperWhisper warm-up failed: %s", e)

    def transcribe(self, wav_path):
        return self.transcribe_many([wav_path])[0]

    def transcribe_many(self, wav_paths):
        """Transcribe several windows in one worker call; one result dict per
        window, in order. On the wire that is one list-form request when the
        worker advertised batching, else one request per window under the
        same lock (still no other pod's window can interleave)."""
        wav_paths = list(wav_paths)
        with self._lock:
            self._last_used = time.time()
            # Raises WorkerUnavailable at once while the breaker is open: the
            # old retry-once-and-respawn loop is what turned one bad spawn
            # into a 180s stall for every pod, once per window.
            self._ensure_proc()
            try:
                if len(wav_paths) == 1 or not self.batch_capable:
                    results = [self._request({"audio": path}, REQUEST_TIMEOUT)
                               for path in wav_paths]
                else:
                    results = self._request_batch(wav_paths)
            except Exception as e:
                self._kill()
                self._failed("request failed: %s" % e, MSG_UNRESPONSIVE)
                raise
            self._last_used = time.time()
            # Only a served window proves the worker healthy (a spawn that
            # then faults on inference must keep growing the backoff).
            self.backoff.record_success()
            self._set_state("ok")
            return results

    def _request(self, job, timeout):
        self._proc.stdin.write(json.dumps(job) + "\n")
        self._proc.stdin.flush()
        line = self._read_reply(timeout=timeout)
        if not line:
            raise RuntimeError("worker closed its pipe")
        return json.loads(line)

    def _request_batch(self, wav_paths):
        windows = [{"id": i, "audio": path} for i, path in enumerate(wav_paths)]
        reply = self._request({"windows": windows},
                              REQUEST_TIMEOUT + BATCH_EXTRA_SECONDS * (len(windows) - 1))
        results = reply.get("results") if isinstance(reply, dict) else None
        if results is None:
            if isinstance(reply, dict) and reply.get("error"):
                # The worker refused the request as a whole (it is still
                # healthy): every window gets that error, none is lost silently.
                return [{"error": reply["error"]} for _ in wav_paths]
            raise RuntimeError("malformed batch reply: %r" % (reply,))
        if len(results) != len(wav_paths):
            raise RuntimeError("batch reply has %d results for %d windows"
                               % (len(results), len(wav_paths)))
        return results


class _Ticket:
    """One window waiting in the pool's FIFO.

    The head ticket becomes the *leader*: it takes a slot and may claim the
    tickets right behind it as *followers* for the same request. A claimed
    follower leaves the FIFO and waits for the leader to fill in its result
    (or error); its thread never touches a slot.
    """

    __slots__ = ("wav_path", "t0", "claimed", "done", "result", "error")

    def __init__(self, wav_path):
        self.wav_path = wav_path
        self.t0 = time.monotonic()
        self.claimed = False
        self.done = False
        self.result = None
        self.error = None


class WorkerPool(_StatusSource):
    """Process-wide pool of live CrisperWhisper worker slots.

    Dispatch: a window takes the lowest free slot that already has a live
    worker. When every live slot is busy it waits in one FIFO shared by all
    pods, bounded by ``request_timeout``, so arrival order across pods is
    honoured and no pod can hog a slot (each pod's flush thread submits one
    window at a time, so per-pod order is preserved whichever slot serves).
    Batching: when the head of the FIFO takes a LIVE slot whose worker
    advertised the list form, it also takes up to ``batch`` - 1 windows
    waiting behind it, in FIFO order, and the slot sends them as one
    request; each result goes back to its own pod's thread. A pod has at
    most one window in flight, so a batch never holds two windows of one
    pod and per-pod order cannot change. A slot that still has to spawn
    takes one window (the others may find another slot meanwhile).
    Spawning is lazy: slot 0 starts on the first window; a further slot
    starts only once a window has waited SPAWN_GRACE_SECONDS on busy slots
    (or when no live slot can take it at all), and every spawn re-checks
    free VRAM, so a second ~3-4GB worker never lands on a full card. A slot
    that refuses (breaker open, spawn refused) hands the window — batch and
    all — to another slot at once; when no slot can serve,
    WorkerUnavailable is raised without waiting.

    Status: degraded only when every slot is degraded; ok as soon as any
    slot serves a window. Same add_listener/remove_listener API as before.
    """

    SPAWN_GRACE_SECONDS = 2.0

    def __init__(self, size=None, slot_factory=None, request_timeout=REQUEST_TIMEOUT,
                 spawn_grace=None, capacity_wait=CAPACITY_WAIT_SECONDS,
                 warning_interval=CAPACITY_WARNING_INTERVAL, batch=None):
        super().__init__()
        self.size = pool_size() if size is None else max(1, int(size))
        self.batch = batch_size() if batch is None else max(1, int(batch))
        factory = slot_factory or _WorkerSlot
        self.slots = [factory(i) for i in range(self.size)]
        for slot in self.slots:
            slot.add_listener(functools.partial(self._slot_changed, slot))
        self.request_timeout = float(request_timeout)
        self.spawn_grace = (self.SPAWN_GRACE_SECONDS if spawn_grace is None
                            else float(spawn_grace))
        self.capacity_wait = float(capacity_wait)
        self.warning_interval = float(warning_interval)
        self._cv = threading.Condition()
        self._waiters = deque()   # FIFO of tickets; only the head may take a slot
        self._busy = set()        # slots currently serving (or spawning for) a window
        self._last_capacity_warning = None
        self._max_wait = 0.0      # longest FIFO wait since the last DEBUG line
        self._requests = 0        # worker requests since the last DEBUG line...
        self._windows = 0         # ...and the windows they carried
        self._monitor_started = False

    # -- status -------------------------------------------------------------

    def _slot_changed(self, slot, state, message):
        if all(s.state != "ok" for s in self.slots):
            self._set_state("degraded", message)
        else:
            self._set_state("ok")

    def _log_state(self, state, message, changed):
        if state != "ok":
            logging.log(logging.WARNING if changed else logging.INFO,
                        "ASR degraded: %s", message)
        else:
            logging.warning("ASR recovered: a CrisperWhisper worker is serving again")

    # -- dispatch -----------------------------------------------------------

    @property
    def waiting(self):
        return len(self._waiters)

    def _serviceable(self, slot, exclude):
        # Could this slot (still) serve a window: serving/spawning now, has a
        # worker, or its breaker allows a spawn attempt.
        return (slot not in exclude
                and (slot in self._busy or slot.alive
                     or slot.backoff.may_attempt(time.time())))

    def _pick(self, exclude, waited):
        """The slot for the head waiter, or None to keep waiting (caller holds _cv)."""
        free = [s for s in self.slots if s not in self._busy and s not in exclude]
        for slot in free:
            if slot.alive:
                return slot
        now = time.time()
        spawnable = [s for s in free if s.backoff.may_attempt(now)]
        if not spawnable:
            return None
        # Lazy spawn: while a live slot is busy, wait a beat for it rather
        # than start another ~3-4GB worker for a momentary collision.
        if waited < self.spawn_grace and any(
                s not in exclude and (s in self._busy or s.alive) for s in self.slots):
            return None
        return spawnable[0]

    def _acquire(self, ticket, exclude, followers):
        """Wait for a slot as the leader (returned, ``followers`` topped up
        with the claimed tickets), or, if a leader claimed this ticket, until
        its result is in (returns None). Caller: transcribe()."""
        t0 = time.monotonic()
        deadline = t0 + self.request_timeout
        with self._cv:
            self._start_monitor()
            self._waiters.append(ticket)
            try:
                while True:
                    if ticket.claimed:
                        # In a leader's batch: the reply (bounded by the
                        # slot's request timeout) or the leader's failure
                        # resolves it; nothing to pick here.
                        while not ticket.done:
                            self._cv.wait(1.0)
                        return None
                    now = time.monotonic()
                    waited = now - t0
                    if self._waiters[0] is ticket:
                        slot = self._pick(exclude, waited)
                        if slot is not None:
                            self._busy.add(slot)
                            self._note_wait(waited)
                            self._claim_followers(slot, followers, now)
                            return slot
                        if not any(self._serviceable(s, exclude) for s in self.slots):
                            # Today's contract: refused at once, never after a wait.
                            raise WorkerUnavailable(
                                "no CrisperWhisper worker can serve (%d slot%s, breakers open)"
                                % (self.size, "" if self.size == 1 else "s"))
                    if now >= deadline:
                        self._note_wait(waited)
                        raise WorkerUnavailable(
                            "no free CrisperWhisper worker within %.0fs (%d windows waiting)"
                            % (self.request_timeout, len(self._waiters)))
                    # Short slices: breakers close and the spawn grace elapses
                    # on the clock, not on a notify.
                    timeout = min(1.0, deadline - now)
                    if waited < self.spawn_grace:
                        timeout = min(timeout, self.spawn_grace - waited)
                    self._cv.wait(max(0.0, timeout))
            finally:
                if ticket in self._waiters:   # a claimed follower is already out
                    self._waiters.remove(ticket)
                self._cv.notify_all()

    def _claim_followers(self, slot, followers, now):
        # Caller holds _cv and is the head of the FIFO, about to use `slot`.
        # Only a live worker that speaks the list form gets a batch; a slot
        # that must spawn first takes one window so the rest stay available
        # to any other slot that frees up during the ~7s load.
        if not (slot.alive and slot.batch_capable):
            return
        room = self.batch - 1 - len(followers)
        while room > 0 and len(self._waiters) > 1:
            follower = self._waiters[1]
            self._waiters.remove(follower)
            follower.claimed = True
            self._note_wait(now - follower.t0)
            followers.append(follower)
            room -= 1

    def _resolve(self, followers, results=None, error=None):
        # Hand each follower its result (or the batch's failure) and wake it.
        with self._cv:
            for i, follower in enumerate(followers):
                if error is not None:
                    follower.error = type(error)(*error.args)   # its own instance to raise
                else:
                    follower.result = results[i]
                follower.done = True
            self._cv.notify_all()

    def _release(self, slot):
        with self._cv:
            self._busy.discard(slot)
            self._cv.notify_all()

    def _note_wait(self, waited):
        # Caller holds _cv. The rate-limited WARNING is the load generator's
        # "capacity reached" signal (CAPACITY_WARNING).
        self._max_wait = max(self._max_wait, waited)
        if waited < self.capacity_wait:
            return
        now = time.monotonic()
        if (self._last_capacity_warning is not None
                and now - self._last_capacity_warning < self.warning_interval):
            return
        self._last_capacity_warning = now
        logging.warning("%s: a window waited %.1fs for a free worker "
                        "(%d waiting, %d/%d slots busy)",
                        CAPACITY_WARNING, waited, len(self._waiters),
                        len(self._busy), self.size)

    def _start_monitor(self):
        # Caller holds _cv. One DEBUG line a minute with the queue depth.
        if self._monitor_started:
            return
        self._monitor_started = True
        threading.Thread(target=self._monitor, daemon=True,
                         name="crisper-pool-monitor").start()

    def _monitor(self):
        while True:
            time.sleep(60)
            with self._cv:
                waiting, busy, longest = len(self._waiters), len(self._busy), self._max_wait
                requests, windows = self._requests, self._windows
                self._max_wait = 0.0
                self._requests = self._windows = 0
            alive = sum(1 for s in self.slots if s.alive)
            logging.debug("CrisperWhisper pool: %d waiting, %d/%d slots busy, %d alive, "
                          "longest wait %.1fs, %d windows in %d requests in the last minute",
                          waiting, busy, self.size, alive, longest, windows, requests)

    # -- API ----------------------------------------------------------------

    def warm(self):
        """Preload slot 0 (the others spawn on demand)."""
        self.slots[0].warm()

    def transcribe(self, wav_path):
        """This window's result dict, however it got served: on its own, as
        the leader of a batch, or inside another pod's batch."""
        ticket = _Ticket(wav_path)
        tried = set()
        followers = []
        try:
            while True:
                slot = self._acquire(ticket, tried, followers)
                if slot is None:
                    break                      # served as a follower
                paths = [ticket.wav_path] + [f.wav_path for f in followers]
                try:
                    results = slot.transcribe_many(paths)
                except WorkerUnavailable:
                    # Refused before any work (breaker open / spawn refused):
                    # another slot may take this same batch.
                    tried.add(slot)
                    if len(tried) >= self.size:
                        raise
                    continue
                finally:
                    self._release(slot)
                with self._cv:
                    self._requests += 1
                    self._windows += len(paths)
                self._resolve(followers, results=results[1:])
                followers = []
                return results[0]
        except BaseException as e:
            # Whatever stopped the leader stops its followers the same way
            # (their threads are waiting in _acquire; never leave them there).
            if isinstance(e, WorkerUnavailable):
                self._resolve(followers, error=WorkerUnavailable(str(e)))
            else:
                self._resolve(followers, error=RuntimeError("batch request failed: %s" % e))
            raise
        if ticket.error is not None:
            raise ticket.error
        return ticket.result


_pool = WorkerPool()


class CrisperWhisperASR(BaseASR):
    """Live connector: fixed windows against the worker pool.

    Two threads: ``_processing`` only drains the ingest queue and assembles
    windows (so a slow or saturated pool can never stop it draining, which is
    what made the reactor's queue fill and evict audio), and ``_transcribing``
    feeds completed windows to the pool one at a time, so this pod's windows
    are transcribed in order whichever slot serves them. Window start times
    come from the chunks' absolute sample offsets (asr_ingest), not from how
    much audio survived the queue, so transcript times match AudioBuffer.

    Before a window goes to the pool the flush thread asks this pod's
    SpeechGate (asr_ingest; DC_ASR_VAD) whether it holds any speech; a
    silent window is skipped — counted, reported once a minute, and exposed
    as ``vad_skip_ratio`` — and the next window keeps its own absolute start,
    so the timeline never drifts. The gate is built lazily on the flush
    thread (a Silero session per pod) and any failure in it fails OPEN.
    """

    WINDOW_SECONDS = 12.0
    # ~2 min of audio waiting on the pool; beyond that the OLDEST window is
    # dropped so captions stay live rather than minutes stale.
    MAX_PENDING_WINDOWS = 10

    def __init__(self, audio_queue, transcript_queue, config, media_type, interval):
        super().__init__(audio_queue, transcript_queue, config, media_type, interval)
        self._assembler = WindowAssembler(self.SAMPLE_RATE, self.DEPTH, self.WINDOW_SECONDS)
        self._window_queue = queue_module.Queue(maxsize=self.MAX_PENDING_WINDOWS)
        # Set by the server: callable(state, message) that tells this pod's
        # client whether ASR is 'ok' or 'degraded'.
        self.on_status = None
        # Voice-activity gate: built by gate_factory() on the flush thread
        # (see _transcribing); None means every window is transcribed.
        self.gate_factory = make_speech_gate
        self._gate = None

    def start(self):
        self.running = True
        _pool.add_listener(self._notify_status)
        # Warm in the background so joining a pod doesn't block ~7s on the
        # model load when no worker is up yet.
        threading.Thread(target=_pool.warm, daemon=True,
                         name="crisper-warm").start()
        self.asr_thread = threading.Thread(target=self._processing, name="crisper-asr")
        self.asr_thread.daemon = True
        self.asr_thread.start()
        self._flush_thread = threading.Thread(target=self._transcribing,
                                              name="crisper-flush", daemon=True)
        self._flush_thread.start()

    def stop(self):
        super().stop()
        _pool.remove_listener(self._notify_status)

    def _notify_status(self, state, message):
        callback = self.on_status
        if callback is not None:
            callback(state, message)

    def _processing(self):
        while self.running:
            try:
                chunk = self.audio_queue.get(timeout=0.25)
            except queue_module.Empty:
                continue
            if chunk is None or not isinstance(chunk, (bytes, bytearray)):
                break
            for window in self._assembler.add(chunk):
                self._enqueue_window(window)
        tail = self._assembler.flush()
        if tail:
            self._enqueue_window(tail)
        self._window_queue.put(None)

    def _enqueue_window(self, window):
        try:
            self._window_queue.put_nowait(window)
            return
        except queue_module.Full:
            pass
        try:
            dropped = self._window_queue.get_nowait()
            logging.warning("CrisperWhisper: transcription backlog full; "
                            "dropped the window at %.1fs", dropped[0])
        except queue_module.Empty:
            pass
        try:
            self._window_queue.put_nowait(window)
        except queue_module.Full:
            logging.warning("CrisperWhisper: dropped the window at %.1fs", window[0])

    # -- voice-activity gate -------------------------------------------------

    @property
    def windows_skipped(self):
        return self._gate.windows_skipped if self._gate is not None else 0

    @property
    def vad_skip_ratio(self):
        """Fraction of this pod's windows the gate kept off the GPU (metrics)."""
        return self._gate.skip_ratio if self._gate is not None else 0.0

    def _build_gate(self):
        try:
            self._gate = self.gate_factory()
        except Exception as e:
            # No gate is only slower, never wrong: transcribe everything.
            logging.warning("CrisperWhisper: VAD gate unavailable, transcribing "
                            "every window: %s", e)
            self._gate = None
        else:
            logging.info("CrisperWhisper: VAD gate %s",
                         self._gate.name if self._gate is not None else "off")

    def _admit(self, start_seconds, pcm):
        gate = self._gate
        if gate is None:
            return True
        try:
            transcribe, text = gate.admit(pcm)
        except Exception as e:
            logging.warning("CrisperWhisper: VAD gate (%s) failed, transcribing every "
                            "window from now on: %s", gate.name, e)
            self._gate = None
            return True
        if text:
            logging.info("%s", text)
        if not transcribe:
            logging.debug("CrisperWhisper: window at %.1fs has no speech; skipped",
                          start_seconds)
        return transcribe

    def _transcribing(self):
        self._build_gate()
        while True:
            window = self._window_queue.get()
            if window is None:
                break
            if self._admit(*window):
                self._flush(*window)
        self.transcript_queue.put(None)

    def _flush(self, start_seconds, pcm):
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(self.DEPTH)
                wf.setframerate(self.SAMPLE_RATE)
                wf.writeframes(pcm)
            data = _pool.transcribe(tmp.name)
            if data.get("error"):
                logging.warning("CrisperWhisper window failed: %s", data["error"])
            else:
                _emit_segments(self.transcript_queue, data.get("words", []),
                               offset=start_seconds)
        except WorkerUnavailable as e:
            # Already WARNed once by the slot/pool; one INFO line per lost window.
            logging.info("CrisperWhisper window at %.1fs skipped: %s", start_seconds, e)
        except Exception as e:
            logging.warning("CrisperWhisper transcription failed: %s", e)
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass


class CrisperWhisperPosthocASR(PosthocFileASR):
    """Post-hoc connector: one-shot worker over the whole recording."""

    DRAIN_NAME = "crisper"

    def __init__(self, audio_queue, transcript_queue, config, media_type,
                 interval, audio_file=None, model_id=None, mode=None):
        self.audio_queue = audio_queue
        self.transcript_queue = transcript_queue
        self.config = config
        self.audio_file = audio_file
        self.model_id = model_id or DEFAULT_MODEL
        self.mode = mode or "verbatim"
        self.running = False

    # start()/stop()/_drain_queue() come from PosthocFileASR.

    def _transcribe_file(self):
        try:
            logging.info("CrisperWhisper: transcribing %s via %s (mode=%s)",
                         self.audio_file, self.model_id, self.mode)
            data = run_json_worker(
                lambda out_path: [_worker_python(), _WORKER,
                                  "--oneshot", self.audio_file, out_path,
                                  "--model", self.model_id, "--mode", self.mode],
                timeout=POSTHOC_WORKER_TIMEOUT,
                handle=getattr(self, 'worker', None))  # killable on cancel
            words = data.get("words", [])
            logging.info("CrisperWhisper: %d words", len(words))
            _emit_segments(self.transcript_queue, words)
        except Exception as e:
            logging.error("CrisperWhisper transcription failed: %s", e, exc_info=True)
            self.fail(e)  # consumer keeps the pod's previous results
        finally:
            self.running = False
            self.transcript_queue.put(None)
