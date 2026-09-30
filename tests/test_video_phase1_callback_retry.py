"""Phase 1 / audit E.4: video callback posts get a bounded retry queue.

Connect/disconnect/video-metric posts were single-shot; one that raced an API
restart was lost for good. Now the first attempt runs inline, a retryable
failure is queued, replayed with 1 s -> 60 s backoff, abandoned after 10 min,
and the queue holds ~2000 entries dropping the oldest. Every attempt carries
X-Idempotency-Key: <processing_key>:<kind>:<time_stamp>.

Driven with a fake sender and clock; no thread is started (start_thread=False)
and no HTTP happens. requests/config are stubbed if missing (CI).
"""
import logging
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_processing"))

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = types.ModuleType("requests")
sys.modules.setdefault("config", types.SimpleNamespace(initialize=lambda: None))

import callbacks  # noqa: E402
from callbacks import (CallbackRetryQueue, idempotency_key, metrics_time_stamp,  # noqa: E402
                       IDEMPOTENCY_HEADER, RETRY_BACKOFF_MAX, RETRY_GIVE_UP_SECONDS)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Sender:
    """Scripted responses: an int is a status, an Exception instance is raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, dict(headers), timeout))
        r = self.script.pop(0) if self.script else 200
        if isinstance(r, Exception):
            raise r
        return r


def _q(script, clock=None, **kw):
    clock = clock or Clock()
    sender = Sender(script)
    q = CallbackRetryQueue(sender=sender, clock=clock, start_thread=False, timeout=30, **kw)
    return q, sender, clock


def test_first_attempt_inline_with_idempotency_header():
    q, sender, _ = _q([200])
    assert q.post("http://api/x", {"a": 1}, "connect", "pod:connect:2026") is True
    assert len(q) == 0
    url, payload, headers, timeout = sender.calls[0]
    assert (url, payload) == ("http://api/x", {"a": 1})
    assert headers[IDEMPOTENCY_HEADER] == "pod:connect:2026"
    assert timeout == 30  # the 30 s per-attempt timeout is kept


def test_connection_error_is_queued_and_replayed_with_backoff():
    q, sender, clock = _q([ConnectionError("refused"), ConnectionError("refused"),
                           ConnectionError("refused"), 200])
    assert q.post("u", {}, "video metric", "k") is False
    assert len(q) == 1
    assert q.run_due() == 1.0            # not due yet: first backoff is 1 s
    assert len(sender.calls) == 1
    clock.t += 1.0
    q.run_due()                          # attempt 2 fails -> backoff 2 s
    assert len(sender.calls) == 2
    clock.t += 1.0
    q.run_due()
    assert len(sender.calls) == 2        # 1 s later: still waiting on the 2 s backoff
    clock.t += 1.0
    q.run_due()                          # attempt 3 fails -> backoff 4 s
    assert len(sender.calls) == 3
    clock.t += 4.0
    assert q.run_due() is None           # attempt 4 lands; queue empty
    assert len(sender.calls) == 4
    assert q.delivered == 1
    # every replay carried the same idempotency key
    assert {c[2][IDEMPOTENCY_HEADER] for c in sender.calls} == {"k"}


def test_backoff_caps_at_sixty_seconds():
    q, sender, clock = _q([ConnectionError("x")] * 20)
    q.post("u", {}, "n", "k")
    delays = []
    while len(sender.calls) < 10:
        d = q.run_due()
        delays.append(d)
        clock.t += d
    assert max(delays) == RETRY_BACKOFF_MAX == 60.0
    assert delays[-1] == 60.0


def test_5xx_is_retried_but_4xx_is_not():
    q, sender, clock = _q([503, 200])
    assert q.post("u", {}, "n", "k") is False
    assert len(q) == 1
    clock.t += 1.0
    q.run_due()
    assert q.delivered == 1 and len(q) == 0

    q, sender, clock = _q([400])
    assert q.post("u", {}, "n", "k") is False
    assert len(q) == 0  # permanent rejection: nothing to replay


def test_gives_up_after_ten_minutes():
    q, sender, clock = _q([ConnectionError("x")] * 100)
    q.post("u", {}, "n", "k")
    while len(q):
        d = q.run_due()
        if d is None:
            break
        clock.t += d
    assert q.gave_up == 1 and len(q) == 0
    assert RETRY_GIVE_UP_SECONDS == 600
    assert clock.t - 1000.0 >= 600.0
    assert clock.t - 1000.0 < 700.0  # gave up promptly after the window, not much later


def test_queue_is_bounded_and_drops_oldest(caplog):
    caplog.set_level(logging.WARNING)
    q, sender, clock = _q([ConnectionError("x")] * 10, max_entries=3)
    for i in range(5):
        q.post("u", {"i": i}, "n", "k%d" % i)
    assert len(q) == 3
    assert q.dropped == 2
    keys = [it["key"] for it in q._items]
    assert keys == ["k2", "k3", "k4"]  # k0, k1 (oldest) were dropped
    assert any("retry queue full" in r.getMessage() for r in caplog.records)


def test_idempotency_key_contract():
    assert idempotency_key("pod-7", "videometrics", 42) == "pod-7:videometrics:42"
    assert idempotency_key("pod-7", "connect", "2026-09-30 12:00:00.5") == "pod-7:connect:2026-09-30 12:00:00.5"
    # earliest metric time_stamp identifies the batch
    assert metrics_time_stamp({"p1": [[12, "neutral", 1, "screen"], [13, "happy", 2, "peer"]],
                               "p2": [[11, "neutral", 1, "screen"]]}) == 11
    assert isinstance(metrics_time_stamp({}), int)


def test_public_posters_use_the_queue_and_header(monkeypatch):
    sender = Sender([200, 200, 200])
    q = CallbackRetryQueue(sender=sender, clock=Clock(), start_thread=False)
    monkeypatch.setattr(callbacks, "_retry_queue", q)
    monkeypatch.setattr(callbacks, "config", types.SimpleNamespace(
        connect_callback=lambda: "http://api/connect",
        disconnect_callback=lambda: "http://api/disconnect",
        video_metrics_callback=lambda: "http://api/speakervideometrics"))

    assert callbacks.post_connect("pod-1") is True
    url, payload, headers, _ = sender.calls[0]
    assert url == "http://api/connect" and payload["source"] == "pod-1"
    assert headers[IDEMPOTENCY_HEADER] == "pod-1:connect:" + payload["time"]

    assert callbacks.post_disconnect("pod-1") is True
    url, payload, headers, _ = sender.calls[1]
    assert url == "http://api/disconnect"
    assert headers[IDEMPOTENCY_HEADER] == "pod-1:disconnect:" + payload["time"]

    metrics = {"p1": [[30, "neutral", 1, "screen"]]}
    assert callbacks.post_video_metrics("pod-1", metrics) is True
    url, payload, headers, _ = sender.calls[2]
    assert url == "http://api/speakervideometrics"
    assert payload == {"source": "pod-1", "video_metrics": metrics}
    assert headers[IDEMPOTENCY_HEADER] == "pod-1:videometrics:30"


def test_module_queue_starts_no_thread_at_import():
    # Importing callbacks must not spawn the retry thread; it starts lazily
    # on the first queued failure.
    assert callbacks._retry_queue._thread is None
