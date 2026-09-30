"""Per-pod pending frame batches for the detect worker (audit B.3).

A batch is 40 native-resolution frames (~250 MB at 1080p). The old
Queue(maxsize=50) per pod let one pod whose analytics lagged pin ~12 GB. Keep
at most MAX_PENDING_BATCHES per pod and drop the OLDEST when full: the
detector samples the live stream, so the newest batch is the useful one.
Frame rate and resolution are deliberately untouched (they are the analytics
input); see the ffmpeg note in server.py for the scale follow-up.

Import-light on purpose (like frame_payload.py) so it is unit-testable in CI.
"""
import logging
import time
from queue import Queue, Empty, Full

MAX_PENDING_BATCHES = 2
DROP_WARN_INTERVAL_SECONDS = 60.0


class BoundedBatchQueue(Queue):
    """A queue.Queue the detect scheduler drains with get_nowait() unchanged,
    plus a non-blocking drop-oldest put and a per-pod dropped counter."""

    def __init__(self, label, maxsize=MAX_PENDING_BATCHES,
                 warn_interval=DROP_WARN_INTERVAL_SECONDS, clock=time.monotonic):
        Queue.__init__(self, maxsize=maxsize)
        self.label = label
        self.dropped = 0  # batches evicted unprocessed for this pod
        self._warn_interval = warn_interval
        self._clock = clock
        self._last_warn = None
        self._dropped_since_warn = 0

    def put_latest(self, payload):
        """Enqueue, evicting the oldest pending batch if full. Never blocks."""
        for _ in range(3):  # bounded: the consumer may race the eviction
            try:
                self.put_nowait(payload)
                return True
            except Full:
                pass
            try:
                self.get_nowait()
            except Empty:
                continue
            self._note_drop()
        # Only reachable if producer and consumer keep racing; the new batch
        # is the casualty this time so the drop is still counted.
        self._note_drop()
        logging.warning('frame queue for %s: could not enqueue latest batch; dropping it', self.label)
        return False

    def _note_drop(self):
        self.dropped += 1
        self._dropped_since_warn += 1
        now = self._clock()
        if self._last_warn is None or now - self._last_warn >= self._warn_interval:
            logging.warning(
                'frame queue for %s full (max %d batches): dropped oldest batch; '
                '%d dropped in the last %ds, %d total (analytics lagging: fewer '
                'pods or lower resolution)',
                self.label, self.maxsize, self._dropped_since_warn,
                int(self._warn_interval), self.dropped)
            self._last_warn = now
            self._dropped_since_warn = 0
