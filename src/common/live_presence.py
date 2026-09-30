"""Live-pod presence keys shared by the live audio server and the post-hoc
services (audit Phase 2, item 14: post-hoc GPU work defers to live classes).

Contract (both sides import this module so the key format cannot drift):

  * key      ``live_pod:<processing_key>``, value ``"1"``
  * TTL      ``DEFAULT_TTL`` seconds, set on the pod's ``start`` and refreshed
             every ``REFRESH_SECONDS`` while the pod streams
  * delete   on end/disconnect (best effort — the TTL is the safety net: a
             crashed audio process leaves no key behind for more than one TTL)
  * consumer ``live_count(r) > 0`` means a class is live; defer GPU-heavy jobs

Import-light on purpose (no redis import): any client that speaks
SET/DELETE/SCAN works, including a fake in tests. Redis errors propagate to
the caller, which decides whether presence is best-effort (audio server) or
must fail closed (post-hoc side).
"""

PREFIX = 'live_pod:'
DEFAULT_TTL = 60
REFRESH_SECONDS = 20  # well under TTL/2 so one missed refresh never drops presence


def make_key(processing_key):
    return PREFIX + str(processing_key)


def mark_live(r, key, ttl=DEFAULT_TTL):
    """Assert (or refresh) that pod ``key`` is streaming for another ``ttl`` s."""
    r.set(make_key(key), "1", ex=int(ttl))


def clear_live(r, key):
    """Drop pod ``key``'s presence now rather than waiting for the TTL."""
    r.delete(make_key(key))


def live_keys(r):
    """Processing keys of every pod currently marked live."""
    keys = []
    for k in r.scan_iter(match=PREFIX + '*', count=100):
        if isinstance(k, bytes):
            k = k.decode('utf-8', 'replace')
        keys.append(k[len(PREFIX):])
    return keys


def live_count(r):
    """How many pods are live right now (0 = safe to take the GPU)."""
    return len(live_keys(r))
