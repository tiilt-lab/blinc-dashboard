"""Per-session "record now, analyse later" for video (session.live_video_analytics).

Plumbing under test, end to end:

  POST /api/v1/sessions {liveVideoAnalytics: false}
    -> session_handler.create_session: session row + 'live_video_analytics'
       in the Redis session config the video service fetches at 'start'
    -> video server: ProcessingConfig.live_video_analytics folds into the
       existing per-pod record-only branch (VidRecorder only: no ffmpeg
       decoder, no VideoProcessor / GPU models)
    -> end_session: one post-hoc VIDEO leg per pod with a recording on disk
       (posthoc_queue.enqueue(..., legs=('video',)); no audio leg).

The API modules import Flask/DB at load, so their helpers are exec'd from
source and the rest are source contracts; the queue runs against FakeRedis.
"""
import ast
import json
import os
import sys
import textwrap
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "server"))
sys.path.insert(0, os.path.join(ROOT, "src", "common"))
sys.path.insert(0, os.path.join(ROOT, "src", "video_processing"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.modules.setdefault('posthoc_state', types.SimpleNamespace(is_running=lambda d: False))

from fake_redis import FakeRedis  # noqa: E402
import redis_sync  # noqa: E402
import posthoc_queue as q  # noqa: E402


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def _func_source(text, name, class_name=None):
    tree = ast.parse(text)
    nodes = tree.body
    if class_name:
        nodes = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name).body
    for node in nodes:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(text, node))
    raise AssertionError("%s not found" % name)


HANDLER = ("src", "server", "handlers", "session_handler.py")


# ---- queue: a job can carry a subset of legs ------------------------------------

@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(redis_sync, "_client", r)
    return r


def test_enqueue_persists_the_legs_and_defaults_to_both(fake):
    assert q.enqueue(1, [5, 6], legs=("video",)) == [5, 6]
    assert q.enqueue(1, [7]) == [7]
    jobs = {j["device_id"]: j for j in q._all_jobs()}
    assert jobs[5]["legs"] == ["video"] and jobs[6]["legs"] == ["video"]
    assert "legs" not in jobs[7]
    assert q.enqueue(1, [5], legs=("audio", "video")) == []   # already queued


def test_enqueue_rejects_unknown_or_empty_legs(fake):
    with pytest.raises(ValueError):
        q.enqueue(1, [8], legs=("style",))
    with pytest.raises(ValueError):
        q.enqueue(1, [8], legs=())
    assert q.status() == []


def _run(monkeypatch, job):
    ran = []
    monkeypatch.setattr(q, "_job_base", lambda j: {"sessionid": j["session_id"], "sessiondeviceid": j["device_id"]})
    monkeypatch.setattr(q, "_mint", lambda d: "ticket")
    monkeypatch.setattr(q, "_audio_produced_output", lambda d: True)
    monkeypatch.setattr(q, "_run_leg",
                        lambda url, init, start, device_id, scope, deadline:
                        ran.append((scope, init["type"], start)) or None)
    q._run_job(job)
    return ran


def test_video_only_job_runs_the_video_leg_and_never_touches_audio(monkeypatch, fake):
    job = {"session_id": 1, "device_id": 5, "state": "running", "legs": ["video"]}
    ran = _run(monkeypatch, job)
    assert ran == [("video", "Initialize_video_processing_analytics", "start_posthoc_video_processing")]
    assert job["done_legs"] == ["video"]


def test_job_without_legs_still_runs_audio_then_video(monkeypatch, fake):
    job = {"session_id": 1, "device_id": 5, "state": "running"}
    assert [r[0] for r in _run(monkeypatch, job)] == ["audio", "video"]


def test_status_shape_is_unchanged_by_legs(fake):
    q.enqueue(1, [5], legs=("video",))
    assert q.status(1) == [{"session_id": 1, "device_id": 5, "state": "queued",
                            "error": None, "started_at": None, "finished_at": None}]


# ---- end of session: which pods get a video leg -------------------------------

def _handler_helpers(enqueue):
    src = _read(*HANDLER)
    ns = {"os": os, "logging": __import__("logging"),
          "posthoc_queue": types.SimpleNamespace(enqueue=enqueue)}
    for name in ("video_recordings_dir", "pods_with_video_recordings", "queue_deferred_video_analysis"):
        exec(_func_source(src, name), ns)
    return ns


def _touch(d, name):
    with open(os.path.join(d, name), "wb") as f:
        f.write(b"x")


