"""Observability: the blinc_metrics wrapper, the metric-name contracts, the
monitoring stack's configuration, and the wiring of every service's exporter
(deploy/monitoring/README.md).

Nothing here needs a running service, Flask, Twisted or a GPU: the wrapper
is exercised on private registries (and in a subprocess for gunicorn's
multiprocess mode), the accessors get fakes, NVML is stubbed, and the service
entry points are read as source. promtool is not installed, so prometheus.yml
and the rules are validated structurally with PyYAML.
"""
import glob
import http.client
import importlib.util
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
MON = os.path.join(REPO, "deploy", "monitoring")
for path in (os.path.join(SRC, "common"), os.path.join(SRC, "audio_processing")):
    if path not in sys.path:
        sys.path.insert(0, path)

import blinc_metrics as bm  # noqa: E402
from prometheus_client import CollectorRegistry  # noqa: E402

yaml = pytest.importorskip("yaml")

EXPECTED_PORTS = {"coordinator": 9110, "audio": 9111, "video": 9112,
                  "audio_posthoc": 9113, "video_posthoc": 9114, "gpu": 9115}

# Every file that defines metrics through the wrapper.
METRIC_SOURCES = [
    ("common", "blinc_metrics.py"),
    ("server", "routes", "metrics.py"),
    ("server", "coordinator.py"),
    ("audio_processing", "live_metrics.py"),
    ("audio_processing", "server_posthoc.py"),
    ("video_processing", "server_posthoc.py"),
]
_DEFINE = re.compile(r"\b(?:bm\.)?(counter|gauge|histogram|callback_gauge|callback_counter)\(\s*['\"]([a-z0-9_]+)['\"]")


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def _yaml(*parts):
    return yaml.safe_load(_read(*parts))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def exported_names():
    """Every metric name the code exports (plus the documented video ones)."""
    names = set()
    sources = [_read("src", *parts) for parts in METRIC_SOURCES]
    sources.append(_read("deploy", "monitoring", "gpu_exporter.py"))
    for text in sources:
        for kind, name in _DEFINE.findall(text):
            full = bm.full_name(name)
            names.add(full)
            if kind == "histogram":
                names.update({full + "_bucket", full + "_count", full + "_sum"})
    names.update(re.findall(r"blinc_video_[a-z0-9_]+", _read("deploy", "monitoring", "README.md")))
    return names


@pytest.fixture
def registry():
    bm._reset_for_tests()
    reg = CollectorRegistry()
    yield reg
    bm._reset_for_tests()


# --- the wrapper -----------------------------------------------------------

def test_names_get_the_prefix_and_repeat_calls_return_the_same_metric(registry):
    c = bm.counter("things_total", "doc", ("kind",), registry=registry)
    g = bm.gauge("depth", "doc", registry=registry)
    h = bm.histogram("took_seconds", "doc", ("op",), buckets=(0.1, 1, 10), registry=registry)
    c.labels("a").inc(3)
    g.set(7)
    h.labels("x").observe(0.5)
    assert bm.counter("things_total", "other doc", ("kind",), registry=registry) is c
    assert bm.gauge("depth", "doc", registry=registry) is g
    text = bm.render(registry).decode()
    assert 'blinc_things_total{kind="a"} 3.0' in text
    assert "blinc_depth 7.0" in text
    assert 'blinc_took_seconds_bucket{le="1.0",op="x"} 1.0' in text
    assert "blinc_took_seconds_created" not in text, "created twins are disabled"
    assert bm.full_name("blinc_x") == "blinc_x" and bm.full_name("x") == "blinc_x"


