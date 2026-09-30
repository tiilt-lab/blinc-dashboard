"""Cross-process claim primitive (architecture fix #6).

Coordination like "is this pod already being processed?" was an in-process
dict guarded by a threading.Lock. That serializes THREADS within one process,
but says nothing to a second process (or a future second instance) — the
architecture silently assumed exactly one process per role, forever.

This is the missing shared primitive: an atomic claim in Redis (SET key NX EX),
so at most one holder across all processes. Two properties make adopting it
low-risk:

  * In-memory fallback. If Redis is unreachable (or unconfigured), it degrades
    to a per-process dict — i.e. exactly today's behavior. Never worse.
  * A TTL on every claim. A holder that dies without releasing self-heals when
    the TTL expires, instead of wedging the pod forever (the in-process dict
    never expired — that was a real bug class this session).

Phase 2 (audit C.4): the stored value is an owner token ``<pid>:<boot-id>``
rather than "1", so release and renewal are owner-checked (a claim that
expired and was re-taken by another process is never dropped by the old
holder), long runs can renew their TTL, and a service can clear at start the
claims a dead predecessor of its own left behind on this host.

The Redis client is injected (``redis_factory``) so the primitive is unit
testable without a server, and so callers can share the process's existing
connection.
"""
import logging
import os
import threading
import time


def _boot_id():
    # Tells a pid from before a reboot apart from the same pid now; falls back
    # to this process's start time where /proc is unavailable.
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except OSError:
        return str(int(time.time()))


BOOT_ID = _boot_id()
OWNER_TOKEN = '%d:%s' % (os.getpid(), BOOT_ID)

# Owner-checked scripts: touch the key only while it still carries OUR token.
CAD_SCRIPT = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
              "return redis.call('del', KEYS[1]) end return 0")
CAE_SCRIPT = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
              "return redis.call('expire', KEYS[1], ARGV[2]) end return 0")


def compare_and_delete(r, key, owner):
    return bool(r.eval(CAD_SCRIPT, 1, key, owner))


def compare_and_expire(r, key, owner, ttl):
    return bool(r.eval(CAE_SCRIPT, 1, key, owner, int(ttl)))


def _text(value):
    return value.decode('utf8', 'replace') if isinstance(value, bytes) else str(value or '')


def parse_owner(value):
    """(pid, boot_id) from a stored token; None for the legacy "1" or garbage."""
    pid, sep, boot = _text(value).partition(':')
    if not sep or not pid.isdigit():
        return None
    return int(pid), boot


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class DistributedClaim:
    def __init__(self, redis_factory=None, prefix="claim:", owner=None):
        # redis_factory: callable returning a redis client, or None for a pure
        # in-memory claim (single-process). A factory that raises is treated
        # as "Redis unavailable" and falls back to memory.
        self._redis_factory = redis_factory
        self._prefix = prefix
        self._owner = owner or OWNER_TOKEN
        self._mem = {}
        self._lock = threading.Lock()
        self._warned = False

    def _redis(self):
        if self._redis_factory is None:
            return None
        try:
            return self._redis_factory()
        except Exception as e:
            self._warn(e)
            return None

    def _warn(self, e):
        if not self._warned:
            self._warned = True
            logging.warning("DistributedClaim: Redis unavailable, using "
                            "in-memory (single-process) fallback: %s", e)

    def try_claim(self, key, ttl=3600):
        """Atomically claim ``key``. True if acquired, False if already held.
        The claim auto-expires after ``ttl`` seconds so a dead holder can't
        wedge the resource permanently."""
        r = self._redis()
        if r is not None:
            try:
                # SET NX EX: set-if-absent with expiry, in one atomic op.
                return bool(r.set(self._prefix + key, self._owner, nx=True, ex=ttl))
            except Exception as e:
                self._warn(e)  # fall through to the in-memory path
        with self._lock:
            if key in self._mem:
                return False
            self._mem[key] = True
            return True

    def release(self, key):
        r = self._redis()
        if r is not None:
            try:
                # Compare-and-delete: never drop a claim another process holds
                # now (ours expired mid-run and was re-taken).
                if not compare_and_delete(r, self._prefix + key, self._owner):
                    logging.info("DistributedClaim: %s%s not released (not held by this process)",
                                 self._prefix, key)
            except Exception as e:
                self._warn(e)  # TTL will clear it regardless
        with self._lock:
            self._mem.pop(key, None)

    def renew(self, key, ttl):
        """Extend a held claim's TTL. False once it has expired or belongs to
        another process."""
        r = self._redis()
        if r is not None:
            try:
                return compare_and_expire(r, self._prefix + key, self._owner, ttl)
            except Exception as e:
                self._warn(e)
        with self._lock:
            return key in self._mem

    def is_claimed(self, key):
        r = self._redis()
        if r is not None:
            try:
                return bool(r.exists(self._prefix + key))
            except Exception as e:
                self._warn(e)
        with self._lock:
            return key in self._mem

    def clear_stale(self, alive=pid_alive):
        """Delete claims under this prefix whose holder is dead on this host:
        same boot id and the pid is gone, or the pre-token value "1" (only a
        previous process of this role wrote those). Claims from another boot
        or host are left to their TTL. Returns the keys cleared."""
        r = self._redis()
        if r is None:
            return []
        cleared = []
        try:
            for full in list(r.scan_iter(match=self._prefix + '*', count=200)):
                value = r.get(full)
                parsed = parse_owner(value)
                if parsed is None:
                    stale = _text(value) == '1'
                else:
                    pid, boot = parsed
                    stale = boot == BOOT_ID and not alive(pid)
                if stale and r.delete(full):
                    cleared.append(_text(full))
        except Exception as e:
            self._warn(e)
        if cleared:
            logging.warning("DistributedClaim: cleared %d stale claim(s) at start: %s",
                            len(cleared), cleared)
        return cleared
