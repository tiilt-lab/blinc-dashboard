"""GPU admission for the post-hoc services (audit C.1; Phase 2 item 14, post-hoc half).

Every post-hoc run loads 5-10 GB of models onto the one GPU that live classes
also use, and nothing stopped two runs, or a run and a class, from doing so at
once (the CUDA OOMs of 09-23/24). A run now:

  * is refused while any live pod streams (``live_pod:<processing_key>`` keys,
    60 s TTL, written by the live services) or every ``gpu_lease:<slot>`` is
    held — the caller replies ``GPU_BUSY_REPLY`` and does nothing else;
  * holds its slot with SET NX + TTL, renewed every ttl/3 by a heartbeat
    thread and released owner-checked, so a crashed holder frees the GPU
    within the TTL (3 min);
  * is PRE-EMPTED when a class starts mid-run: ``PreemptionWatcher`` polls
    live presence every 10 s and fires the run's stop path (same as cancel),
    which posts nothing, reports ``posthoc_failed`` (reason "preempted") and
    sends ``PREEMPTED_REPLY`` so the queue re-enqueues the pod.

Redis trouble counts as busy: a run that starts blind could land on a class.
"""
import logging
import threading

from distributed_claim import compare_and_delete, compare_and_expire

DEFAULT_TTL = 180
LIVE_POD_PATTERN = 'live_pod:*'
WATCH_INTERVAL = 10.0

GPU_BUSY_REPLY = {
    'type': 'error', 'code': 'gpu_busy', 'retry_after': 60,
    'message': 'The GPU is in use by a live session or another analysis; '
               'try again later or use the queue.',
}
PREEMPTED_REPLY = {
    'type': 'error', 'code': 'gpu_busy', 'retry_after': 120,
    'message': 'Paused: a live session started. The analysis will restart '
               'from the queue when the class ends.',
}
PREEMPTED = 'preempted'

try:
    from live_presence import live_count as _live_count  # the live workstream's helper
except ImportError:
    def _live_count(r):
        return sum(1 for _ in r.scan_iter(match=LIVE_POD_PATTERN, count=200))


def live_pod_count(r):
    """Pods streaming to the live services right now. Raises on Redis trouble."""
    return int(_live_count(r))


def _text(value):
    return value.decode('utf8', 'replace') if isinstance(value, bytes) else value


class GpuLease:
    def __init__(self, r, owner, ttl=DEFAULT_TTL, slots=1, key='gpu_lease', heartbeat_period=None):
        self._r = r
        self.owner = str(owner)
        self.ttl = int(ttl)
        self.slots = int(slots)
        self.key = key
        self._period = heartbeat_period or self.ttl / 3.0
        self.slot = None
        self._hb = None
        self._stop = threading.Event()

    def slot_key(self, n):
        return '%s:%d' % (self.key, n)

    def try_acquire(self):
        """Take the first free slot (SET NX EX); False when all are held."""
        if self.slot is not None:
            return True
        for n in range(self.slots):
            try:
                if self._r.set(self.slot_key(n), self.owner, nx=True, ex=self.ttl):
                    self.slot = n
                    return True
            except Exception as e:
                logging.warning('GpuLease: Redis error acquiring %s: %s', self.slot_key(n), e)
                return False
        return False

    def heartbeat(self):
        """Re-arm the TTL while we still own the slot; False once it is lost."""
        if self.slot is None:
            return False
        try:
            ok = compare_and_expire(self._r, self.slot_key(self.slot), self.owner, self.ttl)
        except Exception as e:
            logging.warning('GpuLease: heartbeat failed for %s: %s', self.owner, e)
            return False
        if not ok:
            logging.warning('GpuLease: %s lost slot %d (expired; now %s)',
                            self.owner, self.slot, self.holder())
        return ok

    def release(self):
        """Compare-and-delete our slot (a slot that expired and was re-taken
        by another run is left alone). Stops the heartbeat first."""
        self.stop_heartbeat()
        if self.slot is None:
            return False
        slot, self.slot = self.slot, None
        try:
            return compare_and_delete(self._r, self.slot_key(slot), self.owner)
        except Exception as e:
            logging.warning('GpuLease: release failed for %s (TTL will clear it): %s', self.owner, e)
            return False

    def holder(self, slot=None):
        """Owner token currently in a slot (ours by default), or None."""
        n = slot if slot is not None else (self.slot or 0)
        try:
            return _text(self._r.get(self.slot_key(n)))
        except Exception:
            return None

    def holders(self):
        return [self.holder(n) for n in range(self.slots)]

    def start_heartbeat(self):
        if self._hb is not None:
            return
        self._stop.clear()
        self._hb = threading.Thread(target=self._beat, name='gpu-lease-heartbeat', daemon=True)
        self._hb.start()

    def stop_heartbeat(self):
        self._stop.set()
        self._hb = None

    def _beat(self):
        while not self._stop.wait(self._period):
            if self.slot is None:
                break
            self.heartbeat()


class PreemptionWatcher:
    """While a run holds the GPU, poll live presence every ``interval`` s; on
    the first live pod set ``preempted`` and call ``on_preempt`` once. Redis
    errors are logged, not treated as a class (the lease, not the watcher,
    guards a blind start)."""

    def __init__(self, r, on_preempt, interval=WATCH_INTERVAL, count=live_pod_count):
        self._r = r
        self._on_preempt = on_preempt
        self._interval = interval
        self._count = count
        self._stop = threading.Event()
        self._thread = None
        self.preempted = False

    def check_once(self):
        """One poll: True (and on_preempt fired) when a live pod is present."""
        if self.preempted:
            return True
        try:
            n = self._count(self._r)
        except Exception as e:
            logging.warning('PreemptionWatcher: live presence unavailable: %s', e)
            return False
        if n <= 0:
            return False
        self.preempted = True
        logging.warning('PreemptionWatcher: %d live pod(s) appeared; pre-empting the post-hoc run', n)
        try:
            self._on_preempt()
        except Exception:
            logging.exception('PreemptionWatcher: on_preempt failed')
        return True

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name='gpu-preempt-watch', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self._interval):
            if self.check_once():
                break


def live_pods_present(r):
    """Final check right before a run swaps its results in: True when a class
    is streaming (or Redis cannot say), so the swap must not happen."""
    try:
        return live_pod_count(r) > 0
    except Exception as e:
        logging.warning('live_pods_present: Redis unavailable (%s); assuming busy', e)
        return True


def acquire_for_run(r, owner, ttl=DEFAULT_TTL, slots=1):
    """Lease for one post-hoc run with its heartbeat running, or None when the
    GPU must not be touched (a live pod streams, every slot is held, or Redis
    cannot answer). The caller releases it in a finally."""
    try:
        n = live_pod_count(r)
    except Exception as e:
        logging.warning('GPU lease for %s refused: live presence unavailable (%s)', owner, e)
        return None
    if n > 0:
        logging.info('GPU lease for %s deferred: %d live pod(s)', owner, n)
        return None
    lease = GpuLease(r, owner, ttl=ttl, slots=slots)
    if not lease.try_acquire():
        logging.info('GPU lease for %s refused: held by %s', owner, lease.holders())
        return None
    lease.start_heartbeat()
    return lease
