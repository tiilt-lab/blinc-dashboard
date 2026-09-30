"""Batched live inference: several windows per worker call.

Protocol v2 of crisper_worker.py --serve: the ready line advertises
"batch": true, a request may carry a list of windows and the reply returns
one result per window in the same order (the single form still works).
WorkerPool: when the FIFO head takes a live, batch-capable slot it also
takes up to DC_ASR_BATCH - 1 windows waiting behind it as one request and
routes each result back to its own pod. A worker that did not advertise
batching (an older script, the FakeProc in test_asr_worker_pool.py) keeps
getting one window per request. DC_ASR_MODEL picks the worker's model.

The subprocess is faked as in test_asr_worker_pool.py; nothing touches a GPU.
"""
import io
import json
import logging
import os
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing", "asr_connectors"))

from asr_connectors import crisperwhisper_asr as cw  # noqa: E402
import crisper_worker  # noqa: E402


# --- the worker's protocol -----------------------------------------------------

class FakeModel:
    """Stands in for CrisperWhisperModel: echoes the path as the one word."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = []

    def transcribe(self, audio, language, mode, word_timestamps):
        self.calls.append((audio, language, mode))
        if audio in self.fail_on:
            raise FileNotFoundError(audio)
        word = types.SimpleNamespace(word=audio, start=0.0, end=1.0)
        return types.SimpleNamespace(text=audio, words=[word])


def test_single_form_request_is_unchanged():
    model = FakeModel()
    data = crisper_worker.handle(model, {"audio": "a.wav"}, "en", "verbatim")
    assert data == {"text": "a.wav", "words": [["a.wav", 0.0, 1.0]]}
    assert model.calls == [("a.wav", "en", "verbatim")]


def test_list_form_returns_one_result_per_window_in_order_with_ids():
    model = FakeModel(fail_on={"b.wav"})
    job = {"windows": [{"id": "A", "audio": "a.wav"}, {"id": "B", "audio": "b.wav"},
                       {"id": 7, "audio": "c.wav", "mode": "intended"}]}
    data = crisper_worker.handle(model, job, "en", "verbatim")
    assert data == {"results": [
        {"id": "A", "text": "a.wav", "words": [["a.wav", 0.0, 1.0]]},
        {"id": "B", "error": "b.wav"},                       # its own failure only
        {"id": 7, "text": "c.wav", "words": [["c.wav", 0.0, 1.0]]},
    ]}
    assert [c[2] for c in model.calls] == ["verbatim", "verbatim", "intended"]   # sequential, in order
    assert crisper_worker.handle(model, {"windows": [{"audio": "d.wav"}]}, "en", "verbatim") == {
        "results": [{"id": 0, "text": "d.wav", "words": [["d.wav", 0.0, 1.0]]}]}


def test_serve_loop_advertises_batching_survives_bad_lines_and_exits():
    out = io.StringIO()
    lines = ['{"audio": "a.wav"}', "", "not json", '{"windows": [{"id": 1, "audio": "b.wav"}]}',
             '{"exit": true}', '{"audio": "never.wav"}']
    model = FakeModel()
    crisper_worker.serve(model, lines, out, "en", "verbatim", model_id="m")
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert replies[0] == {"ready": True, "protocol": 2, "batch": True, "model": "m"}
    assert replies[1]["words"] == [["a.wav", 0.0, 1.0]]
    assert "error" in replies[2]
    assert replies[3] == {"results": [{"id": 1, "text": "b.wav", "words": [["b.wav", 0.0, 1.0]]}]}
    assert len(replies) == 4 and "never.wav" not in [c[0] for c in model.calls]


# --- the fake --serve subprocess ------------------------------------------------

class FakeProc:
    def __init__(self, slot, harness):
        self.slot = slot
        self.h = harness
        self.stdin = self.stdout = self
        self.pid = 1000 + slot.index
        self._job = None
        self._rc = None
        self.killed = False

    def write(self, line):
        self._job = json.loads(line)

    def flush(self):
        pass

    def readline(self):
        job, self._job = self._job, None
        if job is None:
            hello = {"ready": True}
            if self.h.batch_capable:
                hello.update(protocol=2, batch=True)
            return json.dumps(hello) + "\n"
        if "windows" in job:
            windows = job["windows"]
            paths = [w["audio"] for w in windows]
        else:
            windows = None
            paths = [job["audio"]]
        self.h.requests.append((paths, self.slot.index))
        time.sleep(self.h.service * len(paths))              # sequential inside the worker
        if any(p in self.h.die_on for p in paths):
            self.die(137)
            return ""                                        # the pipe closed mid-request
        for p in paths:
            self.h.served.append((p, self.slot.index))
        if windows is None:
            return json.dumps({"words": [[paths[0], 0.0, 1.0]]}) + "\n"
        return json.dumps({"results": [{"id": w["id"], "words": [[w["audio"], 0.0, 1.0]]}
                                       for w in windows]}) + "\n"

    def poll(self):
        return self._rc

    def die(self, rc=1):
        self._rc = rc

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        pass


class FakeSlot(cw._WorkerSlot):
    def __init__(self, index, harness):
        super().__init__(index)
        self.h = harness

    def _launch(self):
        self.h.spawns.append(self.index)
        return FakeProc(self, self.h)

    def _read_reply(self, timeout):
        return self._proc.stdout.readline()


class Harness:
    def __init__(self, size=1, service=0.05, batch=None, batch_capable=True,
                 spawn_grace=0.05, **pool_kw):
        self.service = service
        self.batch_capable = batch_capable
        self.spawns = []
        self.requests = []      # (paths in the request, slot index)
        self.served = []        # (window, slot index) in completion order
        self.die_on = set()
        self.results = {}       # pod -> [result dicts]
        self.errors = {}        # pod -> [exceptions]
        self.pool = cw.WorkerPool(size=size, slot_factory=lambda i: FakeSlot(i, self),
                                  spawn_grace=spawn_grace, batch=batch, **pool_kw)

    def pod(self, name, windows=1):
        """A pod's flush thread: its windows one after another, in order."""
        def run():
            for k in range(windows):
                try:
                    self.results.setdefault(name, []).append(self.pool.transcribe("%s-%d" % (name, k)))
                except Exception as e:
                    self.errors.setdefault(name, []).append(e)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def pods(self, names, windows=1, spacing=0.01):
        threads = []
        for name in names:
            threads.append(self.pod(name, windows))
            time.sleep(spacing)
        return threads

    def order(self):
        return [w for w, _ in self.served]


