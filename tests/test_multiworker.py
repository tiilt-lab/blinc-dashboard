"""Multi-worker API (infra audit A): the state that used to live in one
process is now in Redis or in the coordinator process.

Functional tests run against tests/fake_redis.py; the source contracts pin
the shape of the refactor (no route touches the Twisted manager, the api
role starts no singleton, websocket-only Socket.IO on both ends, 3 gunicorn
workers by default) so it cannot quietly regress.
"""
import ast
import importlib.util
import json
import os
import re
import sys
import threading
import time
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "src", "server")
UNITS = os.path.join(REPO, "deploy", "instances")
for path in (SERVER, os.path.dirname(os.path.abspath(__file__))):
    if path not in sys.path:
        sys.path.insert(0, path)

from fake_redis import FakeRedis  # noqa: E402
import redis_sync  # noqa: E402
import device_commands as dc  # noqa: E402


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(redis_sync, "_client", r)
    return r


# --- device commands over Redis ----------------------------------------------

class _Manager:
    """Stands in for the Twisted ConnectionManager in the coordinator."""

    def __init__(self, connected=True, reply=None, timeout=False):
        self.connected = connected
        self.reply = reply
        self.timeout = timeout
        self.sent = []

    def is_connected(self, device_id):
        return self.connected

    def send_command(self, device_id, cmd):
        self.sent.append((device_id, cmd))
        return True

    def send_command_and_wait(self, device_id, cmd):
        self.sent.append((device_id, cmd))
        return (False, None) if self.timeout else (True, self.reply)


def _wire(fake_r, manager):
    # publish() invokes the coordinator's handler synchronously, as Redis
    # would deliver it to the subscribed coordinator.
    sub = dc.CommandSubscriber(manager, client=fake_r)
    fake_r.subscribers.append(lambda channel, message: sub.handle(message))
    return sub


def test_send_command_publishes_the_agreed_shape_and_is_forwarded():
    r, manager = FakeRedis(), _Manager()
    _wire(r, manager)
    proxy = dc.RedisCommandProxy(client=r)
    assert proxy.send_command(7, {"cmd": "blink", "op": "start"}) is True
    channel, message = r.published[0]
    assert channel == "device_cmd"
    assert json.loads(message) == {"device_id": 7, "cmd": {"cmd": "blink", "op": "start"}}
    assert manager.sent == [(7, {"cmd": "blink", "op": "start"})]


def test_send_command_without_a_coordinator_returns_false():
    proxy = dc.RedisCommandProxy(client=FakeRedis())
    assert proxy.send_command(7, {"cmd": "end"}) is False


def test_waited_command_round_trips_through_a_reply_key():
    r, manager = FakeRedis(), _Manager(reply={"logs": "hello"})
    _wire(r, manager)
    proxy = dc.RedisCommandProxy(client=r, timeout=2)
    assert proxy.send_command_and_wait(7, {"cmd": "logs"}) == (True, {"logs": "hello"})
    msg = json.loads(r.published[0][1])
    assert msg["cmd"] == {"cmd": "logs"} and msg["device_id"] == 7
    assert msg["reply_key"].startswith("device_reply:")
    assert manager.sent == [(7, {"cmd": "logs"})]
    assert r.exists(msg["reply_key"]) == 0          # consumed


def test_not_connected_device_gets_the_not_connected_reply_at_once():
    r, manager = FakeRedis(), _Manager(connected=False)
    _wire(r, manager)
    proxy = dc.RedisCommandProxy(client=r, timeout=5)
    t0 = time.time()
    assert proxy.send_command_and_wait(999999, {"cmd": "logs"}) == \
        (False, {"success": False, "error": "not connected"})
    assert time.time() - t0 < 2                      # not the 5 s wait
    assert manager.sent == []                        # never forwarded


def test_pod_that_never_answers_is_reported_as_timeout():
    r, manager = FakeRedis(), _Manager(timeout=True)
    _wire(r, manager)
    proxy = dc.RedisCommandProxy(client=r, timeout=2)
    assert proxy.send_command_and_wait(7, {"cmd": "logs"}) == \
        (False, {"success": False, "error": "timeout"})


