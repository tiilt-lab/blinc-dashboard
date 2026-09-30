"""Live-pod presence keys (src/common/live_presence.py) — Phase 2 item 14.

The audio server holds ``live_pod:<processing_key>`` with a 60 s TTL while a
pod streams and refreshes it every 20 s; the post-hoc side defers GPU work
while any such key exists. Behaviour is tested against a fake Redis; the
audio server's wiring is a source contract (server.py loads models at import).
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

import live_presence  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class FakeRedis:
    """SET/DELETE/SCAN with TTL bookkeeping, driven by an explicit clock."""

    def __init__(self):
        self.store = {}   # key -> (value, expires_at)
        self.now = 1000.0

    def _alive(self, k):
        v = self.store.get(k)
        if v is None:
            return False
        if v[1] is not None and v[1] <= self.now:
            del self.store[k]
            return False
        return True

    def set(self, k, v, ex=None):
        self.store[k] = (v, None if ex is None else self.now + ex)

    def get(self, k):
        return self.store[k][0] if self._alive(k) else None

    def delete(self, k):
        return 1 if self.store.pop(k, None) is not None else 0

    def scan_iter(self, match="*", count=None):
        prefix = match.rstrip("*")
        for k in list(self.store):
            if k.startswith(prefix) and self._alive(k):
                yield k

    def ttl(self, k):
        return self.store[k][1] - self.now if self._alive(k) else -2


def test_key_format_is_the_shared_contract():
    assert live_presence.PREFIX == "live_pod:"
    assert live_presence.make_key("abc-123") == "live_pod:abc-123"
    assert live_presence.DEFAULT_TTL == 60
    assert live_presence.REFRESH_SECONDS == 20
    assert live_presence.REFRESH_SECONDS < live_presence.DEFAULT_TTL / 2


def test_mark_sets_value_one_with_the_ttl():
    r = FakeRedis()
    live_presence.mark_live(r, "podA")
    assert r.get("live_pod:podA") == "1"
    assert r.ttl("live_pod:podA") == 60
    live_presence.mark_live(r, "podB", ttl=5)
    assert r.ttl("live_pod:podB") == 5


def test_refresh_extends_and_expiry_clears_without_a_delete():
    r = FakeRedis()
    live_presence.mark_live(r, "podA")
    r.now += 50
    live_presence.mark_live(r, "podA")      # refresh
    r.now += 50                              # 100 s after the first set
    assert live_presence.live_count(r) == 1  # the refresh kept it alive
    r.now += 11
    assert live_presence.live_count(r) == 0  # a crashed process leaves nothing


def test_clear_and_count_and_keys():
    r = FakeRedis()
    r.set("posthoc_ticket:x", "1")           # unrelated keys are not counted
    live_presence.mark_live(r, "p1")
    live_presence.mark_live(r, "p2")
    assert live_presence.live_count(r) == 2
    assert sorted(live_presence.live_keys(r)) == ["p1", "p2"]
    live_presence.clear_live(r, "p1")
    assert live_presence.live_keys(r) == ["p2"]
    live_presence.clear_live(r, "p1")        # idempotent
    assert live_presence.live_count(r) == 1


def test_bytes_keys_from_an_undecoded_client_are_handled():
    class BytesRedis(FakeRedis):
        def scan_iter(self, match="*", count=None):
            for k in super().scan_iter(match, count):
                yield k.encode()

    r = BytesRedis()
    live_presence.mark_live(r, "p9")
    assert live_presence.live_keys(r) == ["p9"]


# ---- audio server wiring (source contract) --------------------------------

def _server():
    with open(os.path.join(SRC, "audio_processing", "server.py")) as f:
        return f.read()


def test_audio_server_marks_on_start_refreshes_and_clears_on_end():
    s = _server()
    assert "import live_presence" in s
    # marked when the pod starts streaming, on a 20 s LoopingCall
    start = s[s.index("def signal_start"):s.index("def _mark_live")]
    assert "self._start_presence()" in start
    assert "task.LoopingCall(self._mark_live)" in s
    assert "start(live_presence.REFRESH_SECONDS, now=True)" in s
    # Redis I/O never runs on the reactor
    mark = s[s.index("def _mark_live"):s.index("def _start_presence")]
    assert "reactor_safety.defer_blocking(" in mark and "live_presence.mark_live(" in mark
    stop = s[s.index("def _stop_presence"):s.index("def send_close")]
    assert "lc.stop()" in stop and "live_presence.clear_live(" in stop
    assert "reactor_safety.defer_blocking(" in stop
    # cleared on end/disconnect, before the ASR/processor teardown
    end = s[s.index("def signal_end"):s.index("if __name__")]
    assert end.index("self._stop_presence()") < end.index("self.asr.stop()")


def test_audio_server_uses_the_processing_key_as_the_pod_key():
    s = _server()
    for fn in ("def _mark_live", "def _stop_presence"):
        body = s[s.index(fn):]
        body = body[:body.index("\n    def ", 10)]
        assert re.search(r"key = self\.config\.auth_key", body), fn