@pytest.fixture
def plenty_of_vram(monkeypatch):
    monkeypatch.setattr(cw, "free_vram_mib", lambda run=None: 8000)


def join(threads):
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()


# --- batch size knob -----------------------------------------------------------

def test_batch_size_comes_from_dc_asr_batch(monkeypatch):
    monkeypatch.delenv("DC_ASR_BATCH", raising=False)
    assert cw.batch_size() == 4 and cw.WorkerPool(size=1).batch == 4
    monkeypatch.setenv("DC_ASR_BATCH", "2")
    assert cw.batch_size() == 2 and cw.WorkerPool(size=1).batch == 2
    monkeypatch.setenv("DC_ASR_BATCH", "0")
    assert cw.batch_size() == 1                              # never less than one window
    monkeypatch.setenv("DC_ASR_BATCH", "many")
    assert cw.batch_size() == 4
    assert cw.WorkerPool(size=1, batch=8).batch == 8


# --- dispatch ------------------------------------------------------------------

def test_waiting_windows_go_to_the_free_slot_as_one_request_in_fifo_order(plenty_of_vram):
    h = Harness(size=1, service=0.05)
    h.pool.transcribe("warm-0")
    threads = h.pods("XABC", spacing=0.01)                   # X occupies the slot; A, B, C queue up
    join(threads)
    assert h.requests[:2] == [(["warm-0"], 0), (["X-0"], 0)]
    assert h.requests[2] == (["A-0", "B-0", "C-0"], 0)       # one request, arrival order
    assert h.order() == ["warm-0", "X-0", "A-0", "B-0", "C-0"]
    for name in "XABC":                                      # each pod got ITS window's result
        assert h.results[name][0]["words"][0][0] == "%s-0" % name
    assert h.pool.waiting == 0 and h.errors == {}


def test_a_batch_is_capped_by_dc_asr_batch(plenty_of_vram):
    h = Harness(size=1, service=0.05, batch=2)
    h.pool.transcribe("warm-0")
    join(h.pods("XABCD", spacing=0.01))
    assert [p for p, _ in h.requests[2:]] == [["A-0", "B-0"], ["C-0", "D-0"]]


def test_batch_of_one_means_one_window_per_request(plenty_of_vram):
    h = Harness(size=1, service=0.05, batch=1)
    h.pool.transcribe("warm-0")
    join(h.pods("XABC", spacing=0.01))
    assert all(len(p) == 1 for p, _ in h.requests)
    assert h.order() == ["warm-0", "X-0", "A-0", "B-0", "C-0"]