def test_callback_gauges_are_computed_at_scrape_and_failures_are_skipped(registry):
    state = {"n": 1}
    assert bm.callback_gauge("cb_plain", "doc", lambda: state["n"], registry=registry) is True
    assert bm.callback_gauge("cb_plain", "doc", lambda: 99, registry=registry) is False, "second registration is ignored"
    bm.callback_gauge("cb_labelled", "doc", lambda: {("a",): 1, ("b",): 2}, labels=("k",), registry=registry)
    bm.callback_counter("cb_events_total", "doc", lambda: 5, registry=registry)

    def boom():
        raise RuntimeError("redis down")
    bm.callback_gauge("cb_broken", "doc", boom, registry=registry)
    text = bm.render(registry).decode()
    assert "blinc_cb_plain 1.0" in text
    state["n"] = 2
    assert "blinc_cb_plain 2.0" in bm.render(registry).decode()
    assert 'blinc_cb_labelled{k="b"} 2.0' in text
    assert "blinc_cb_events_total 5.0" in text and "# TYPE blinc_cb_events_total counter" in text
    assert "blinc_cb_broken" not in text.replace("# HELP blinc_cb_broken", "").replace("# TYPE blinc_cb_broken gauge", "")


def test_process_metrics_are_sampled_with_service_and_pid(registry, monkeypatch):
    monkeypatch.delenv(bm.MULTIPROC_ENV, raising=False)
    bm.install_process_metrics("unit_test", interval=3600, registry=registry)
    text = bm.render(registry).decode()
    pid = str(os.getpid())
    m = re.search(r'blinc_process_rss_bytes\{pid="%s",service="unit_test"\} ([0-9.e+]+)' % pid, text)
    assert m and float(m.group(1)) > 0
    for name in ("blinc_process_threads", "blinc_process_open_fds", "blinc_process_uptime_seconds",
                 "blinc_process_cpu_seconds_total", "blinc_service_up"):
        assert '%s{pid="%s",service="unit_test"}' % (name, pid) in text, name


def test_port_for_reads_the_env_override_and_zero_disables(monkeypatch):
    assert bm.DEFAULT_PORTS == EXPECTED_PORTS
    monkeypatch.delenv(bm.ENV_PORT, raising=False)
    assert bm.port_for("audio") == 9111
    monkeypatch.setenv(bm.ENV_PORT, "9555")
    assert bm.port_for("audio") == 9555
    monkeypatch.setenv(bm.ENV_PORT, "0")
    assert bm.port_for("gpu") == 0
    assert bm.start_exporter(0) is False
    monkeypatch.setenv(bm.ENV_PORT, "nope")
    assert bm.port_for("gpu") == 0


def test_exporter_serves_metrics_on_loopback_and_is_idempotent(registry):
    port = _free_port()
    bm.gauge("served", "doc", registry=registry).set(42)
    assert bm.start_exporter(port, registry=registry) is True
    assert bm.start_exporter(port, registry=registry) is True, "second call is a no-op"
    assert bm.exporter_running(port)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/metrics")
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200 and "blinc_served 42.0" in body
        conn.close()
    finally:
        server, _thread = bm._started.pop(port)
        server.shutdown()
        server.server_close()


def test_everything_is_a_noop_without_prometheus_client(monkeypatch):
    monkeypatch.setattr(bm, "_pc", None)
    assert bm.available() is False
    c = bm.counter("x_total", "doc")
    c.labels("a").inc()
    assert c is bm.NOOP
    assert bm.callback_gauge("y", "doc", lambda: 1) is False
    assert bm.start_exporter(_free_port()) is False
    assert bm.render() == b""
    assert bm.install_process_metrics("svc") is None


MULTIPROC_SCRIPT = r"""
import os, sys, time
sys.path.insert(0, %(common)r)
import blinc_metrics as bm
c = bm.counter('mp_events_total', 'doc')
g = bm.gauge('mp_clients', 'doc', multiprocess_mode='livesum')
pids = []
for n in (2, 3):
    pid = os.fork()
    if pid == 0:
        bm.install_process_metrics('api', interval=3600)
        c.inc(n); g.set(1)
        os._exit(0)
    pids.append(pid)
for pid in pids:
    os.waitpid(pid, 0)
text = bm.render().decode()
print('EVENTS', [l for l in text.splitlines() if l.startswith('blinc_mp_events_total')])
print('CLIENTS', [l for l in text.splitlines() if l.startswith('blinc_mp_clients')])
rss = [l for l in text.splitlines() if l.startswith('blinc_process_rss_bytes')]
print('RSS', len(rss), all('pid="' in l and 'service="api"' in l for l in rss))
from prometheus_client import multiprocess
multiprocess.mark_process_dead(pids[0])
text = bm.render().decode()
print('RSS_AFTER', len([l for l in text.splitlines() if l.startswith('blinc_process_rss_bytes')]))
"""


