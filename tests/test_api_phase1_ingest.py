"""Phase 1 API ingest/read helpers on an in-memory SQLite (no MySQL, no Redis).

database.py and routes/callback.py both do `from app import ...`; a stub `app`
module (Flask + Flask-SQLAlchemy on sqlite://) stands in, so the real helpers
and the real callback route bodies run against a throwaway schema. The heavy
neighbours of callback.py (wrappers, config, posthoc_state, device_commands,
handlers.callback_handlers) are replaced by inert stubs. Skipped where Flask is
not installed (CI), like the other server-side tests.

Covers: ?after_id= filtering, the idempotent transcript insert (natural key),
the batched + idempotent video-metric insert, and the connect/disconnect
callbacks (idempotent; a replayed disconnect older than the latest connect is
ignored).
"""
import importlib.util
import itertools
import json
import os
import sys
import types

import pytest

flask = pytest.importorskip("flask")
flask_sqlalchemy = pytest.importorskip("flask_sqlalchemy")

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
SERVER = os.path.join(SRC, "server")
_seq = itertools.count(1)


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _stub_module(name, **attrs):
    module = types.ModuleType(name)
    module._phase1_stub = True
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _ours(module):
    # Stubs and first-party modules bound to the stub app; never third-party
    # C extensions, which must not be re-imported in one process.
    path = getattr(module, "__file__", None) or ""
    return getattr(module, "_phase1_stub", False) or os.path.abspath(path).startswith(SRC)


@pytest.fixture(scope="module")
def env():
    if SERVER not in sys.path:
        sys.path.insert(0, SERVER)
    loaded = sys.modules.get("app")
    if loaded is not None and not getattr(loaded, "_phase1_stub", False):
        pytest.skip("the real app module is loaded in this process")
    before = dict(sys.modules)   # restored on teardown so the stubs never leak

    flask_app = flask.Flask("phase1")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app_stub = _stub_module(
        "app", _phase1_stub=True, app=flask_app,
        db=flask_sqlalchemy.SQLAlchemy(flask_app),
        socketio=types.SimpleNamespace(emit=_Recorder()))
    _stub_module("wrappers", verify_local=lambda f: f,
                 verify_login=lambda public=False: (lambda f: f))
    _stub_module("config")
    _stub_module("posthoc_state")
    send_command = _Recorder()
    _stub_module("device_commands", ConnectionManager=types.SimpleNamespace(
        instance=types.SimpleNamespace(send_command=send_command)))
    handlers = _stub_module("handlers")
    handlers.callback_handlers = _stub_module("handlers.callback_handlers")

    import database  # noqa: E402  (resolves the stub app)
    spec = importlib.util.spec_from_file_location(
        "phase1_callback", os.path.join(SERVER, "routes", "callback.py"))
    callback = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(callback)

    ctx = flask_app.app_context()
    ctx.push()
    # Only the tables SQLite can compile (session_synthesized_report uses
    # MySQL LONGTEXT); everything these tests touch is dialect-neutral.
    tables = [t for t in app_stub.db.metadata.sorted_tables
              if not any(type(c.type).__module__.startswith("sqlalchemy.dialects.mysql") for c in t.columns)]
    app_stub.db.metadata.create_all(app_stub.db.engine, tables=tables)
    yield types.SimpleNamespace(database=database, callback=callback, flask_app=flask_app,
                                db=app_stub.db, emits=app_stub.socketio.emit.calls,
                                send_command=send_command)
    app_stub.db.session.remove()
    ctx.pop()
    for name, module in list(sys.modules.items()):
        if name not in before and _ours(module):
            del sys.modules[name]
    for name, module in before.items():
        if sys.modules.get(name) is not module and _ours(sys.modules.get(name, module)):
            sys.modules[name] = module


@pytest.fixture
def pod(env):
    from tables.session import Session
    from tables.session_device import SessionDevice
    from tables.user import User
    n = next(_seq)
    user = User("phase1-%d@example.org" % n, "user", password="Abcdef1!")
    env.db.session.add(user)
    env.db.session.flush()
    session = Session(user.id, "S%d" % n)
    env.db.session.add(session)
    env.db.session.flush()
    device = SessionDevice(session.id, None, "Pod %d" % n)
    env.db.session.add(device)
    env.db.session.flush()
    device.create_key()
    env.db.session.commit()
    return device


def _add(database, pod_id, start, length=1, text=None):
    return database.add_transcript(pod_id, start, length, text or "t%s" % start,
                                   False, 0, 0, 0, 0, 0, 0, -1, "A", -1)


def _post(env, view, payload):
    with env.flask_app.test_request_context("/api/v1/callback/x", method="POST", json=payload):
        return view()


