"""Phase 1 / audit B.3: per-pod frame batches are bounded, drop-oldest.

videoprocessor.py fed the detect worker through Queue(maxsize=50) per pod —
40 native-resolution frames per batch, ~12 GB pinned by one lagging 1080p pod.
BoundedBatchQueue keeps 2 batches, evicts the oldest, counts drops per pod and
warns at most once a minute with counts. The detect scheduler still drains it
with plain get_nowait().
"""
import logging
import os
import sys
from queue import Empty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src",
                                "video_processing", "video_cartoonizer"))

from bounded_batch_queue import BoundedBatchQueue, MAX_PENDING_BATCHES  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_default_bound_is_two_batches():
    assert MAX_PENDING_BATCHES == 2
    q = BoundedBatchQueue("pod-1")
    assert q.maxsize == 2


def test_drops_oldest_when_full_and_counts():
    q = BoundedBatchQueue("pod-1", clock=Clock())
    assert q.put_latest("b1") and q.put_latest("b2")
    assert q.dropped == 0
    assert q.put_latest("b3") is True   # evicts b1
    assert q.put_latest("b4") is True   # evicts b2
    assert q.dropped == 2
    assert q.get_nowait() == "b3"
    assert q.get_nowait() == "b4"
    try:
        q.get_nowait()
        assert False, "queue should be empty"
    except Empty:
        pass


def test_never_blocks_and_keeps_newest():
    q = BoundedBatchQueue("pod-1", clock=Clock())
    for i in range(100):
        q.put_latest(i)
    assert q.qsize() == 2
    assert q.dropped == 98
    assert [q.get_nowait(), q.get_nowait()] == [98, 99]


def test_warning_is_rate_limited_with_counts(caplog):
    caplog.set_level(logging.WARNING)
    clock = Clock()
    q = BoundedBatchQueue("pod-9", warn_interval=60.0, clock=clock)
    for i in range(12):        # 10 drops within the first minute
        q.put_latest(i)
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1     # first drop warns, the rest are counted
    assert "pod-9" in warns[0] and "1 total" in warns[0]
    clock.t = 61.0
    q.put_latest("late")        # 11th drop, a minute later
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2
    assert "10 dropped in the last 60s" in warns[1] and "11 total" in warns[1]
    assert q.dropped == 11


def test_videoprocessor_uses_the_bounded_queue():
    # Source contract: the 50-deep queue must not come back.
    path = os.path.join(os.path.dirname(__file__), "..", "src", "video_processing",
                        "video_cartoonizer", "videoprocessor.py")
    with open(path) as f:
        s = f.read()
    assert "Queue(maxsize=50)" not in s
    assert "BoundedBatchQueue(candidate_queue_id)" in s
    assert ".put_latest(payload)" in s