def test_multiprocess_mode_merges_every_worker_and_drops_dead_ones(tmp_path):
    env = dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(tmp_path))
    env.pop("prometheus_multiproc_dir", None)
    out = subprocess.run([sys.executable, "-c", MULTIPROC_SCRIPT % {"common": os.path.join(SRC, "common")}],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "EVENTS ['blinc_mp_events_total 5.0']" in out.stdout, out.stdout
    assert "CLIENTS ['blinc_mp_clients 2.0']" in out.stdout, out.stdout
    assert "RSS 2 True" in out.stdout, out.stdout
    assert "RSS_AFTER 1" in out.stdout, out.stdout


# --- gunicorn hooks ----------------------------------------------------------

def _load_conf(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    spec = importlib.util.spec_from_file_location(
        "blinc_gunicorn_conf_metrics", os.path.join(SRC, "server", "gunicorn.conf.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gunicorn_conf_owns_the_multiprocess_dir_at_start_and_child_exit(monkeypatch, tmp_path):
    conf = _load_conf(monkeypatch)
    assert "PROMETHEUS_MULTIPROC_DIR" not in os.environ, "set only in on_starting, never at import"
    monkeypatch.setenv("DC_PORT", "5999")
    assert conf._multiproc_dir() == "/dev/shm/blinc-prometheus-5999", "private per instance"
    target = tmp_path / "mp"
    target.mkdir()
    (target / "counter_1.db").write_bytes(b"stale")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(target))
    conf.on_starting(None)
    assert os.environ["PROMETHEUS_MULTIPROC_DIR"] == str(target)
    assert os.listdir(target) == [], "wiped at master start (pids are reused)"
    (target / "gauge_liveall_4242.db").write_bytes(b"")
    (target / "gauge_livesum_4242.db").write_bytes(b"")
    (target / "counter_4242.db").write_bytes(b"")

    class _Log:
        def warning(self, *a, **k):
            raise AssertionError("child_exit must not fail: %s" % (a,))

    conf.child_exit(types.SimpleNamespace(log=_Log()), types.SimpleNamespace(pid=4242))
    assert sorted(os.listdir(target)) == ["counter_4242.db"], "live gauges of the dead worker are dropped"


# --- API route + registration -----------------------------------------------

def test_metrics_route_is_loopback_only_and_hooks_every_request():
    src = _read("src", "server", "routes", "metrics.py")
    route = src[src.index("@api_routes.route('/metrics'"):]
    assert "@wrappers.verify_local" in route.split("def metrics")[0]
    assert "@api_routes.before_app_request" in src and "@api_routes.after_app_request" in src
    assert "request.url_rule" in src, "the route label is the matched rule, never the raw path"
    assert "response.status_code == 429" in src, "rate-limit hits are counted from the 429s"
    assert "socketio.server.eio.sockets" in src and "db.engine.pool" in src
    assert "posthoc_failed:*" in src and "live_count(r)" in src
    assert "multiprocess_mode='livesum'" in src
    assert "api_routes.record_once(" in src, "samplers start on registration, not import"
    dc = _read("src", "server", "discussion_capture.py")
    assert "from routes.metrics import api_routes as metrics_api" in dc
    assert "app.register_blueprint(metrics_api)" in dc


def test_coordinator_exports_on_its_port_with_its_own_gauges():
    src = _read("src", "server", "coordinator.py")
    assert "def install_metrics():" in src
    assert "bm.start_exporter(bm.port_for('coordinator'))" in src
    assert "install_metrics()" in src[src.index("def main():"):]
    for name in ("device_connections", "posthoc_runner_alive", "scheduler_job_runs_total"):
        assert "'%s'" % name in src, name
    assert "register_shared_gauges()" in src
    assert "scheduler.add_listener(" in src


# --- live audio ------------------------------------------------------------

def test_audio_server_wires_the_accessor_module_not_the_connectors():
    src = _read("src", "audio_processing", "server.py")
    assert "import live_metrics" in src
    assert "live_metrics.install(cm, retry_queue=getattr(callbacks, '_RETRIES', None))" in src
    main = src[src.index("if __name__ == '__main__':"):]
    assert main.index("reactor.listenTCP(") < main.index("live_metrics.install(") < main.index("reactor.run()")
    end = src[src.index("def signal_end"):src.index("if __name__")]
    assert "live_metrics.retire(self._ingest)" in end
    status = src[src.index("def _send_asr_status"):src.index("def signal_end")]
    assert "self.asr_state = state" in status
    lm = _read("src", "audio_processing", "live_metrics.py")
    assert "bm.start_exporter(bm.port_for('audio')" in lm
    assert "def pool_stats():" in lm
    for name in ("audio_window_backlog_seconds", "audio_chunks_dropped_total", "audio_asr_degraded",
                 "asr_slots", "audio_callback_retries_total"):
        assert "'%s'" % name in lm, name
    # asr_connectors/* belong to another workstream: read only, never edited here.
    for path in glob.glob(os.path.join(SRC, "audio_processing", "asr_connectors", "*.py")):
        with open(path, encoding="utf-8") as f:
            assert "blinc_metrics" not in f.read(), path


class _FakeIngest:
    def __init__(self, received, dropped):
        self.chunks_received = received
        self.chunks_dropped = dropped
        self.samples_received = received * 1360
        self.samples_dropped = dropped * 1360


class _FakeAsr:
    WINDOW_SECONDS = 12.0

    def __init__(self, pending):
        self._window_queue = queue.Queue()
        for _ in range(pending):
            self._window_queue.put(object())


def _proto(key, running=True, stream="audio", pending=0, received=0, dropped=0, asr_state="ok", queued=0):
    p = types.SimpleNamespace(running=running, stream_data=stream,
                              config=types.SimpleNamespace(auth_key=key),
                              _ingest=_FakeIngest(received, dropped), asr=_FakeAsr(pending),
                              asr_state=asr_state, asr_audio_queue=queue.Queue())
    for _ in range(queued):
        p.asr_audio_queue.put(b"x")
    return p


class _FakeCm:
    def __init__(self, protocols):
        self.lock = threading.RLock()
        self.connections = list(protocols)

    def get_number_of_connections(self):
        return len(self.connections)


class _FakeRetries:
    def stats(self):
        return {"size": 4, "delivered": 10, "gave_up": 1, "dropped": 2}


class _FakeSlot:
    def __init__(self, alive, state):
        self.alive = alive
        self.state = state


def _fake_pool_module(size=2, busy=1, alive=2, degraded=0, waiting=3, state="ok"):
    slots = [_FakeSlot(i < alive, "degraded" if i < degraded else "ok") for i in range(size)]
    pool = types.SimpleNamespace(size=size, slots=slots, _busy=set(slots[:busy]), waiting=waiting, state=state)
    module = types.ModuleType("asr_connectors.crisperwhisper_asr")
    module._pool = pool
    package = types.ModuleType("asr_connectors")
    package.crisperwhisper_asr = module
    return package, module


def test_live_metrics_accessors_read_existing_objects(registry, monkeypatch):
    import live_metrics as lm
    monkeypatch.setenv(bm.ENV_PORT, "0")  # no listener in tests
    monkeypatch.setattr(lm, "_retired", {"chunks_received": 0, "chunks_dropped": 0,
                                         "samples_received": 0, "samples_dropped": 0})
    package, module = _fake_pool_module()
    monkeypatch.setitem(sys.modules, "asr_connectors", package)
    monkeypatch.setitem(sys.modules, "asr_connectors.crisperwhisper_asr", module)
    live = _proto("k1", pending=2, received=100, dropped=5, queued=7)
    degraded = _proto("k2", received=50, asr_state="degraded")
    enrolling = _proto("k3", running=False, stream="audio-video-fingerprint", received=9)
    cm = _FakeCm([live, degraded, enrolling])
    assert lm.install(cm, retry_queue=_FakeRetries(), registry=registry) is False

    assert lm.live_protocols() == [live, degraded]
    assert lm.window_backlog_seconds() == {("k1",): 24.0, ("k2",): 0.0}
    assert lm.ingest_queue_chunks() == {("k1",): 7, ("k2",): 0}
    assert lm.asr_degraded_by_pod() == {("k1",): 0, ("k2",): 1}
    assert lm.ingest_totals()["chunks_received"] == 150
    assert lm.pool_stats() == {"size": 2, "busy": 1, "alive": 2, "degraded": 0, "waiting": 3, "state": "ok"}

    # A pod that ends keeps its totals in the exported counters (monotonic).
    lm.retire(live._ingest)
    cm.connections.remove(live)
    totals = lm.ingest_totals()
    assert totals["chunks_received"] == 150 and totals["chunks_dropped"] == 5

    text = bm.render(registry).decode()
    assert "blinc_audio_live_pods 1.0" in text and "blinc_audio_connections 2.0" in text
    assert "blinc_audio_chunks_received_total 150.0" in text
    assert 'blinc_audio_window_backlog_seconds{pod="k2"} 0.0' in text
    assert 'blinc_audio_asr_degraded{pod="k2"} 1.0' in text
    assert 'blinc_asr_slots{state="busy"} 1.0' in text and 'blinc_asr_slots{state="free"} 1.0' in text
    assert "blinc_asr_windows_waiting 3.0" in text and "blinc_asr_pool_degraded 0.0" in text
    assert 'blinc_audio_callback_retries_total{outcome="gave_up"} 1.0' in text
    assert "blinc_audio_callback_retry_queue 4.0" in text


def test_pool_stats_survives_a_missing_connector(monkeypatch):
    import live_metrics as lm
    monkeypatch.setitem(sys.modules, "asr_connectors", None)
    monkeypatch.setitem(sys.modules, "asr_connectors.crisperwhisper_asr", None)
    assert lm.pool_stats() == {"size": 0, "busy": 0, "alive": 0, "degraded": 0, "waiting": 0, "state": "unknown"}


def test_retry_queue_reports_stats():
    import callback_retry
    outcomes = {"ok": False}
    q = callback_retry.RetryQueue(lambda url, payload, headers: outcomes["ok"],
                                  clock=lambda: 0.0, autostart=False, give_up_after=1.0)
    q.submit("k", "http://api", {})
    assert q.stats() == {"size": 1, "delivered": 0, "gave_up": 0, "dropped": 0}
    outcomes["ok"] = True
    assert q.run_due(now=5.0) == 1
    assert q.stats() == {"size": 0, "delivered": 1, "gave_up": 0, "dropped": 0}


# --- post-hoc services -----------------------------------------------------

@pytest.mark.parametrize("tree,service,registry_name", [
    ("audio_processing", "audio_posthoc", "running_audio_processes"),
    ("video_processing", "video_posthoc", "running_video_processes"),
])
def test_posthoc_servers_count_runs_claims_and_lease_refusals(tree, service, registry_name):
    src = _read("src", tree, "server_posthoc.py")
    assert "bm.start_exporter(bm.port_for('%s'))" % service in src
    main = src[src.index("if __name__ == '__main__':"):]
    assert main.index("reactor.listenTCP(") < main.index("install_metrics()") < main.index("reactor.run()")
    assert src.count("%s.try_claim(key)" % registry_name) == 1, "every claim goes through _claim()"
    assert "if not _claim(key):" in src
    complete = src[src.index("def on_run_complete"):]
    complete = complete[:complete.index("\n    def ") if "\n    def " in complete else len(complete)]
    assert "_M_RUNS.labels(_run_outcome(" in complete
    assert complete.index("_M_RUNS.labels(") < complete.index("try:"), "counted before the cleanup can raise"
    assert "_M_GPU_BUSY.inc()" in src
    for name in ("posthoc_runs_total", "posthoc_claims_total", "posthoc_gpu_busy_total",
                 "posthoc_runs_in_progress", "posthoc_lease_held", "posthoc_connections"):
        assert "'%s'" % name in src, name


def test_audio_posthoc_counts_fragment_joins():
    src = _read("src", "audio_processing", "server_posthoc.py")
    block = src[src.index("recording_fragments.join_fragments(to_join, chosen)"):]
    block = block[:block.index("chosen = to_join[0]")]
    assert "_M_FRAGMENT_JOINS.labels('ok').inc()" in block and "_M_FRAGMENT_JOINS.labels('failed').inc()" in block


def test_run_outcome_mapping():
    """Same rule in both servers: success -> ok; a pre-empted run -> preempted; else failed."""
    audio = _read("src", "audio_processing", "server_posthoc.py")
    video = _read("src", "video_processing", "server_posthoc.py")
    ns = {}
    exec(audio[audio.index("def _run_outcome(success, watch):"):audio.index("def install_metrics")], ns)
    assert ns["_run_outcome"](True, None) == "ok"
    assert ns["_run_outcome"](False, types.SimpleNamespace(preempted=True)) == "preempted"
    assert ns["_run_outcome"](False, None) == "failed"
    ns = {"PREEMPTED": "preempted"}
    exec(video[video.index("def _run_outcome(success, reason):"):video.index("def install_metrics")], ns)
    assert ns["_run_outcome"](False, "preempted") == "preempted"
    assert ns["_run_outcome"](False, "cancelled") == "failed"
    assert "from gpu_lease import PREEMPTED" in video


# --- GPU exporter ----------------------------------------------------------

class _FakeNvml:
    NVML_TEMPERATURE_GPU = 0

    class NVMLError(Exception):
        pass

    def nvmlInit(self):
        pass

    def nvmlDeviceGetCount(self):
        return 1

    def nvmlDeviceGetHandleByIndex(self, i):
        return "h%d" % i

    def nvmlDeviceGetName(self, h):
        return b"Quadro RTX 8000"

    def nvmlDeviceGetMemoryInfo(self, h):
        return types.SimpleNamespace(used=30 * 2 ** 30, total=48 * 2 ** 30)

    def nvmlDeviceGetUtilizationRates(self, h):
        return types.SimpleNamespace(gpu=25)

    def nvmlDeviceGetTemperature(self, h, kind):
        return 61

    def nvmlDeviceGetPowerUsage(self, h):
        return 123000

    def nvmlDeviceGetComputeRunningProcesses(self, h):
        return [types.SimpleNamespace(pid=os.getpid(), usedGpuMemory=5 * 2 ** 30),
                types.SimpleNamespace(pid=os.getpid(), usedGpuMemory=1)]

    def nvmlDeviceGetGraphicsRunningProcesses(self, h):
        raise self.NVMLError()


def test_gpu_exporter_reports_memory_utilization_and_per_process_vram(registry, monkeypatch):
    spec = importlib.util.spec_from_file_location("blinc_gpu_exporter", os.path.join(MON, "gpu_exporter.py"))
    ge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ge)
    monkeypatch.setattr(ge, "pynvml", _FakeNvml())
    monkeypatch.setattr(ge, "_names", {})
    ge.register(registry)
    text = bm.render(registry).decode()
    from prometheus_client.utils import floatToGoString
    assert 'blinc_gpu_memory_used_bytes{gpu="0",name="Quadro RTX 8000"} %s' % floatToGoString(30 * 2 ** 30) in text
    assert 'blinc_gpu_memory_total_bytes{gpu="0",name="Quadro RTX 8000"}' in text
    assert 'blinc_gpu_utilization_ratio{gpu="0",name="Quadro RTX 8000"} 0.25' in text
    assert 'blinc_gpu_temperature_celsius{gpu="0",name="Quadro RTX 8000"} 61.0' in text
    assert 'blinc_gpu_power_watts{gpu="0",name="Quadro RTX 8000"} 123.0' in text
    assert 'blinc_gpu_processes{gpu="0",name="Quadro RTX 8000"} 1.0' in text
    assert re.search(r'blinc_gpu_process_memory_bytes\{gpu="0",pid="%d",process="[^"]+"\} %s'
                     % (os.getpid(), re.escape(floatToGoString(5 * 2 ** 30))), text)
    assert "bm.start_exporter(bm.port_for('gpu'))" in _read("deploy", "monitoring", "gpu_exporter.py")
    unit = _read("deploy", "monitoring", "blinc-gpu-exporter.service")
    assert "BLINC_METRICS_PORT=9115" in unit and "deploy/monitoring/gpu_exporter.py" in unit


# --- monitoring stack configuration -----------------------------------------

def test_prometheus_config_scrapes_every_service_on_loopback():
    cfg = _yaml("deploy", "monitoring", "prometheus.yml")
    jobs = {sc["job_name"]: [t for s in sc["static_configs"] for t in s["targets"]] for sc in cfg["scrape_configs"]}
    assert jobs["api"] == ["127.0.0.1:5001"] and cfg["scrape_configs"][0]["metrics_path"] == "/metrics"
    for service, port in EXPECTED_PORTS.items():
        job = service.replace("_", "-")
        assert jobs[job] == ["127.0.0.1:%d" % port], job
    assert jobs["node"] == ["127.0.0.1:9100"]
    for targets in jobs.values():
        assert all(t.startswith("127.0.0.1:") for t in targets), targets
    assert cfg["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == ["127.0.0.1:9093"]
    assert cfg["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    assert cfg["global"]["scrape_interval"] == "15s"


REQUIRED_ALERTS = {
    "HostDiskLow", "GpuMemoryHigh", "LivePodWindowBacklog", "AsrPoolDegraded", "CallbackGiveUps",
    "PosthocRunFailed", "PosthocQueueBacklog", "ApiHigh5xxRate", "BlincTargetDown",
}
NODE_OR_BUILTIN = re.compile(r"^(node_|up$|ALERTS)")


def _rules():
    rules = []
    for path in glob.glob(os.path.join(MON, "rules", "*.yml")):
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        for group in doc["groups"]:
            rules.extend(group["rules"])
    return rules


def test_alert_rules_are_well_formed_and_cover_the_required_conditions():
    rules = _rules()
    names = {r["alert"] for r in rules}
    assert REQUIRED_ALERTS <= names, REQUIRED_ALERTS - names
    for r in rules:
        assert r["expr"].strip(), r["alert"]
        assert r["labels"]["severity"] in ("critical", "warning"), r["alert"]
        assert r["annotations"]["summary"] and r["annotations"]["description"], r["alert"]
        if "for" in r:
            assert re.fullmatch(r"\d+[smh]", r["for"]), r["alert"]
    by = {r["alert"]: r for r in rules}
    assert "< 0.15" in by["HostDiskLow"]["expr"]
    assert "> 0.90" in by["GpuMemoryHigh"]["expr"]
    assert "blinc_audio_window_backlog_seconds > 30" in by["LivePodWindowBacklog"]["expr"] and by["LivePodWindowBacklog"]["for"] == "2m"
    assert by["AsrPoolDegraded"]["for"] == "1m"
    assert "[10m]" in by["CallbackGiveUps"]["expr"] and 'outcome="gave_up"' in by["CallbackGiveUps"]["expr"]
    assert 'state="queued"} > 10' in by["PosthocQueueBacklog"]["expr"] and by["PosthocQueueBacklog"]["for"] == "1h"
    assert 'status=~"5.."' in by["ApiHigh5xxRate"]["expr"]
    assert by["BlincTargetDown"]["expr"].startswith("up")


def test_rules_and_dashboard_reference_only_exported_metrics():
    exported = exported_names()
    referenced = set()
    for r in _rules():
        referenced.update(re.findall(r"\b(blinc_[a-z0-9_]+)", r["expr"]))
    with open(os.path.join(MON, "grafana", "dashboards", "blinc.json"), encoding="utf-8") as f:
        dashboard = json.load(f)
    referenced.update(re.findall(r"\b(blinc_[a-z0-9_]+)", json.dumps(dashboard)))
    unknown = {n for n in referenced if n not in exported}
    assert not unknown, "referenced but not exported: %s" % sorted(unknown)
    # And the node_exporter/builtin names are the only non-blinc ones used.
    others = set()
    for r in _rules():
        others.update(n for n in re.findall(r"\b([a-z_][a-z0-9_]*)\s*[{\[<>=/ ]", r["expr"])
                      if not n.startswith("blinc_"))
    assert all(NODE_OR_BUILTIN.match(n) or n in {"rate", "increase", "sum", "max", "count", "and", "on", "by"}
               for n in others if "_" in n or n == "up"), sorted(others)


def test_dashboard_has_a_row_per_service_plus_host_gpu_and_one_datasource():
    with open(os.path.join(MON, "grafana", "dashboards", "blinc.json"), encoding="utf-8") as f:
        dashboard = json.load(f)
    rows = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    for word in ("API", "Coordinator", "Live audio", "Live video", "Post-hoc", "Host & GPU"):
        assert any(word in r for r in rows), (word, rows)
    ds = _yaml("deploy", "monitoring", "grafana", "provisioning", "datasources", "prometheus.yml")
    uid = ds["datasources"][0]["uid"]
    assert ds["datasources"][0]["url"] == "http://127.0.0.1:9090"
    panels = [p for p in dashboard["panels"] if p["type"] != "row"]
    assert len(panels) >= 30
    for p in panels:
        assert p["datasource"]["uid"] == uid, p["title"]
        assert p["targets"], p["title"]
        assert p["fieldConfig"]["defaults"]["unit"], p["title"]
    provider = _yaml("deploy", "monitoring", "grafana", "provisioning", "dashboards", "dashboards.yml")
    assert provider["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"
    assert dashboard["uid"] == "blinc-overview"


def test_alertmanager_routes_to_a_webhook_from_the_env_var():
    am = _yaml("deploy", "monitoring", "alertmanager.yml")
    assert am["route"]["receiver"] == "webhook"
    receivers = {r["name"]: r for r in am["receivers"]}
    assert receivers["webhook"]["webhook_configs"][0]["url_file"] == "/etc/alertmanager/webhook_url"
    compose = _yaml("deploy", "monitoring", "docker-compose.yml")
    assert "ALERT_WEBHOOK_URL" in compose["configs"]["webhook_url"]["content"]
    assert {"source": "webhook_url", "target": "/etc/alertmanager/webhook_url"} in compose["services"]["alertmanager"]["configs"]
    assert "ALERT_WEBHOOK_URL" in _read("deploy", "monitoring", ".env.example")


def test_compose_stack_binds_loopback_only_with_30d_retention():
    compose = _yaml("deploy", "monitoring", "docker-compose.yml")
    services = compose["services"]
    for name in ("prometheus", "alertmanager", "grafana", "node-exporter"):
        assert services[name]["network_mode"] == "host", name
        assert "ports" not in services[name], name
    assert "--web.listen-address=127.0.0.1:9090" in services["prometheus"]["command"]
    assert "--storage.tsdb.retention.time=30d" in services["prometheus"]["command"]
    assert "--web.listen-address=127.0.0.1:9093" in services["alertmanager"]["command"]
    assert "--cluster.listen-address=" in services["alertmanager"]["command"]
    assert "--web.listen-address=127.0.0.1:9100" in services["node-exporter"]["command"]
    env = services["grafana"]["environment"]
    assert env["GF_SERVER_HTTP_ADDR"] == "127.0.0.1" and str(env["GF_SERVER_HTTP_PORT"]) == "3000"
    assert env["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert "dcgm" in services["dcgm-exporter"]["profiles"], "opt-in only: unverified on this host"
    volumes = services["grafana"]["volumes"]
    assert any(v.startswith("./grafana/provisioning:") for v in volumes)
    assert any(v.startswith("./grafana/dashboards:") for v in volumes)


def test_readme_documents_ports_and_the_video_contract():
    readme = _read("deploy", "monitoring", "README.md")
    for port in list(EXPECTED_PORTS.values()) + [9090, 3000, 9093, 9100, 5001]:
        assert str(port) in readme, port
    video = _read("src", "video_processing", "server.py")
    for name in ("blinc_video_pods", "blinc_video_batches_dropped_total", "blinc_video_decode_mode"):
        assert name in readme, name
        assert "'%s'" % name in video, "video server exports %s" % name
    assert "'9112'" in video and "127.0.0.1" in video, "video exporter on loopback 9112 by default"
    assert "docker compose up -d" in readme and "BLINC_METRICS_PORT" in readme
    assert "Adding a receiver" in readme