def test_a_worker_that_did_not_advertise_batching_gets_one_window_per_request(plenty_of_vram):
    h = Harness(size=1, service=0.05, batch_capable=False)
    h.pool.transcribe("warm-0")
    join(h.pods("XABC", spacing=0.01))
    assert not h.pool.slots[0].batch_capable
    assert all(len(p) == 1 for p, _ in h.requests)
    assert h.order() == ["warm-0", "X-0", "A-0", "B-0", "C-0"]


def test_a_slot_that_still_has_to_spawn_takes_one_window(plenty_of_vram):
    h = Harness(size=2, service=0.2)
    h.pool.transcribe("warm-0")                              # slot 0 up
    join(h.pods("XABC", spacing=0.01))                       # X on slot 0; A waits past the grace
    assert h.spawns == [0, 1]
    first_on_1 = next(p for p, i in h.requests if i == 1)
    assert first_on_1 == ["A-0"]                             # spawned for A alone; B, C stayed queued


def test_per_pod_order_holds_and_no_batch_carries_two_windows_of_one_pod(plenty_of_vram):
    h = Harness(size=2, service=0.03)
    join(h.pods("ABCDE", windows=4, spacing=0.005))
    for name in "ABCDE":
        assert [w for w in h.order() if w.startswith(name)] == ["%s-%d" % (name, k) for k in range(4)]
        assert [r["words"][0][0] for r in h.results[name]] == ["%s-%d" % (name, k) for k in range(4)]
    for paths, _ in h.requests:
        assert len({p[0] for p in paths}) == len(paths)     # distinct pods within a request
        assert len(paths) <= 4
    assert any(len(p) > 1 for p, _ in h.requests)            # batching actually happened
    assert h.errors == {}


def test_capacity_warning_still_fires_with_batching(plenty_of_vram, caplog):
    h = Harness(size=1, service=0.2, capacity_wait=0.05, warning_interval=60.0)
    h.pool.transcribe("warm-0")
    with caplog.at_level(logging.WARNING):
        join(h.pods("XABC", spacing=0.01))
    hits = [r for r in caplog.records if r.getMessage().startswith(cw.CAPACITY_WARNING)]
    assert len(hits) == 1 and "waited" in hits[0].getMessage()


# --- failure paths --------------------------------------------------------------

def test_a_worker_death_mid_batch_fails_every_window_of_it_and_degrades_the_slot(plenty_of_vram):
    h = Harness(size=1, service=0.05)
    h.pool.transcribe("warm-0")
    h.die_on = {"B-0"}
    told = []
    h.pool.add_listener(lambda s, m: told.append((s, m)))
    join(h.pods("XABC", spacing=0.01))
    assert h.requests[2][0] == ["A-0", "B-0", "C-0"]
    assert not any(h.results.get(n) for n in "ABC")
    assert all(len(h.errors[n]) == 1 for n in "ABC")        # every pod in the batch hears about it
    assert isinstance(h.errors["A"][0], RuntimeError) and isinstance(h.errors["B"][0], RuntimeError)
    assert told == [("degraded", cw.MSG_UNRESPONSIVE)] and h.pool.slots[0].backoff.failures == 1
    assert h.pool.waiting == 0


def test_a_batch_fails_over_intact_when_its_slot_turns_out_dead(plenty_of_vram):
    h = Harness(size=2, service=0.1, spawn_grace=1.0)        # A, B wait for slot 0 rather than spawn
    h.pool.transcribe("warm-0")                              # slot 0 up and batch-capable
    threads = h.pods("XAB", spacing=0.01)                    # X running; A, B waiting
    time.sleep(0.03)
    h.pool.slots[0]._proc.die(137)                           # OOM-killed while serving X
    join(threads)
    assert h.results["X"][0]["words"][0][0] == "X-0"         # X's reply was already on the pipe
    assert h.requests[2:] == [(["A-0", "B-0"], 1)]           # slot 0 refused; the batch moved whole
    assert h.spawns == [0, 1]
    assert h.errors == {} and h.pool.state == "ok" and h.pool.slots[0].state == "degraded"


def test_followers_are_refused_with_the_leader_when_no_slot_can_serve(monkeypatch):
    monkeypatch.setattr(cw, "free_vram_mib", lambda run=None: 8000)
    h = Harness(size=1, service=0.1)
    h.pool.transcribe("warm-0")
    threads = h.pods("XAB", spacing=0.01)
    time.sleep(0.03)
    h.pool.slots[0]._proc.die(137)                           # dies under X; A leads B, slot refuses
    join(threads)
    assert all(isinstance(h.errors[n][0], cw.WorkerUnavailable) for n in "AB")
    assert h.pool.waiting == 0 and h.pool.state == "degraded"


