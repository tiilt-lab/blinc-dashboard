"""Cross-process registry of pods currently being post-hoc processed.

Four posthoc claim flows (audio full-analytics / P&I / E&T, and video) each
hand-rolled the same thing: a threading.Lock, a module-level dict, an
"if key in dict: reject; dict[key]='running'" claim, and scattered
dict.pop(...) releases — plus, after fix #6, a parallel DistributedClaim call
next to each. That is duplicated coordination boilerplate.

PodRegistry is that pattern, once. It fuses the DistributedClaim (at most one
holder across processes, TTL self-heal) with the local dict that holds the live
processing object (a live object can't cross processes, so it stays local for
teardown). It is a drop-in for the old dict where the processors touch it
(pop/get/keys/__contains__/[]=), but the claim DECISION is the explicit atomic
``try_claim`` — a plain ``[]=`` can't express "reject if already running".

Phase 2 (audit C.4): claims held here are renewed every ``renew_interval``
seconds so a run longer than the TTL keeps its claim, and ``clear_stale``
drops what a dead predecessor of this service left behind.
"""
import logging
import threading
import time

from distributed_claim import DistributedClaim


class PodRegistry:
    def __init__(self, redis_factory=None, prefix="", ttl=3 * 60 * 60, renew_interval=600):
        # ttl: longer than any real run; bounds a claim whose holder died so
        # the pod isn't wedged forever (the old dict never expired).
        self._claim = DistributedClaim(redis_factory=redis_factory, prefix=prefix)
        self._ttl = ttl
        self._renew_interval = renew_interval
        self._renewer = None
        self._objs = {}
        self._lock = threading.Lock()

    def try_claim(self, key, obj="running"):
        """Atomically claim ``key`` across all processes. True if acquired (and
        ``obj`` is stored locally), False if a run for this pod is already
        active anywhere."""
        if not self._claim.try_claim(key, ttl=self._ttl):
            return False
        with self._lock:
            self._objs[key] = obj
        self._ensure_renewer()
        return True

    # --- dict-like surface for the parts the processors already use ---------

    def __setitem__(self, key, obj):
        # Replace the stored object (e.g. swap "running" for the live processor
        # once it exists). Assumes the claim is already held by try_claim.
        with self._lock:
            self._objs[key] = obj

    def __contains__(self, key):
        with self._lock:
            return key in self._objs

    def get(self, key, default=None):
        with self._lock:
            return self._objs.get(key, default)

    def keys(self):
        with self._lock:
            return list(self._objs.keys())

    def __len__(self):
        # So `if registry:` / `len(registry)` reflect active runs in THIS
        # process, matching the old dict truthiness (e.g. idle-recycle checks).
        with self._lock:
            return len(self._objs)

    def pop(self, key, default=None):
        """Remove the pod from the local registry AND release its cross-process
        claim. Matches dict.pop(key, default) so the processors' completion
        cleanup (``registry.pop(auth_key, None)``) releases the claim for free."""
        with self._lock:
            obj = self._objs.pop(key, default)
        self._claim.release(key)
        return obj

    def is_running(self, key):
        """Cross-process: is a run for this pod active anywhere (even another
        process)? Unlike ``key in self`` which only sees THIS process."""
        return self._claim.is_claimed(key)

    # --- Phase 2: heartbeat + stale clean-up ---------------------------------

    def renew_all(self):
        """Extend every claim this process holds; returns the keys that could
        not be renewed (expired and possibly re-taken elsewhere)."""
        lost = [key for key in self.keys() if not self._claim.renew(key, self._ttl)]
        if lost:
            logging.warning("PodRegistry: claim(s) lost before renewal: %s", lost)
        return lost

    def _ensure_renewer(self):
        # A multi-hour recording outlives the TTL; renew while runs are held.
        with self._lock:
            if self._renewer is not None or not self._renew_interval:
                return
            self._renewer = threading.Thread(target=self._renew_loop,
                                             name="claim-renew", daemon=True)
            self._renewer.start()

    def _renew_loop(self):
        while True:
            time.sleep(self._renew_interval)
            try:
                self.renew_all()
            except Exception:
                logging.exception("PodRegistry: renewal failed")

    def clear_stale(self):
        """At service start: drop this prefix's claims whose holder is dead on
        this host (see DistributedClaim.clear_stale)."""
        return self._claim.clear_stale()
