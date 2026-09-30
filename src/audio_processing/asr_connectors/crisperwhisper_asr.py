"""CrisperWhisper 2.0 connectors (verbatim ASR with crisp word timestamps).

CrisperWhisper (nyra health) is a Whisper fine-tune that transcribes
*verbatim* — fillers, stutters, false starts — with word timestamps accurate
enough to measure pauses, which regular Whisper glues onto neighboring
words. Weights are non-commercial-research licensed; inference code is MIT.

The package needs its own CTranslate2 fork, so it lives in src/venv-crisper
and all inference goes through crisper_worker.py subprocesses:
  - CrisperWhisperASR (live): all pods share ONE persistent --serve worker
    (one ~4GB model copy on the GPU instead of one per pod). A window
    transcribes in ~1s and each pod produces one per 12s, so serialized
    requests stay far under budget; the worker is reaped after 15 idle
    minutes to give the GPU memory back between sessions. A failed spawn or
    a dead worker opens a circuit breaker (SpawnBackoff, 30s -> 300s) and
    every live pod is told ASR is degraded until a window succeeds again.
  - CrisperWhisperPosthocASR: --oneshot on the whole recording in its own
    process (so a long file can never head-of-line-block live captions),
    then emits gap-segmented Google-shaped AsrResults (same contract as
    Qwen3ASR; speaker attribution happens downstream via fingerprints).
"""
import json
import logging
import os
import queue as queue_module
import subprocess
import tempfile
import threading
import time
import wave

from .base_asr import (BaseASR, AsrResult, PosthocFileASR, worker_python,
                       run_json_worker, POSTHOC_WORKER_TIMEOUT)
from asr_ingest import WindowAssembler  # audio_processing/ is on sys.path

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crisper_worker.py")
# Worker stderr, appended for the life of the service. Under
# audio_processing/ so the existing logrotate rule (*.log, copytruncate)
# covers it; a death used to be unrecordable because stderr was DEVNULL.
_STDERR_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "crisper_worker.stderr.log")
_stderr_fh = None

DEFAULT_MODEL = "nyralabs/CrisperWhisper2.0_large"

# Refuse to spawn when the GPU has less than this free: the model needs ~4GB
# and a spawn that OOMs takes the 180s start timeout under the shared lock.
MIN_FREE_VRAM_MIB = 3500

# Short human text for the pod client's asr_status banner.
MSG_RESTARTING = "Live captions paused: the speech recognizer is restarting."
MSG_UNRESPONSIVE = "Live captions paused: the speech recognizer stopped responding."
MSG_LOW_VRAM = "Live captions paused: the server's GPU memory is low."


def _worker_python():
    # venv-crisper lives in src/ (3 dirs above asr_connectors/); shared resolver.
    return worker_python("venv-crisper", 3)


def _stderr_file():
    global _stderr_fh
    if _stderr_fh is None:
        _stderr_fh = open(_STDERR_LOG, "ab", buffering=0)
    return _stderr_fh