def test_waited_command_without_a_coordinator_fails_fast():
    proxy = dc.RedisCommandProxy(client=FakeRedis(), timeout=5)
    t0 = time.time()
    assert proxy.send_command_and_wait(7, {"cmd": "logs"}) == \
        (False, {"success": False, "error": "no coordinator"})
    assert time.time() - t0 < 1


def test_reply_keys_expire_after_60s():
    r = FakeRedis()
    sub = dc.CommandSubscriber(_Manager(), client=r)
    sub.push_reply("device_reply:abc", {"success": True, "data": {"x": 1}})
    assert r.ttl("device_reply:abc") == 60 == dc.REPLY_TTL
    assert json.loads(r.lrange("device_reply:abc", 0, -1)[0]) == {"success": True, "data": {"x": 1}}


def test_malformed_messages_are_ignored():
    manager = _Manager()
    sub = dc.CommandSubscriber(manager, client=FakeRedis())
    sub.handle("not json")
    sub.handle(json.dumps({"cmd": {"cmd": "end"}}))   # no device_id
    assert manager.sent == []


def test_dispatcher_picks_the_backend_by_role(monkeypatch):
    d = dc._Dispatcher()
    monkeypatch.setenv("DC_ROLE", "api")
    assert isinstance(d._backend(), dc.RedisCommandProxy)
    stub = types.SimpleNamespace(ConnectionManager=types.SimpleNamespace(instance="twisted"))
    monkeypatch.setitem(sys.modules, "device_websockets", stub)
    for role in ("coordinator", "all"):
        monkeypatch.setenv("DC_ROLE", role)
        assert d._backend() == "twisted"
    stub.ConnectionManager.instance = None
    with pytest.raises(RuntimeError):
        d._backend()
    # the surface routes use
    assert callable(dc.ConnectionManager.instance.send_command)
    assert callable(dc.ConnectionManager.instance.send_command_and_wait)


# --- post-hoc queue in Redis --------------------------------------------------

@pytest.fixture
def q(fake, monkeypatch):
    sys.modules.setdefault("posthoc_state", types.SimpleNamespace(is_running=lambda d: False))
    import posthoc_queue
    monkeypatch.setattr(posthoc_queue, "_worker", None)
    return posthoc_queue


def test_enqueue_status_clear_use_the_redis_list_and_hashes(q, fake):
    assert q.enqueue(1, [10, 11]) == [10, 11]
    assert q.enqueue(1, [11, 12]) == [12]                  # already queued: skipped
    assert fake.lrange("posthoc_queue:jobs", 0, -1) == ["1", "2", "3"]
    job = fake.hgetall("posthoc_queue:job:1")
    assert json.loads(job["state"]) == "queued" and json.loads(job["device_id"]) == 10
    assert json.loads(job["session_id"]) == 1 and json.loads(job["models"]) is None
    assert fake.lrange("posthoc_queue:wake", 0, -1) == ["1", "1"]   # the runner is woken
    assert [(j["device_id"], j["state"]) for j in q.status(1)] == \
        [(10, "queued"), (11, "queued"), (12, "queued")]
    assert q.status(2) == []
    assert q.clear_pending() == 3
    assert q.status() == []
    assert fake.lrange("posthoc_queue:jobs", 0, -1) == []
    assert fake.exists("posthoc_queue:job:1") == 0


def test_running_job_survives_clear_and_is_requeued_after_a_restart(q):
    q.enqueue(1, [10])
    job = q._all_jobs()[0]
    job["state"] = "running"
    q._save(job)
    assert q.clear_pending() == 0
    assert q._requeue_running() == 1
    assert q.status()[0]["state"] == "queued"


def test_migration_moves_pending_jobs_from_the_json_file_once(q, tmp_path, monkeypatch):
    state = tmp_path / "posthoc_queue.json"
    state.write_text(json.dumps([
        {"session_id": 1, "device_id": 10, "state": "done", "models": None, "error": None},
        {"session_id": 1, "device_id": 11, "state": "running", "models": {"asr": "whisperx"},
         "error": None, "done_legs": ["audio"]},
        {"session_id": 2, "device_id": 12, "state": "queued", "models": None, "error": None},
    ]))
    monkeypatch.setattr(q, "_state_file", lambda: str(state))
    assert q.migrate_state_file() == 2
    assert {j["device_id"]: j["state"] for j in q.status()} == {11: "queued", 12: "queued"}
    jobs = q._all_jobs()
    assert jobs[0]["done_legs"] == ["audio"] and jobs[0]["models"] == {"asr": "whisperx"}
    assert not state.exists() and (tmp_path / "posthoc_queue.json.migrated").exists()
    assert q.migrate_state_file() == 0                       # never replayed


