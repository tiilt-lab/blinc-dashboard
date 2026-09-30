"""Process-wide bounded worker pool for per-utterance processing (audit B.4).

processor.py used to start one thread per ASR segment with no cap: every
pod's every utterance ran keyword/feature/topic/DoA work plus ~30 ECAPA
encodes at once, on 8 cores shared with ffmpeg, the video models, post-hoc
and a co-tenant. Now every AudioProcessor in the process shares ONE pool of
``WORKERS`` threads and ``submit`` never blocks.

Backlog policy: when more than ``PENDING_SOFT_CAP`` segments are waiting,
the OLDEST waiting segment is flagged ``lite`` — it still runs and still
posts its transcript (with a single whole-segment speaker match), it just
skips the optional enrichment (questions, keywords, features, topic, DoA,
the windowed speaker split). A transcript is never dropped, only its
extras; and shedding the oldest first is what lets the queue drain.

Pure and import-light (stdlib only) so it is unit-tested in CI.
"""
import collections
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

WORKERS = 4
PENDING_SOFT_CAP = 64
DEGRADE_LOG_INTERVAL = 30.0


class Job:
    """Hand-off record: ``lite`` may be flipped while the job is waiting; the
    worker reads it exactly once, when the job starts."""
    __slots__ = ('lite', 'started')

    def __init__(self):
        self.lite = False
        self.started = False


class SegmentPool:
    def __init__(self, workers=WORKERS, soft_cap=PENDING_SOFT_CAP,
                 clock=time.monotonic, executor=None):
        self.workers = workers
        self.soft_cap = soft_cap
        self._clock = clock
        self._executor = executor  # injectable (tests run jobs by hand)
        self._lock = threading.Lock()
        self._pending = collections.deque()  # waiting Jobs, oldest first
        self.degraded = 0
        self._degraded_since_log = 0
        self._last_log = None

    def _pool(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix='utterance')
        return self._executor

    def pending(self):
        with self._lock:
            return len(self._pending)

    def submit(self, fn, *args, **kwargs):
        """Queue ``fn(*args, lite=<bool>, **kwargs)`` without blocking.

        Returns the Job so a caller (or test) can inspect ``lite``. Above the
        soft cap the oldest not-yet-lite waiting job is degraded and the
        event is logged at most once per ``DEGRADE_LOG_INTERVAL`` seconds.
        """
        job = Job()
        warning = None
        with self._lock:
            self._pending.append(job)
            if len(self._pending) > self.soft_cap:
                oldest = next((j for j in self._pending if not j.lite), None)
                if oldest is not None:
                    oldest.lite = True
                    self.degraded += 1
                    self._degraded_since_log += 1
                    now = self._clock()
                    if self._last_log is None or now - self._last_log >= DEGRADE_LOG_INTERVAL:
                        warning = ('utterance pool backlog %d > %d: %d segment(s) degraded to '
                                   'transcript-only since last report (%d total)'
                                   % (len(self._pending), self.soft_cap,
                                      self._degraded_since_log, self.degraded))
                        self._last_log = now
                        self._degraded_since_log = 0
        if warning:
            logging.warning(warning)

        def _run():
            with self._lock:
                job.started = True
                try:
                    self._pending.remove(job)
                except ValueError:
                    pass
                lite = job.lite
            return fn(*args, lite=lite, **kwargs)

        self._pool().submit(_run)
        return job

    def shutdown(self):
        """Drop queued work and let in-flight jobs finish (process exit)."""
        ex = self._executor
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)


_shared = None
_shared_lock = threading.Lock()


def shared():
    """The one pool every live AudioProcessor in this process submits to."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = SegmentPool()
        return _shared


def shutdown():
    # Called after the reactor stops: the executor's threads are non-daemon,
    # so without this a long backlog would hold process exit until it drained.
    with _shared_lock:
        if _shared is not None:
            _shared.shutdown()