def _stderr_tail(nbytes=600):
    """Last lines the worker wrote, for the failure WARNING."""
    try:
        with open(_STDERR_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf8", "replace").strip()
    except OSError:
        return ""


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
    """Circuit breaker for (re)spawning the shared worker.

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
    """Raised at once (never after a wait) while the spawn breaker is open."""


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


class _SharedWorker:
    """Process-wide CrisperWhisper worker shared by every live pod.

    The model is identical and read-only across pods, so one subprocess
    (one ~4GB GPU copy, one ~7s load) serves them all. Requests are
    serialized under a lock — the worker is single-threaded anyway, and
    doing send+readline as one unit keeps the pipe protocol in sync with
    no request-ID bookkeeping. A dead or unspawnable worker opens the
    SpawnBackoff breaker: windows are skipped (WorkerUnavailable, raised at
    once — the lock is never held across a wait) and pods are told ASR is
    degraded until a window succeeds. After IDLE_SHUTDOWN_SECONDS without a
    window the worker is shut down to give the GPU memory back; the next
    window relaunches it transparently.
    """

    IDLE_SHUTDOWN_SECONDS = 900

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._last_used = 0.0
        self._reaper_started = False
        self.backoff = SpawnBackoff()
        # Degraded/ok fan-out to live pods. Own lock: listeners are cheap
        # (reactor-marshalled sends) but must never need the worker lock.
        self._listeners = []
        self._listeners_lock = threading.Lock()
        self._state = ("ok", None)

    # -- status fan-out -----------------------------------------------------

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
        # One WARNING per state change and one message per pod, however many
        # windows fail while the breaker is open.
        with self._listeners_lock:
            if (state, message) == self._state:
                return
            changed = state != self._state[0]
            self._state = (state, message)
            listeners = list(self._listeners)
        if state != "ok":
            logging.log(logging.WARNING if changed else logging.INFO,
                        "ASR degraded: %s", message)
        else:
            logging.warning("ASR recovered: shared CrisperWhisper worker is serving again")
        for callback in listeners:
            try:
                callback(state, message)
            except Exception as e:
                logging.debug("asr_status listener failed: %s", e)

    def _failed(self, reason, client_message):
        # Breaker + one WARNING carrying the worker's last stderr lines (a
        # death finally has a recorded cause) + degraded status to the pods.
        delay = self.backoff.record_failure(time.time())
        tail = _stderr_tail()
        logging.warning("CrisperWhisper worker: %s; next spawn attempt in %.0fs%s",
                        reason, delay,
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

    def _ensure_proc(self):
        if self._proc is not None:
            rc = self._proc.poll()
            if rc is None:
                return
            # Died between requests (OOM-kill, CUDA fault): same breaker as
            # a failed spawn, so a crash loop can't cost a 7s model load
            # under the shared lock for every window of every pod.
            self._proc = None
            self._failed("worker exited unexpectedly (rc=%s)" % rc, MSG_RESTARTING)
        now = time.time()
        if not self.backoff.may_attempt(now):
            raise WorkerUnavailable("restart backoff, %.0fs left"
                                    % self.backoff.retry_in(now))
        free = free_vram_mib()
        if free is not None and free < MIN_FREE_VRAM_MIB:
            self._failed("only %d MiB of GPU memory free (need %d)"
                         % (free, MIN_FREE_VRAM_MIB), MSG_LOW_VRAM)
            raise WorkerUnavailable("GPU memory low (%d MiB free)" % free)
        import config as cf
        model = cf.crisperwhisper_model()
        mode = cf.crisperwhisper_mode()
        logging.info("Starting shared CrisperWhisper worker (model=%s, mode=%s)",
                     model, mode)
        ok = False
        try:
            err = _stderr_file()
            err.write(("---- %s spawn model=%s mode=%s ----\n"
                       % (time.strftime("%Y-%m-%d %H:%M:%S"), model, mode)).encode())
            self._proc = subprocess.Popen(
                [_worker_python(), _WORKER, "--serve", "--model", model, "--mode", mode],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=err, text=True, bufsize=1)
            ready = self._read_reply(timeout=180)
            ok = bool(ready) and bool(json.loads(ready).get("ready"))
            reason = "worker exited during model load"
        except Exception as e:
            reason = "spawn failed: %s" % e
        if not ok:
            self._kill()
            self._failed(reason, MSG_RESTARTING)
            raise WorkerUnavailable(reason)
        if not self._reaper_started:
            self._reaper_started = True
            threading.Thread(target=self._reap_idle, daemon=True,
                             name="crisper-idle-reaper").start()

    def _kill(self):
        proc, self._proc = self._proc, None
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
        except Exception:
            self._kill()

    def _reap_idle(self):
        while True:
            time.sleep(60)
            with self._lock:
                if (self._proc is not None
                        and time.time() - self._last_used > self.IDLE_SHUTDOWN_SECONDS):
                    logging.info("Shared CrisperWhisper worker idle >%ds — "
                                 "shutting down to free GPU memory",
                                 self.IDLE_SHUTDOWN_SECONDS)
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
        with self._lock:
            self._last_used = time.time()
            # Raises WorkerUnavailable at once while the breaker is open: the
            # old retry-once-and-respawn loop is what turned one bad spawn
            # into a 180s stall for every pod, once per window.
            self._ensure_proc()
            try:
                self._proc.stdin.write(json.dumps({"audio": wav_path}) + "\n")
                self._proc.stdin.flush()
                line = self._read_reply(timeout=120)
                if not line:
                    raise RuntimeError("worker closed its pipe")
                data = json.loads(line)
            except Exception as e:
                self._kill()
                self._failed("request failed: %s" % e, MSG_UNRESPONSIVE)
                raise
            self._last_used = time.time()
            # Only a served window proves the worker healthy (a spawn that
            # then faults on inference must keep growing the backoff).
            self.backoff.record_success()
            self._set_state("ok")
            return data


_shared_worker = _SharedWorker()


class CrisperWhisperASR(BaseASR):
    """Live connector: fixed windows against the shared worker.

    Two threads: ``_processing`` only drains the ingest queue and assembles
    windows (so a slow or locked worker can never stop it draining, which is
    what made the reactor's queue fill and evict audio), and ``_transcribing``
    feeds completed windows to the shared worker. Window start times come
    from the chunks' absolute sample offsets (asr_ingest), not from how much
    audio survived the queue, so transcript times match AudioBuffer.
    """

    WINDOW_SECONDS = 12.0
    # ~2 min of audio waiting on the worker; beyond that the OLDEST window is
    # dropped so captions stay live rather than minutes stale.
    MAX_PENDING_WINDOWS = 10

    def __init__(self, audio_queue, transcript_queue, config, media_type, interval):
        super().__init__(audio_queue, transcript_queue, config, media_type, interval)
        self._assembler = WindowAssembler(self.SAMPLE_RATE, self.DEPTH, self.WINDOW_SECONDS)
        self._window_queue = queue_module.Queue(maxsize=self.MAX_PENDING_WINDOWS)
        # Set by the server: callable(state, message) that tells this pod's
        # client whether ASR is 'ok' or 'degraded'.
        self.on_status = None

    def start(self):
        self.running = True
        _shared_worker.add_listener(self._notify_status)
        # Warm in the background so joining a pod doesn't block ~7s on the
        # model load when the shared worker isn't up yet.
        threading.Thread(target=_shared_worker.warm, daemon=True,
                         name="crisper-warm").start()
        self.asr_thread = threading.Thread(target=self._processing, name="crisper-asr")
        self.asr_thread.daemon = True
        self.asr_thread.start()
        self._flush_thread = threading.Thread(target=self._transcribing,
                                              name="crisper-flush", daemon=True)
        self._flush_thread.start()

    def stop(self):
        super().stop()
        _shared_worker.remove_listener(self._notify_status)

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

    def _transcribing(self):
        while True:
            window = self._window_queue.get()
            if window is None:
                break
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
            data = _shared_worker.transcribe(tmp.name)
            if data.get("error"):
                logging.warning("CrisperWhisper window failed: %s", data["error"])
            else:
                _emit_segments(self.transcript_queue, data.get("words", []),
                               offset=start_seconds)
        except WorkerUnavailable as e:
            # Already WARNed once by the worker; one INFO line per lost window.
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
                timeout=POSTHOC_WORKER_TIMEOUT)
            words = data.get("words", [])
            logging.info("CrisperWhisper: %d words", len(words))
            _emit_segments(self.transcript_queue, words)
        except Exception as e:
            logging.error("CrisperWhisper transcription failed: %s", e, exc_info=True)
        finally:
            self.running = False
            self.transcript_queue.put(None)
