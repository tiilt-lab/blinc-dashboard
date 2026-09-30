"""Bounded per-utterance pool (src/audio_processing/segment_pool.py) — audit B.4.

processor.py used to start an uncapped thread per ASR segment. Now every
AudioProcessor shares one 4-worker pool; submit never blocks; past 64 waiting
segments the OLDEST waiting one is degraded to lite (transcript still posted,
optional enrichment skipped). The executor is injected so jobs run by hand.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

import segment_pool  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing")


class ManualExecutor:
    """Collects submitted callables; the test runs them when it wants."""

    def __init__(self):
        self.queue = []
        self.shut = None

    def submit(self, fn):
        self.queue.append(fn)

    def run_one(self):
        return self.queue.pop(0)()

    def shutdown(self, wait=True, cancel_futures=False):
        self.shut = (wait, cancel_futures)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _pool(cap=3):
    ex = ManualExecutor()
    return segment_pool.SegmentPool(soft_cap=cap, clock=Clock(), executor=ex), ex


def test_defaults_are_four_workers_and_a_64_soft_cap():
    assert segment_pool.WORKERS == 4
    assert segment_pool.PENDING_SOFT_CAP == 64
    p = segment_pool.SegmentPool()
    assert p.workers == 4 and p.soft_cap == 64


def test_submit_never_blocks_and_passes_lite_false_under_the_cap():
    p, ex = _pool(cap=3)
    seen = []
    for i in range(3):
        p.submit(lambda i, lite: seen.append((i, lite)), i)
    assert p.pending() == 3 and len(ex.queue) == 3
    while ex.queue:
        ex.run_one()
    assert seen == [(0, False), (1, False), (2, False)]
    assert p.pending() == 0 and p.degraded == 0


def test_past_the_cap_the_oldest_waiting_segment_is_degraded_not_dropped():
    p, ex = _pool(cap=3)
    jobs = [p.submit(lambda i, lite: (i, lite), i) for i in range(5)]
    # 4th submit pushed pending to 4 (> 3): job 0 degraded; 5th: job 1.
    assert [j.lite for j in jobs] == [True, True, False, False, False]
    assert p.degraded == 2
    # every job still runs (nothing dropped) and reports its own lite flag
    results = [ex.run_one() for _ in range(5)]
    assert results == [(0, True), (1, True), (2, False), (3, False), (4, False)]


def test_lite_is_read_when_the_job_starts_not_when_submitted():
    p, ex = _pool(cap=2)
    j0 = p.submit(lambda lite: lite)
    j1 = p.submit(lambda lite: lite)
    assert not j0.lite
    p.submit(lambda lite: lite)          # pending 3 > 2 -> j0 degraded
    assert j0.lite and not j1.lite
    assert ex.run_one() is True          # j0 ran lite
    assert ex.run_one() is False         # j1 unaffected
    assert p.pending() == 1


def test_started_jobs_are_never_degraded_retroactively():
    p, ex = _pool(cap=1)
    j0 = p.submit(lambda lite: lite)
    ex.run_one()                         # j0 started and finished
    assert p.pending() == 0
    p.submit(lambda lite: lite)
    p.submit(lambda lite: lite)          # pending 2 > 1: degrades the waiting one
    assert not j0.lite


def test_degrade_warning_is_rate_limited(caplog):
    import logging
    clk = Clock()
    ex = ManualExecutor()
    p = segment_pool.SegmentPool(soft_cap=1, clock=clk, executor=ex)
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            p.submit(lambda lite: None)
    msgs = [r.getMessage() for r in caplog.records if "utterance pool backlog" in r.getMessage()]
    assert len(msgs) == 1 and "1 segment(s) degraded" in msgs[0]
    clk.t += segment_pool.DEGRADE_LOG_INTERVAL
    with caplog.at_level(logging.WARNING):
        p.submit(lambda lite: None)
    msgs = [r.getMessage() for r in caplog.records if "utterance pool backlog" in r.getMessage()]
    assert len(msgs) == 2 and "3 segment(s) degraded" in msgs[1] and "(4 total)" in msgs[1]


def test_shutdown_cancels_queued_work_without_waiting():
    p, ex = _pool()
    p.submit(lambda lite: None)
    p.shutdown()
    assert ex.shut == (False, True)


def test_shared_pool_is_process_wide():
    a = segment_pool.shared()
    assert a is segment_pool.shared()
    assert isinstance(a, segment_pool.SegmentPool)


# ---- processor.py wiring (source contract; processor imports the models) --

def _processor():
    with open(os.path.join(SRC, "processor.py")) as f:
        return f.read()


def test_processor_submits_to_the_shared_pool_instead_of_spawning_threads():
    s = _processor()
    assert "threading.Thread(target=self.process_transcript" not in s, \
        "the uncapped thread-per-segment spawn is back"
    assert "segment_pool.shared().submit(" in s
    assert "self.process_transcript," in s
    # the latch still brackets every segment (exactly-once completion)
    assert "self._latch.task_started()" in s and "self._latch.task_done()" in s


def test_lite_mode_keeps_the_transcript_post_and_a_single_speaker_match():
    s = _processor()
    assert re.search(r"def process_transcript\(self, .*?, lite=False\)", s)
    assert re.search(r"def _enrich\(self, text, lite=False\)", s)
    body = s[s.index("def process_transcript"):]
    assert "if self.topic_model and not lite:" in body
    assert "self.config.channels == 6 and not lite:" in body
    # windowed split (~30 encodes) skipped in lite; whole-segment match kept
    assert "if not lite:" in body and "segment_split.split_and_attribute(" in body
    assert re.search(r"if parts is None:\s*\n.*?self\._optional\(\s*'fingerprint match'", body, re.S)
    assert "callbacks.post_transcripts(" in body
