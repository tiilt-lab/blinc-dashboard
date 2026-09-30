"""Staged results of a post-hoc audio run (audit C.3).

The run used to wipe the pod's previous transcripts before ASR started and
post each utterance as its thread finished, so an ASR crash (CUDA OOM), a
cancel or a pre-emption left the pod with nothing — and it was then marked
analysed. Results are staged here until the whole run is known good; only
then does the previous analysis go and the new one is posted, in time order.
Anything else keeps the old results. Pure state; the processor posts.
"""
import threading

CANCELLED = 'cancelled'
PREEMPTED = 'preempted'


class RunStaging:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = []   # (start_time, seq, kind, payload)
        self._seq = 0
        self.asr_failure = None
        self.failed = 0
        self.cancelled = False
        self.preempted = False

    def add(self, start_time, kind, payload):
        with self._lock:
            self._seq += 1
            self._items.append((start_time, self._seq, kind, payload))

    def mark_asr_failed(self, reason):
        # First failure wins: it is the one that stopped transcription.
        if self.asr_failure is None:
            self.asr_failure = str(reason)

    def mark_failed(self):
        with self._lock:
            self.failed += 1

    def cancel(self):
        self.cancelled = True

    def preempt(self):
        # A class started: same stop path as cancel, different report.
        self.preempted = True
        self.cancelled = True

    @property
    def accepting(self):
        """Whether further utterances are worth processing at all."""
        return not self.cancelled and self.asr_failure is None

    def __len__(self):
        with self._lock:
            return len(self._items)

    def ordered(self):
        """(kind, payload) in discussion order: start time, then arrival."""
        with self._lock:
            items = sorted(self._items, key=lambda it: (it[0], it[1]))
        return [(kind, payload) for _, _, kind, payload in items]

    def failure(self):
        """Why the run must not be published, or None when it may be."""
        if self.preempted:
            return PREEMPTED
        if self.cancelled:
            return CANCELLED
        if self.asr_failure is not None:
            return 'transcription failed: ' + self.asr_failure
        if self.failed:
            return '%d of %d utterance(s) failed processing' % (self.failed, self.failed + len(self))
        return None
