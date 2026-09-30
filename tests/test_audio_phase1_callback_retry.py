"""Bounded callback retry queue (src/audio_processing/callback_retry.py).

Audit E.4: transcript, speaker-metric and connect posts were sent once with
no retry. Driven here with a fake sender and a fake clock — no thread, no
requests — so it runs in CI.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

from callback_retry import RetryQueue, idempotency_key, IDEMPOTENCY_HEADER  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Sender:
    """Fails the first ``fail`` attempts, then delivers."""

    def __init__(self, fail=0, raise_instead=False):
        self.fail = fail
        self.raise_instead = raise_instead
        self.calls = []

    def __call__(self, url, payload, headers):
        self.calls.append((url, payload, dict(headers)))
        if len(self.calls) <= self.fail:
            if self.raise_instead:
                raise ConnectionError("api down")
            return False
        return True


def _drain(q, clock):
    """Advance the fake clock attempt by attempt; returns the attempt times."""
    times = []
    while len(q):
        clock.now += q.next_due_in(clock.now)
        if q.run_due(clock.now):
            times.append(clock.now)
    return times


def _queue(sender, clock, **kw):
    return RetryQueue(sender, clock=clock, autostart=False, **kw)


def test_idempotency_key_shape():
    assert idempotency_key("abc123", "transcript", 12.5) == "abc123:transcript:12.5"
    assert IDEMPOTENCY_HEADER == "X-Idempotency-Key"


def test_retries_back_off_1s_to_60s_and_carry_the_idempotency_header():
    clock, sender = _Clock(), _Sender(fail=8)
    q = _queue(sender, clock)
    q.submit("k:transcript:1.0", "http://api/t", {"a": 1})
    times = _drain(q, clock)
    assert times[0] == 1.0
    assert [b - a for a, b in zip(times, times[1:])] == [2, 4, 8, 16, 32, 60, 60, 60]
    assert all(h == {IDEMPOTENCY_HEADER: "k:transcript:1.0"} for _, _, h in sender.calls)
    assert all(u == "http://api/t" and p == {"a": 1} for u, p, _ in sender.calls)
    assert q.delivered == 1 and q.gave_up == 0 and len(q) == 0


def test_gives_up_after_ten_minutes():
    clock, sender = _Clock(), _Sender(fail=10 ** 6)
    q = _queue(sender, clock)
    q.submit("k:metrics:2", "u", {})
    times = _drain(q, clock)
    assert q.gave_up == 1 and q.delivered == 0
    assert 540 <= times[-1] <= 660 and len(sender.calls) == len(times)


def test_sender_exception_counts_as_a_retry():
    clock, sender = _Clock(), _Sender(fail=1, raise_instead=True)
    q = _queue(sender, clock)
    q.submit("k:connect:t", "u", {})
    _drain(q, clock)
    assert len(sender.calls) == 2 and q.delivered == 1


def test_permanent_failure_is_not_retried():
    clock = _Clock()
    calls = []

    def sender(url, payload, headers):
        calls.append(url)
        return True  # e.g. a 400: the real sender reports "done" for those

    q = _queue(sender, clock)
    q.submit("k:transcript:3", "u", {})
    _drain(q, clock)
    assert calls == ["u"] and q.delivered == 1


def test_bounded_queue_drops_the_oldest():
    clock = _Clock()
    q = _queue(_Sender(fail=10 ** 6), clock, max_size=3)
    for i in range(1, 5):
        clock.now += 0.1
        q.submit("k%d" % i, "u", {})
    assert len(q) == 3 and q.dropped == 1
    assert sorted(e.key for e in q._entries) == ["k2", "k3", "k4"]


def test_next_due_in_is_none_when_empty_and_never_negative():
    clock = _Clock()
    q = _queue(_Sender(), clock)
    assert q.next_due_in() is None
    q.submit("k", "u", {})
    assert q.next_due_in(0.0) == 1.0
    assert q.next_due_in(5.0) == 0.0