def test_slot_transcribe_many_loops_when_the_worker_lacks_batching_and_batches_when_it_has_it():
    class Proc:
        def __init__(self):
            self.stdin = io.StringIO()
            self.lines = []

        def poll(self):
            return None

        def kill(self):
            pass

    w = cw._WorkerSlot()
    w._proc = Proc()
    replies = ['{"words": [["a", 0, 1]]}\n', '{"words": [["b", 0, 1]]}\n']
    monkeypatch_read = lambda timeout: replies.pop(0)  # noqa: E731
    w._read_reply = monkeypatch_read
    assert w.transcribe_many(["a.wav", "b.wav"]) == [{"words": [["a", 0, 1]]}, {"words": [["b", 0, 1]]}]
    assert [json.loads(l) for l in w._proc.stdin.getvalue().splitlines()] == [
        {"audio": "a.wav"}, {"audio": "b.wav"}]
    w._proc = Proc()
    w.batch_capable = True
    replies = ['{"results": [{"id": 0, "words": [["a", 0, 1]]}, {"id": 1, "error": "bad"}]}\n']
    assert w.transcribe_many(["a.wav", "b.wav"]) == [{"id": 0, "words": [["a", 0, 1]]}, {"id": 1, "error": "bad"}]
    assert json.loads(w._proc.stdin.getvalue()) == {"windows": [{"id": 0, "audio": "a.wav"}, {"id": 1, "audio": "b.wav"}]}
    w._proc = Proc()
    replies = ['{"results": [{"id": 0, "words": []}]}\n']    # wrong length: a broken worker
    with pytest.raises(RuntimeError):
        w.transcribe_many(["a.wav", "b.wav"])
    assert w._proc is None and w.state == "degraded"
    w._proc = Proc()
    w.batch_capable = True
    replies = ['{"error": "cannot parse"}\n']                # whole request refused: per-window errors
    assert w.transcribe_many(["a.wav", "b.wav"]) == [{"error": "cannot parse"}] * 2


# --- model knob ----------------------------------------------------------------

def test_live_model_is_dc_asr_model_else_config_else_default(monkeypatch):
    assert cw.live_model({"DC_ASR_MODEL": "nyralabs/CrisperWhisper2.0_turbo"}) == "nyralabs/CrisperWhisper2.0_turbo"
    fake_config = types.SimpleNamespace(crisperwhisper_model=lambda: "from-config",
                                        crisperwhisper_mode=lambda: "verbatim")
    monkeypatch.setitem(sys.modules, "config", fake_config)
    assert cw.live_model({}) == "from-config"
    assert cw.live_model({"DC_ASR_MODEL": "  "}) == "from-config"

    def broken():
        raise RuntimeError("config not initialised")

    monkeypatch.setitem(sys.modules, "config", types.SimpleNamespace(crisperwhisper_model=broken))
    assert cw.live_model({}) == cw.DEFAULT_MODEL == "nyralabs/CrisperWhisper2.0_large"


def test_launch_passes_the_model_to_the_worker(monkeypatch):
    argvs = []
    fake_config = types.SimpleNamespace(crisperwhisper_model=lambda: "from-config",
                                        crisperwhisper_mode=lambda: "verbatim")
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setattr(cw, "_worker_python", lambda: "python")
    monkeypatch.setattr(cw.subprocess, "Popen", lambda argv, **kw: argvs.append(argv))
    slot = cw._WorkerSlot(0)
    monkeypatch.setattr(slot, "_stderr_file", lambda: io.BytesIO())
    monkeypatch.setenv("DC_ASR_MODEL", "nyralabs/CrisperWhisper2.0_turbo")
    slot._launch()
    monkeypatch.delenv("DC_ASR_MODEL")
    slot._launch()
    assert argvs[0][1:] == [cw._WORKER, "--serve", "--model", "nyralabs/CrisperWhisper2.0_turbo", "--mode", "verbatim"]
    assert argvs[1][3:5] == ["--model", "from-config"]


def test_worker_default_model_and_env(monkeypatch):
    assert crisper_worker.DEFAULT_MODEL == cw.DEFAULT_MODEL
    assert crisper_worker.MODEL_ENV == cw.MODEL_ENV == "DC_ASR_MODEL"