# ---------------------------------------------------------------- after_id

def test_get_transcripts_after_id_filters_on_id_and_keeps_start_order(env, pod):
    d = env.database
    t30 = _add(d, pod.id, 30)   # lowest id, latest start
    t10 = _add(d, pod.id, 10)
    t20 = _add(d, pod.id, 20)   # highest id
    assert [t.id for t in d.get_transcripts(session_device_id=pod.id)] == [t10.id, t20.id, t30.id]
    assert [t.id for t in d.get_transcripts(session_device_id=pod.id, after_id=t30.id)] == [t10.id, t20.id]
    assert [t.id for t in d.get_transcripts(session_id=pod.session_id, after_id=t10.id)] == [t20.id]
    assert d.get_transcripts(session_id=pod.session_id, after_id=t20.id) == []
    assert "id" in t10.json()


def test_get_speaker_video_metrics_after_id(env, pod):
    d = env.database
    added = d.add_speaker_video_metrics_batch(
        pod.id, [("alice", 9, "happy", 1, "book"), ("alice", 3, "sad", 0, None)])
    ids_by_time = [m["id"] for m in sorted(added, key=lambda m: m["time_stamp"])]
    assert [m.id for m in d.get_speaker_video_metrics(session_device_id=pod.id)] == ids_by_time
    newest = max(m["id"] for m in added)
    assert [m.id for m in d.get_speaker_video_metrics(session_device_id=pod.id, after_id=newest - 1)] == [newest]
    assert d.get_speaker_video_metrics(session_id=pod.session_id, after_id=newest) == []
    assert all("id" in m for m in added)


# ------------------------------------------------------- idempotent ingest

def test_as_int_rounds_half_away_from_zero_like_mysql(env):
    d = env.database
    assert [d._as_int(v) for v in (10, 10.2, 10.5, 11.5, 0.49, None)] == [10, 10, 11, 12, 0, None]


def test_transcript_insert_is_idempotent_on_natural_key(env, pod, monkeypatch):
    d = env.database
    from tables.keyword_usage import KeywordUsage
    from tables.transcript import Transcript
    commits = _Recorder()
    real_commit = d.db.session.commit
    monkeypatch.setattr(d.db.session, "commit", lambda: (commits(), real_commit())[1])

    keywords = [{"word": "moles", "keyword": "mole", "similarity": 0.9}]
    args = (pod.id, 12.4, 2.7, "the mole ratio", False, 0, 1, 2, 3, 4, 5, -1, "A", 7)
    t1, created1 = d.get_or_add_transcript(*args, keywords=keywords)
    assert created1 is True and len(commits.calls) == 1   # transcript + keywords, one commit
    t2, created2 = d.get_or_add_transcript(*args, keywords=keywords)
    assert created2 is False and t2.id == t1.id and len(commits.calls) == 1
    assert (t1.start_time, t1.length) == (12, 3)          # stored as MySQL would round them
    assert d.db.session.query(Transcript).filter_by(session_device_id=pod.id).count() == 1
    assert d.db.session.query(KeywordUsage).filter_by(transcript_id=t1.id).count() == 1
    assert t1.json()["keywords"][0]["keyword"] == "mole"
    # Same rounded second, different words: a distinct utterance, not a retry.
    t3, created3 = d.get_or_add_transcript(pod.id, 12.4, 2.7, "ok", False, 0, 0, 0, 0, 0, 0, -1, "B", 8)
    assert created3 is True and t3.id != t1.id


def test_video_metrics_batch_is_one_commit_and_idempotent(env, pod, monkeypatch):
    d = env.database
    commits = _Recorder()
    real_commit = d.db.session.commit
    monkeypatch.setattr(d.db.session, "commit", lambda: (commits(), real_commit())[1])

    rows = [("alice", 5, "happy", 1, "book"), ("bob", 5, "neutral", 0, None), ("alice", 6, "sad", 1, "laptop")]
    added = d.add_speaker_video_metrics_batch(pod.id, rows)
    assert [(m["student_username"], m["time_stamp"]) for m in added] == [("alice", 5), ("bob", 5), ("alice", 6)]
    assert all(isinstance(m["id"], int) for m in added)
    assert len(commits.calls) == 1
    # Retry of the same POST: nothing inserted, nothing to push, no commit.
    assert d.add_speaker_video_metrics_batch(pod.id, rows) == []
    assert len(commits.calls) == 1
    # Partial retry (and an in-batch duplicate): only the missing row lands.
    added2 = d.add_speaker_video_metrics_batch(pod.id, rows + [("bob", 6, "happy", 1, None), ("bob", 6, "happy", 1, None)])
    assert [(m["student_username"], m["time_stamp"]) for m in added2] == [("bob", 6)]
    assert len(d.get_speaker_video_metrics(session_device_id=pod.id)) == 4
    assert d.add_speaker_video_metrics_batch(pod.id, []) == []


