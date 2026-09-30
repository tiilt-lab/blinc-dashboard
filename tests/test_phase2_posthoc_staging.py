"""Staged post-hoc results (src/common/posthoc_staging.py; audit C.3): the run's
utterances are held until the whole run is known good, then published in
discussion order; a cancel, pre-emption, ASR failure or a lost utterance
abandons the run so the pod's previous results survive.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

from posthoc_staging import RunStaging, CANCELLED, PREEMPTED  # noqa: E402


def test_ordered_by_start_time_then_arrival():
    s = RunStaging()
    s.add(12.0, "transcript", "c")
    s.add(3.0, "metrics", "a")
    s.add(3.0, "metrics", "b")     # same start: keeps arrival order
    s.add(7.5, "transcript", "x")
    assert s.ordered() == [("metrics", "a"), ("metrics", "b"), ("transcript", "x"), ("transcript", "c")]
    assert len(s) == 4


def test_a_clean_run_publishes():
    s = RunStaging()
    s.add(1.0, "transcript", {})
    assert s.failure() is None and s.accepting


def test_first_asr_failure_wins_and_stops_accepting():
    s = RunStaging()
    s.mark_asr_failed("worker failed: CUDA out of memory")
    s.mark_asr_failed("second")
    assert s.failure() == "transcription failed: worker failed: CUDA out of memory"
    assert not s.accepting


def test_a_lost_utterance_fails_the_run_with_counts():
    s = RunStaging()
    s.add(1.0, "transcript", {})
    s.add(2.0, "transcript", {})
    s.mark_failed()
    assert s.failure() == "1 of 3 utterance(s) failed processing"
    assert s.accepting, "processing continues; the verdict is at completion"


def test_cancel_and_preempt_precedence():
    s = RunStaging()
    s.mark_asr_failed("x")
    s.cancel()
    assert s.failure() == CANCELLED and not s.accepting
    s.preempt()
    assert s.failure() == PREEMPTED and s.cancelled