def test_pods_with_video_recordings_matches_session_and_pod_exactly(tmp_path):
    d = str(tmp_path)
    _touch(d, "5-aaaa_1_5_(Wed Sep 30 20:00:00 2026)_orig.webm")
    _touch(d, "6-bbbb_1_6_(Wed Sep 30 20:00:00 2026)_orig_1.mp4")   # mp4 pods append _<n>
    _touch(d, "7-cccc_2_7_(Wed Sep 30 20:00:00 2026)_orig.webm")    # another session
    _touch(d, "8-dddd_1_8_(Wed Sep 30 20:00:00 2026)_audio.wav")    # not video
    _touch(d, "15-eeee_1_15_(Wed Sep 30 20:00:00 2026)_orig.webm")  # pod 15, not 1 or 5
    _touch(d, "vid_img_frames_5-aaaa_1_5_(Wed Sep 30 20:00:00 2026)")
    pods = _handler_helpers(lambda *a, **k: [])["pods_with_video_recordings"]
    assert pods(1, [5, 6, 7, 8, 9, 1], d) == [5, 6]
    assert pods(2, [5, 6, 7], d) == [7]
    assert pods(1, [15], d) == [15]
    assert pods(1, [], d) == []
    assert pods(1, [5], os.path.join(d, "missing")) == []


def test_queue_deferred_video_analysis_enqueues_a_video_leg_per_recorded_pod(tmp_path):
    d = str(tmp_path)
    _touch(d, "5-aaaa_1_5_(Wed Sep 30 20:00:00 2026)_orig.webm")
    _touch(d, "6-bbbb_1_6_(Wed Sep 30 20:00:00 2026)_orig.webm")
    calls = []

    def enqueue(session_id, device_ids, models=None, legs=None):
        calls.append((session_id, list(device_ids), models, legs))
        return list(device_ids)

    ns = _handler_helpers(enqueue)
    session = types.SimpleNamespace(id=1)
    devices = [types.SimpleNamespace(id=i) for i in (5, 6, 9)]
    assert ns["queue_deferred_video_analysis"](session, devices, recordings_dir=d) == [5, 6]
    assert calls == [(1, [5, 6], None, ("video",))]


def test_queue_deferred_video_analysis_is_best_effort(tmp_path):
    d = str(tmp_path)
    _touch(d, "5-aaaa_1_5_(Wed Sep 30 20:00:00 2026)_orig.webm")

    def boom(*a, **k):
        raise RuntimeError("redis down")

    ns = _handler_helpers(boom)
    session = types.SimpleNamespace(id=1)
    assert ns["queue_deferred_video_analysis"](session, [types.SimpleNamespace(id=5)], recordings_dir=d) == []
    # nothing recorded -> nothing queued, no error
    ns = _handler_helpers(boom)
    assert ns["queue_deferred_video_analysis"](session, [types.SimpleNamespace(id=9)], recordings_dir=d) == []


def test_recordings_dir_is_the_live_video_services_folder():
    src = _func_source(_read(*HANDLER), "video_recordings_dir")
    assert "'video_processing', 'videorecordings'" in src


# ---- API: route -> handler -> Redis config -> end_session ---------------------

def test_route_reads_the_flag_defaulting_to_true_and_rejects_non_booleans():
    s = _read("src", "server", "routes", "session.py")
    create = _func_source(s, "create_session")
    assert "request.json.get('liveVideoAnalytics', True)" in create
    assert "if not isinstance(live_video_analytics, bool):" in create
    assert "live_video_analytics=live_video_analytics" in create
    assert create.index("liveVideoAnalytics") < create.index("session_handler.create_session(")


def test_handler_stores_the_flag_and_writes_it_to_the_redis_session_config():
    s = _read(*HANDLER)
    create = _func_source(s, "create_session")
    assert "live_video_analytics=True" in create.splitlines()[0]
    assert "live_video_analytics = live_video_analytics is not False" in create
    assert "session.live_video_analytics = live_video_analytics" in create
    assert "database.save_changes()" in create
    config = create[create.index("config = {"):create.index("RedisSessions.create_session(")]
    assert "'live_video_analytics': live_video_analytics" in config


def test_end_session_queues_video_legs_only_for_record_only_sessions():
    s = _read(*HANDLER)
    end = _func_source(s, "end_session")
    assert "if session.live_video_analytics is False:" in end
    assert "queue_deferred_video_analysis(session, session_devices)" in end
    # after the device rows are settled, before the pods are told to stop
    assert end.index("database.save_changes()") < end.index("queue_deferred_video_analysis(") \
        < end.index("# Ping pod devices")
    deferred = _func_source(s, "queue_deferred_video_analysis")
    assert "legs=('video',)" in deferred


def test_model_column_and_json_default_to_live():
    s = _read("src", "server", "tables", "session.py")
    assert "live_video_analytics = db.Column(db.Boolean, nullable=True)" in s
    assert "live_video_analytics=self.live_video_analytics is not False" in s
    assert "self.live_video_analytics = live_video_analytics is not False" in s