def test_mutations_take_and_release_the_cross_process_lock(q, fake, monkeypatch):
    seen = []
    real_set = fake.set

    def spy(key, value, **kw):
        if key == "posthoc_queue:lock":
            seen.append((kw.get("nx"), kw.get("ex")))
        return real_set(key, value, **kw)

    monkeypatch.setattr(fake, "set", spy)
    q.enqueue(1, [10])
    assert seen == [(True, 10)]
    assert fake.exists("posthoc_queue:lock") == 0


def test_settled_jobs_are_pruned_after_the_linger(q):
    q.enqueue(1, [10])
    job = q._all_jobs()[0]
    job.update(state="done", finished_at=time.time() - q._DONE_LINGER - 1)
    q._save(job)
    assert q.status() == []


def test_queue_module_is_inert_at_import():
    tree = ast.parse(_read("src", "server", "posthoc_queue.py"))
    calls = [ast.unparse(n.value.func) for n in tree.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert calls == [], "posthoc_queue must not restore or start a runner at import: %s" % calls
    assert "def start_runner" in _read("src", "server", "posthoc_queue.py")


# --- watchers, locks, LLM slots, image queue ----------------------------------

def test_watchers_stamp_in_redis_with_a_ttl(fake):
    import watchers
    watchers.mark_watched(5)
    assert fake.ttl("watched:5") == watchers.WATCH_TTL >= 5 * 60
    assert watchers.watched_within(5, 60) is True
    assert watchers.watched_within(6, 60) is False
    fake.set("watched:7", time.time() - 120)                # stamped by another worker
    assert watchers.watched_within(7, 60) is False
    assert watchers.watched_within(7, 300) is True


def test_watchers_fall_back_to_the_local_dict_when_redis_is_down(fake):
    import watchers
    fake.fail = True
    watchers.mark_watched(8)
    assert watchers.watched_within(8, 60) is True
    assert "watched:8" not in fake.store


def test_lock_is_set_nx_ex_and_released_only_by_its_owner(fake):
    with redis_sync.lock("remux_lock:1", 3900, 1) as held:
        assert held is True
        assert fake.ttl("remux_lock:1") == 3900
        with redis_sync.lock("remux_lock:1", 3900, 0.3) as again:
            assert again is False                           # bounded wait, no deadlock
        assert fake.exists("remux_lock:1") == 1             # the inner miss released nothing
    assert fake.exists("remux_lock:1") == 0
    fake.set("remux_lock:1", "someone-else", ex=10)
    with redis_sync.lock("remux_lock:1", 3900, 0.2) as held:
        assert held is False
    assert fake.get("remux_lock:1") == "someone-else"       # a late release never drops a successor


def test_lock_wait_is_bounded(fake):
    fake.set("synthesis_lock:1", "x", ex=60)
    t0 = time.time()
    with redis_sync.lock("synthesis_lock:1", 60, 0.5) as held:
        assert held is False
    assert 0.4 < time.time() - t0 < 3


def test_lock_falls_back_to_a_process_local_lock(fake):
    fake.fail = True
    with redis_sync.lock("remux_lock:9", 10, 1) as held:
        assert held is True
        assert redis_sync._local_lock("remux_lock:9").locked()
    assert not redis_sync._local_lock("remux_lock:9").locked()


def test_llm_slots_cap_across_workers_with_a_ttl_safety(fake):
    slots = redis_sync.Slots("llm_slots", 3, 660)
    assert [slots.acquire() for _ in range(3)] == ["redis"] * 3
    assert fake.ttl("llm_slots") == 660
    assert slots.acquire() is None and fake.get("llm_slots") == "3"
    slots.release("redis")
    assert fake.get("llm_slots") == "2"
    slots.release("redis")
    slots.release("redis")
    assert fake.exists("llm_slots") == 0
    slots.acquire()                                          # leaked by a killed worker...
    fake.now += 661
    assert fake.exists("llm_slots") == 0                     # ...frees itself
    assert slots.acquire() == "redis" and fake.get("llm_slots") == "1"


def test_llm_slots_fall_back_to_the_local_semaphore(fake):
    slots = redis_sync.Slots("llm_slots", 1, 660)
    fake.fail = True
    assert slots.acquire() == "local"
    assert slots.acquire() is None
    slots.release("local")
    assert slots.acquire() == "local"
    slots.release("local")


def test_image_queue_keeps_the_120_newest_frames_and_expires(fake):
    for i in range(130):
        redis_sync.capped_push("image_queue:k", "f%d" % i, 120, 600)
    assert fake.llen("image_queue:k") == 120
    assert fake.ttl("image_queue:k") == 600
    assert fake.brpop("image_queue:k", 1)[1] == "f10"        # oldest ten dropped, FIFO from the tail
    assert fake.lrange("image_queue:k", 0, 0) == ["f129"]    # newest at the head


# --- source contracts ---------------------------------------------------------

def _funcs(src):
    tree = ast.parse(src)
    return tree, {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_no_route_or_handler_imports_the_twisted_manager():
    files = [os.path.join("routes", f) for f in os.listdir(os.path.join(SERVER, "routes")) if f.endswith(".py")]
    files += [os.path.join("handlers", f) for f in os.listdir(os.path.join(SERVER, "handlers")) if f.endswith(".py")]
    files.append("scheduled_tasks.py")
    for name in files:
        assert "device_websockets" not in _read("src", "server", name), name
    for name in ("routes/device.py", "routes/admin.py", "routes/callback.py", "handlers/session_handler.py"):
        assert "from device_commands import ConnectionManager" in _read("src", "server", name), name


def test_api_role_starts_no_scheduler_device_server_or_queue_runner():
    src = _read("src", "server", "discussion_capture.py")
    tree, funcs = _funcs(src)
    assert "COORDINATOR_ROLES = ('coordinator', 'all')" in src
    create = ast.unparse(funcs["create_app"])
    assert "role = _resolve_role(role)" in create
    guarded = [n for n in ast.walk(funcs["create_app"]) if isinstance(n, ast.If)
               and ast.unparse(n.test) == "role in COORDINATOR_ROLES"]
    assert guarded and "_start_background_services()" in ast.unparse(guarded[0])
    assert create.count("_start_background_services()") == 1
    # every singleton lives in that one function
    singles = ast.unparse(funcs["_start_background_services"])
    for call in ("scheduler.add_job", "device_websockets.run_server()",
                 "device_commands.CommandSubscriber", "posthoc_queue.migrate_state_file()",
                 "posthoc_queue.start_runner()", "prewarm_video_cache()"):
        assert call in singles, call
        others = [ast.unparse(f) for n, f in funcs.items() if n != "_start_background_services"]
        assert not any(call in o for o in others), "%s outside _start_background_services" % call
    assert "DC_DISABLE_POSTHOC_QUEUE" in singles
    # the workers default to the api role; the dev runner keeps everything in-process
    assert "os.environ.get('DC_ROLE') or 'api'" in ast.unparse(funcs["_resolve_role"])
    assert "os.environ.setdefault('DC_ROLE', 'all')" in ast.unparse(funcs["main"])
    assert "app = create_app()" in _read("src", "server", "wsgi.py")
    assert "DC_ROLE" not in _read("src", "server", "gunicorn.conf.py")


def test_coordinator_entry_runs_the_coordinator_role_without_http():
    src = _read("src", "server", "coordinator.py")
    assert "os.environ['DC_ROLE'] = 'coordinator'" in src
    assert "create_app(role='coordinator')" in src
    assert "socketio.run(" not in src and "import gunicorn" not in src
    assert "signal.SIGTERM" in src and "scheduler.shutdown" in src


def test_coordinator_unit_mirrors_the_api_unit():
    units = [d for d in os.listdir(UNITS)
             if os.path.exists(os.path.join(UNITS, d, "blinc-discussion-capture.service"))]
    assert units
    for d in units:
        api = _read("deploy", "instances", d, "blinc-discussion-capture.service")
        coord = _read("deploy", "instances", d, "blinc-coordinator.service")
        for key in ("User", "Group", "EnvironmentFile", "WorkingDirectory", "Restart"):
            a = re.search(r"^%s=(.*)$" % key, api, re.M).group(1)
            c = re.search(r"^%s=(.*)$" % key, coord, re.M).group(1)
            assert a == c, (d, key, a, c)
        assert re.search(r"^Restart=always$", coord, re.M)
        assert re.search(r'^Environment="?DC_DEVICE_WS_PORT=9011"?$', coord, re.M)
        assert re.search(r'^Environment="?DC_ROLE=coordinator"?$', coord, re.M)
        execstart = re.search(r"^ExecStart=(.*)$", coord, re.M).group(1)
        assert execstart.endswith("/src/venv-unified/bin/python src/server/coordinator.py"), execstart
        assert "DC_ROLE=coordinator" not in api
        assert "DC_DEVICE_WS_PORT" not in api or "coordinator" in api  # the API no longer binds 9011


def test_socketio_is_websocket_only_on_both_ends():
    app_src = _read("src", "server", "app.py")
    ctor = re.search(r"socketio = SocketIO\(app,(.*)\)\n", app_src).group(1)
    assert "transports=['websocket']" in ctor
    assert "message_queue=cf.redis_url()" in ctor           # cross-worker room emits
    js = _read("frontend", "src", "services", "socket-service.js")
    opts = re.findall(r"transports:\s*\[([^\]]*)\]", js)
    assert opts == ["'websocket'"], opts
    # Flask-SocketIO forwards the option to engineio
    socketio = pytest.importorskip("socketio")
    assert socketio.Server(async_mode="threading", transports=["websocket"]).eio.transports == ["websocket"]


def _load_conf(monkeypatch, workers=None):
    if workers is None:
        monkeypatch.delenv("DC_API_WORKERS", raising=False)
    else:
        monkeypatch.setenv("DC_API_WORKERS", str(workers))
    spec = importlib.util.spec_from_file_location(
        "blinc_gunicorn_conf_mw", os.path.join(SERVER, "gunicorn.conf.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gunicorn_defaults_to_three_workers_of_64_threads(monkeypatch):
    conf = _load_conf(monkeypatch)
    assert conf.workers == 3 and conf.threads == 64
    assert conf.workers * conf.threads >= 150             # about the old 1 x 200
    assert _load_conf(monkeypatch, workers=2).workers == 2
    assert conf.worker_class == "gthread"


def test_per_worker_state_moved_to_redis_keys():
    session = _read("src", "server", "routes", "session.py")
    for gone in ("image_queue_dict", "_remux_locks", "_synthesis_write_lock", "import queue\n"):
        assert gone not in session, gone
    assert "redis_sync.lock('remux_lock:%d'" in session
    assert "redis_sync.lock('synthesis_lock:%d'" in session
    assert "_IMAGE_QUEUE_CAP = 120" in session and "_IMAGE_QUEUE_PREFIX = 'image_queue:'" in session
    assert "redis_sync.capped_push(_IMAGE_QUEUE_PREFIX + queue_key" in session
    assert "r.brpop(key, timeout=1)" in session
    assert "_REMUX_LOCK_TTL = 3900" in session               # > the 3600 s transcode step
    llm = _read("src", "server", "routes", "llm_query.py")
    assert 'redis_sync.Slots("llm_slots", int(os.getenv("LLM_MAX_CONCURRENT", "3")), 660)' in llm
    assert "BoundedSemaphore" not in llm
    cb = _read("src", "server", "routes", "callback.py")
    assert "_LAST_CONNECT_PREFIX = 'device_last_connect:'" in cb
    assert "_LAST_CONNECT_TTL = 24 * 3600" in cb
    assert "_last_connect_time.get(session_device.id)" not in cb
    assert "@api_routes.route('/api/v1/callback/posthoc_failed'" in cb  # today's route stays
    watchers = _read("src", "server", "watchers.py")
    assert "_PREFIX = 'watched:'" in watchers and "ex=WATCH_TTL" in watchers
    queue = _read("src", "server", "posthoc_queue.py")
    for key in ("posthoc_queue:jobs", "posthoc_queue:job:%s", "posthoc_queue:lock", "posthoc_queue:wake"):
        assert key in queue, key