# -------------------------------------------------------- callback routes

def test_transcript_callback_returns_existing_row_on_retry(env, pod):
    from tables.transcript import Transcript
    payload = {"source": pod.processing_key, "start_time": 3.2, "end_time": 5.9,
               "transcript": "hello there", "direction": 90, "questions": [],
               "keywords": [{"word": "hello", "keyword": "greeting", "similarity": 0.8}],
               "features": None, "topic_id": -1, "speaker_tag": "A", "speaker_id": 1}
    r1 = _post(env, env.callback.add_transcript, payload)
    pushed = len(env.emits)
    r2 = _post(env, env.callback.add_transcript, payload)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.get_json()["transcript_id"] == r2.get_json()["transcript_id"]
    assert len(env.emits) == pushed                       # the duplicate is not pushed again
    assert env.db.session.query(Transcript).filter_by(session_device_id=pod.id).count() == 1


def test_speaker_metrics_callback_retry_does_not_duplicate_metrics(env, pod):
    d = env.database
    payload = {"source": pod.processing_key, "start_time": 20, "end_time": 24,
               "transcript": "group answer", "direction": 0, "questions": [], "keywords": [],
               "features": {}, "topic_id": -1, "speaker_tag": "A", "speaker_id": 1,
               "speakers": [1, 2], "participation_scores": [0.5, 0.3, 0.2],
               "internal_cohesion": [1, 1, 1], "responsivity": [1, 1, 1],
               "social_impact": [1, 1, 1], "newness": [1, 1, 1], "communication_density": [1, 1, 1]}
    r1 = _post(env, env.callback.add_speaker_transcript_metrics, payload)
    r2 = _post(env, env.callback.add_speaker_transcript_metrics, payload)
    tid = r1.get_json()["transcript_id"]
    assert r2.get_json()["transcript_id"] == tid
    assert len(d.get_speaker_transcript_metrics(transcript_id=tid)) == 3


def test_video_metrics_callback_batches_and_dedupes(env, pod):
    payload = {"source": pod.processing_key,
               "video_metrics": {"alice": [[1, "happy", 1, "book"], [2, "sad", 0, "laptop"]],
                                 "bob": [[1, "neutral", 1, None]]}}
    assert _post(env, env.callback.add_speaker_video_metrics, payload).status_code == 200
    pushed = len(env.emits)
    event, body = env.emits[-1][0]
    assert event == "video_metrics_update"
    assert len(json.loads(body)["speaker_video_metrics"]) == 3
    assert _post(env, env.callback.add_speaker_video_metrics, payload).status_code == 200
    assert len(env.emits) == pushed                       # retry: nothing new to push
    assert len(env.database.get_speaker_video_metrics(session_device_id=pod.id)) == 3


def test_connect_disconnect_are_idempotent_and_stale_disconnect_is_ignored(env, pod):
    cb = env.callback
    key = pod.processing_key
    connect = {"source": key, "time": "2026-09-30 10:00:00.000000"}
    assert _post(env, cb.device_connected, connect).status_code == 200
    assert pod.connected is True
    _post(env, cb.device_connected, connect)              # replayed connect: no change
    assert pod.connected is True

    # A disconnect from BEFORE the connect, replayed after the reconnect.
    pushed = len(env.emits)
    r = _post(env, cb.device_disconnected, {"source": key, "time": "2026-09-30 09:59:59.000000"})
    assert r.status_code == 200 and pod.connected is True and len(env.emits) == pushed

    # A genuine later disconnect applies; re-POSTing it is a no-op.
    _post(env, cb.device_disconnected, {"source": key, "time": "2026-09-30 10:05:00.000000"})
    assert pod.connected is False and pod.button_pressed is False
    _post(env, cb.device_disconnected, {"source": key, "time": "2026-09-30 10:05:00.000000"})
    assert pod.connected is False
    # Unknown key: 200, nothing touched (as before).
    assert _post(env, cb.device_disconnected, {"source": "nope", "time": "2026-09-30 10:06:00"}).status_code == 200


def test_event_time_accepts_processor_stamp_and_epoch(env):
    from datetime import datetime
    et = env.callback._event_time
    assert et({"time": "2026-09-30 10:00:00.250000"}) == datetime(2026, 9, 30, 10, 0, 0, 250000)
    assert et({"time": 0}) == datetime(1970, 1, 1)
    assert et({"time": "garbage"}) is None and et({}) is None and et(None) is None