def test_migration_adds_the_nullable_column_after_the_cascades_head():
    versions = os.path.join(ROOT, "src", "server", "migrations", "versions")
    fn = [f for f in os.listdir(versions) if f.startswith("3b4c5d6e7f80_")]
    assert len(fn) == 1, fn
    s = _read("src", "server", "migrations", "versions", fn[0])
    assert "revision = '3b4c5d6e7f80'" in s
    assert "down_revision = '2a3b4c5d6e7f'" in s
    up = s[s.index("def upgrade():"):s.index("def downgrade():")]
    assert "if not _has_column('session', 'live_video_analytics'):" in up
    assert "op.add_column('session', sa.Column('live_video_analytics', sa.Boolean(), nullable=True))" in up
    assert "op.drop_column('session', 'live_video_analytics')" in s[s.index("def downgrade():"):]


# ---- video service: the flag reaches ProcessingConfig and skips analytics ------

def _processing_config():
    try:
        import requests  # noqa: F401
    except ImportError:  # CI: pytest+numpy only
        sys.modules["requests"] = types.ModuleType("requests")
    sys.modules.setdefault("config", types.SimpleNamespace(initialize=lambda: None))
    import processing_config
    return processing_config


def test_only_an_explicit_off_value_disables_live_video_analytics():
    pc = _processing_config()
    f = pc.live_video_analytics_from_config
    assert f({}) is True
    assert f({"live_video_analytics": None}) is True
    assert f({"live_video_analytics": True}) is True
    assert f({"live_video_analytics": False}) is False
    assert f({"live_video_analytics": 0}) is False
    assert f({"live_video_analytics": "false"}) is False
    assert f({"live_video_analytics": "1"}) is True
    assert f(None) is True


def test_from_json_reads_the_flag_from_the_sessions_redis_config(monkeypatch):
    pc = _processing_config()
    config = {"server_start": "2026-09-30 20:00:00", "live_video_analytics": False}
    monkeypatch.setattr(pc.callbacks, "get_redis_session_key", lambda k: "session:1", raising=False)
    monkeypatch.setattr(pc.callbacks, "get_redis_session_config", lambda k: json.dumps(config), raising=False)
    for name in ("video_cartoonize", "video_record_original", "video_record_reduced"):
        monkeypatch.setattr(pc.cf, name, lambda: False, raising=False)
    msg = {"key": "5-abc", "encoding": "pcm_f32le", "sample_rate": 16000, "channels": 1,
           "sessionid": 1, "deviceid": 5, "mimeextension": "webm"}
    ok, cfg = pc.ProcessingConfig.from_json(msg)
    assert ok, cfg
    assert cfg.live_video_analytics is False
    config["live_video_analytics"] = True
    ok, cfg = pc.ProcessingConfig.from_json(msg)
    assert ok and cfg.live_video_analytics is True
    del config["live_video_analytics"]   # a session created before the option
    ok, cfg = pc.ProcessingConfig.from_json(msg)
    assert ok and cfg.live_video_analytics is True


def test_video_server_folds_the_session_flag_into_the_record_only_branch():
    s = _read("src", "video_processing", "server.py")
    start = _func_source(s, "process_json", "ServerProtocol")
    assert "if not getattr(self.config, 'live_video_analytics', True):" in start
    flag = start.index("if not getattr(self.config, 'live_video_analytics', True):")
    assert "self.live_analytics = False" in start[flag:flag + 400]
    # the recorder is created unconditionally, the analytics objects only
    # behind the live_analytics guard (no ffmpeg decoder, no GPU processor)
    guard = start.index("and self.live_analytics:")
    assert start.index("VidRecorder(") < guard
    assert guard < start.index("VideoProcessor(") < start.index("StreamingChunkDecoder(")
    branch = start[guard:start.index("if cf.video_record_reduced():")]
    assert branch.count("VideoProcessor(") == 1 and branch.count("StreamingChunkDecoder(") == 1
    # the record-only path still arms the watchdogs and answers 'start'
    tail = start[start.index("if (self.config.videocartoonify or self.config.video) and not (self.live_analytics"):]
    assert "self.arm_silent_check()" in tail and "self.arm_lag_check()" in tail
    assert "'Video processing not activated to start video processor'" in tail


# ---- browser ------------------------------------------------------------------

def test_create_session_ui_sends_the_flag_and_defaults_it_on():
    comp = _read("frontend", "src", "create-session", "create-session-component.jsx")
    assert "const [liveVideoAnalytics, setLiveVideoAnalytics] = useState(" in comp
    assert "folder, asr, liveVideoAnalytics)" in comp
    page = _read("frontend", "src", "create-session", "html-pages.jsx")
    assert "Analyse video live (uses the GPU during class; off = record now, analyse after class)" in page
    assert 'checked={props.liveVideoAnalytics !== false}' in page
    svc = _read("frontend", "src", "services", "session-service.js")
    assert "liveVideoAnalytics: liveVideoAnalytics !== false" in svc
    model = _read("frontend", "src", "models", "session.js")
    assert "model.live_video_analytics = json['live_video_analytics'] !== false" in model


def test_pods_overview_tags_record_only_sessions():
    page = _read("frontend", "src", "pods-overview", "html-pages.jsx")
    assert "session.live_video_analytics === false" in page
    assert "video: recording only" in page
