"""Bounded retry queue for the API callbacks (transcript, speaker metrics,
connect).

Those posts used to go out exactly once, on the utterance's own thread, so
every transcript that landed in an API restart was lost. The first attempt
still runs inline (callers keep their synchronous result); a retryable
failure is handed here and re-sent from one daemon thread with exponential
backoff ``base_delay`` -> ``max_delay`` until it succeeds or is
``give_up_after`` seconds old. The queue is bounded: when full the OLDEST
entry is dropped with a rate-limited WARNING. Every attempt carries
``X-Idempotency-Key`` so a replay after an ambiguous failure (timeout after
the API committed) is harmless server-side.

Pure and import-light (no requests/config): the sender is injected, so this
is unit-tested in CI with a fake.
"""
import logging
import threading
import time

IDEMPOTENCY_HEADER = "X-Idempotency-Key"


def idempotency_key(source, kind, start_time):
    """``<processing_key>:<kind>:<start_time>``, kind = transcript|metrics|connect."""
    return "%s:%s:%s" % (source, kind, start_time)


class _Entry:
    __slots__ = ("key", "url", "payload", "created", "attempts", "due")

    def __init__(self, key, url, payload, created, due):
        self.key = key
        self.url = url
        self.payload = payload
        self.created = created
        self.attempts = 0
        self.due = due


class RetryQueue:
    """``sender(url, payload, headers)`` returns True when the entry is finished
    (delivered, or failed in a way that will never succeed) and False to try
    again later; an exception counts as False. ``clock`` is injectable and
    ``run_due(now)`` can be driven by hand, so tests need no thread."""

    DROP_LOG_INTERVAL = 30.0

    def __init__(self, sender, max_size=2000, base_delay=1.0, max_delay=60.0,
                 give_up_after=600.0, clock=time.monotonic, autostart=True):
        self._sender = sender
        self.max_size = max_size
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.give_up_after = give_up_after
        self._clock = clock
        self._autostart = autostart
        self._entries = []
        self._cv = threading.Condition()
        self._thread = None
        self._drops_since_log = 0
        self._last_drop_log = None
        self.delivered = 0
        self.gave_up = 0
        self.dropped = 0

    def __len__(self):
        with self._cv:
            return len(self._entries)

    def stats(self):
        """Queue depth and lifetime outcome totals (for the metrics exporter)."""
        with self._cv:
            return {"size": len(self._entries), "delivered": self.delivered,
                    "gave_up": self.gave_up, "dropped": self.dropped}

    def delay_for(self, attempts):
        return min(self.max_delay, self.base_delay * (2 ** attempts))

    def submit(self, key, url, payload):
        now = self._clock()
        entry = _Entry(key, url, payload, created=now, due=now + self.delay_for(0))
        with self._cv:
            if len(self._entries) >= self.max_size:
                oldest = min(self._entries, key=lambda e: e.created)
                self._entries.remove(oldest)
                self._note_drop(oldest, now)
            self._entries.append(entry)
            self._cv.notify()
            if self._autostart and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name="callback-retry",
                                                daemon=True)
                self._thread.start()

    def _note_drop(self, entry, now):
        self.dropped += 1
        self._drops_since_log += 1
        if self._last_drop_log is None or now - self._last_drop_log >= self.DROP_LOG_INTERVAL:
            logging.warning("callback retry queue full (%d): dropped %d oldest entries, "
                            "latest %s", self.max_size, self._drops_since_log, entry.key)
            self._drops_since_log = 0
            self._last_drop_log = now

    def next_due_in(self, now=None):
        """Seconds until the earliest entry is due, or None when empty."""
        now = self._clock() if now is None else now
        with self._cv:
            if not self._entries:
                return None
            return max(0.0, min(e.due for e in self._entries) - now)

    def run_due(self, now=None):
        """Attempt every entry whose time has come; returns how many. Sends
        run outside the lock so submit() never waits on the network."""
        now = self._clock() if now is None else now
        with self._cv:
            due = [e for e in self._entries if e.due <= now]
            for entry in due:
                self._entries.remove(entry)
        for entry in due:
            headers = {IDEMPOTENCY_HEADER: entry.key}
            try:
                done = bool(self._sender(entry.url, entry.payload, headers))
            except Exception as e:
                logging.warning("callback retry %s failed: %s", entry.key, e)
                done = False
            if done:
                self.delivered += 1
                continue
            entry.attempts += 1
            entry.due = now + self.delay_for(entry.attempts)
            if entry.due - entry.created > self.give_up_after:
                self.gave_up += 1
                logging.warning("callback %s gave up after %d retries over %.0fs",
                                entry.key, entry.attempts, now - entry.created)
                continue
            with self._cv:
                self._entries.append(entry)
                self._cv.notify()
        return len(due)

    def _run(self):
        while True:
            with self._cv:
                while not self._entries:
                    self._cv.wait()
                wait = min(e.due for e in self._entries) - self._clock()
                if wait > 0:
                    self._cv.wait(timeout=wait)
                    continue
            self.run_due()
