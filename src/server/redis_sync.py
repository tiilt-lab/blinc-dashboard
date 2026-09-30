"""Cross-process primitives on Redis for the multi-worker API (audit A).

gunicorn runs several API workers, so anything that used to be a module-level
dict, Lock or Semaphore now lives either here (under a Redis key) or in the
coordinator process (coordinator.py). Every helper degrades to the old
process-local behaviour when Redis is unreachable: an outage costs
cross-worker coordination, never the request.

Keys (in the instance's redis_db; the caller names them):
  lock(key, ttl, wait)          SET key <token> NX EX ttl
  Slots(key, limit, ttl)        INCR/DECR counter, EXPIRE ttl when first taken
  capped_push(key, v, cap, ttl) LPUSH + LTRIM 0 cap-1 + EXPIRE ttl

Import-light on purpose: the client is resolved lazily (redis_helper needs
config.ini) and ``_client`` is the test seam for a fake.
"""
import contextlib
import logging
import threading
import time
import uuid

_client = None  # test seam; None -> redis_helper.r
_warned = set()
_LOCK_POLL = 0.2


def client():
    if _client is not None:
        return _client
    from redis_helper import r
    return r


def _warn_once(what, e):
    if what not in _warned:
        _warned.add(what)
        logging.warning('%s: Redis unavailable (%s); using the process-local fallback', what, e)


_local_locks = {}
_local_locks_guard = threading.Lock()


def _local_lock(key):
    with _local_locks_guard:
        return _local_locks.setdefault(key, threading.Lock())


@contextlib.contextmanager
def lock(key, ttl, wait):
    """Hold ``key`` for the block. Yields True when acquired, False when
    another holder still had it after ``wait`` s (the caller decides what to
    do then). The key expires after ``ttl`` s so a worker that dies mid-work
    cannot block its successors; only the owner releases (token check), so a
    late release never drops a successor's lock."""
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait
    held = False
    local = None
    while True:
        try:
            if client().set(key, token, nx=True, ex=int(ttl)):
                held = True
                break
        except Exception as e:
            _warn_once('lock %s' % key, e)
            local = _local_lock(key)
            held = local.acquire(timeout=max(0.0, deadline - time.monotonic()))
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(_LOCK_POLL)
    try:
        yield held
    finally:
        if held and local is not None:
            local.release()
        elif held:
            try:
                r = client()
                value = r.get(key)
                if isinstance(value, bytes):
                    value = value.decode()
                if value == token:
                    r.delete(key)
            except Exception as e:
                logging.warning('lock %s: release failed (%s); it expires in %ss', key, e, ttl)


class Slots:
    """At most ``limit`` holders across all workers: INCR on acquire, DECR on
    release. The key expires ``ttl`` s after it was first taken, so a slot
    leaked by a killed worker frees itself (at worst the count is briefly off
    by the leaked slots). Falls back to a local BoundedSemaphore, the
    pre-Redis per-process behaviour, on Redis errors."""

    def __init__(self, key, limit, ttl):
        self.key = key
        self.limit = int(limit)
        self.ttl = int(ttl)
        self._local = threading.BoundedSemaphore(self.limit)

    def acquire(self):
        """A token for release(), or None when every slot is busy."""
        r = None
        n = None
        try:
            r = client()
            n = r.incr(self.key)
            if n == 1:
                r.expire(self.key, self.ttl)
            if n > self.limit:
                r.decr(self.key)
                return None
            return 'redis'
        except Exception as e:
            if n is not None:
                try:
                    r.decr(self.key)
                except Exception:
                    pass
            _warn_once('slots %s' % self.key, e)
            return 'local' if self._local.acquire(blocking=False) else None

    def release(self, token):
        if token == 'local':
            self._local.release()
        elif token == 'redis':
            try:
                r = client()
                if r.decr(self.key) <= 0:
                    r.delete(self.key)  # never negative after a TTL reset
            except Exception as e:
                logging.warning('slots %s: release failed (%s); the key expires in %ss', self.key, e, self.ttl)


def capped_push(key, value, cap, ttl):
    """Append ``value`` as the newest item of list ``key``, keeping only the
    ``cap`` newest (the oldest fall off the tail, where BRPOP reads) and
    giving the list ``ttl`` more seconds of life so an unwatched stream is
    dropped rather than hoarded."""
    pipe = client().pipeline()
    pipe.lpush(key, value)
    pipe.ltrim(key, 0, int(cap) - 1)
    pipe.expire(key, int(ttl))
    pipe.execute()
