"""Event markers from the join page (src/server/routes/event_markers.py,
tables/event_marker.py): joining, starting the recording and each baseline
step, stored on the session clock in ms like the wearable streams.

Runs the real route and access wrappers on an in-memory SQLite database.
"""
import csv
import datetime
import importlib.util
import io
import itertools
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SERVER = os.path.join(SRC, "server")
sys.path.insert(0, SERVER)
sys.path.insert(0, os.path.join(SRC, "common"))

import flask  # noqa: E402
import flask_sqlalchemy  # noqa: E402

_seq = itertools.count(1)
# Session second 0 in the tests: 2026-11-07 15:00:00 UTC.
START = datetime.datetime(2026, 11, 7, 15, 0, 0)
START_MS = START.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000.0


def _stub_module(name, **attrs):
    module = types.ModuleType(name)
    module._phase1_stub = True
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _ours(module):
    path = getattr(module, "__file__", None) or ""
    return getattr(module, "_phase1_stub", False) or os.path.abspath(path).startswith(SRC)


@pytest.fixture(scope="module")
def env():
    loaded = sys.modules.get("app")
    if loaded is not None and not getattr(loaded, "_phase1_stub", False):
        pytest.skip("the real app module is loaded in this process")
    before = dict(sys.modules)
    flask_app = flask.Flask("event_markers")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.secret_key = "test"
    app_stub = _stub_module(
        "app", app=flask_app, db=flask_sqlalchemy.SQLAlchemy(flask_app),
        limiter=types.SimpleNamespace(limit=lambda *a, **k: (lambda f: f)),
        socketio=types.SimpleNamespace(emit=lambda *a, **k: None))
    for name in ("database", "wrappers", "utility", "folder_access", "physio_time"):
        sys.modules.pop(name, None)   # bind to the stub app above
    import database
    spec = importlib.util.spec_from_file_location(
        "event_marker_routes", os.path.join(SERVER, "routes", "event_markers.py"))
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    flask_app.register_blueprint(routes.api_routes)
    ctx = flask_app.app_context()
    ctx.push()
    tables = [t for t in app_stub.db.metadata.sorted_tables
              if not any(type(c.type).__module__.startswith("sqlalchemy.dialects.mysql") for c in t.columns)]
    app_stub.db.metadata.create_all(app_stub.db.engine, tables=tables)
    yield types.SimpleNamespace(database=database, db=app_stub.db, routes=routes, app=flask_app)
    app_stub.db.session.remove()
    ctx.pop()
    for name, module in list(sys.modules.items()):
        if name not in before and _ours(module):
            del sys.modules[name]
    for name, module in before.items():
        if sys.modules.get(name) is not module and _ours(sys.modules.get(name, module)):
            sys.modules[name] = module


def _user(env):
    from tables.user import User
    user = User("markers-%d@example.org" % next(_seq), "user", password="Abcdef1!")
    env.db.session.add(user)
    env.db.session.commit()
    return user


@pytest.fixture
def world(env):
    from tables.session import Session
    from tables.session_device import SessionDevice
    owner = _user(env)
    session = Session(owner.id, "Baseline study")
    session.creation_date = START
    env.db.session.add(session)
    env.db.session.commit()
    pods = []
    for name in ("Group A", "Group B"):
        pod = SessionDevice(session.id, None, name)
        env.db.session.add(pod)
        env.db.session.flush()
        pod.create_key()
        pods.append(pod)
    env.db.session.commit()
    return types.SimpleNamespace(owner=owner, session=session, a=pods[0], b=pods[1], stranger=_user(env))


def _post(env, pod, markers, key=None, client_now=None):
    body = {"markers": markers}
    if client_now is not None:
        body["client_now"] = client_now
    return env.app.test_client().post("/api/v1/devices/%d/markers/client" % pod.id, json=body,
                                      headers={"X-Processing-Key": key or pod.processing_key})


def _client(env, user=None):
    client = env.app.test_client()
    if user is not None:
        with client.session_transaction() as s:
            s["user"] = {"id": user.id}
    return client


def test_markers_land_on_the_session_clock(env, world, monkeypatch):
    # A synced page sends server_t; an unsynced one only t, corrected by the
    # batch skew (server now - client_now): here the laptop runs 2 s slow.
    server_now = START_MS + 100_000
    monkeypatch.setattr(env.routes.time, "time", lambda: server_now / 1000.0)
    resp = _post(env, world.a, [
        {"marker_id": "m1", "label": "session_joined", "t": 1, "server_t": START_MS + 12_345.6,
         "sensors": "polar:Polar H10 A1 (Ana)"},
        {"marker_id": "m2", "label": "recording_started", "t": START_MS + 58_000 - 2_000},
    ], client_now=server_now - 2_000)
    assert resp.status_code == 200 and resp.get_json() == {"added": 2}
    rows = env.database.get_event_markers(session_device_id=world.a.id)
    assert [(m.label, m.time_stamp, m.time_ms) for m in rows] == [
        ("session_joined", 12, 12346), ("recording_started", 58, 58000)]
    assert rows[0].sensors == "polar:Polar H10 A1 (Ana)" and rows[1].sensors is None


def test_a_resent_batch_is_stored_once(env, world):
    marker = {"marker_id": "dup-1", "label": "game_start", "server_t": START_MS + 5_000}
    assert _post(env, world.a, [marker]).get_json() == {"added": 1}
    assert _post(env, world.a, [marker, {**marker, "marker_id": "dup-2", "label": "game_end"}]).get_json() == {"added": 1}
    assert [m.label for m in env.database.get_event_markers(session_device_id=world.a.id)] == ["game_start", "game_end"]
    # the same id from another pod is that pod's own press
    assert _post(env, world.b, [marker]).get_json() == {"added": 1}


def test_unknown_or_incomplete_markers_are_skipped(env, world):
    resp = _post(env, world.a, [
        {"marker_id": "x1", "label": "lunch", "server_t": START_MS},
        {"label": "eyes_open_start", "server_t": START_MS},
        {"marker_id": "x3", "label": "eyes_open_start"},
        {"marker_id": "x4", "label": "eyes_open_start", "server_t": START_MS - 9_000},
        "not a marker",
    ])
    assert resp.get_json() == {"added": 1}
    (m,) = env.database.get_event_markers(session_device_id=world.a.id)
    assert (m.label, m.time_ms) == ("eyes_open_start", 0), "before second 0 clamps to 0, like the streams"
    assert _post(env, world.a, []).get_json() == {"added": 0}


def test_a_pod_key_only_writes_its_own_pod(env, world):
    resp = _post(env, world.b, [{"marker_id": "k1", "label": "session_joined", "server_t": START_MS}],
                 key=world.a.processing_key)
    assert resp.status_code == 404
    assert env.database.get_event_markers(session_device_id=world.b.id) == []


def test_pod_markers_and_the_session_csv(env, world):
    _post(env, world.b, [{"marker_id": "b1", "label": "eyes_closed_end", "server_t": START_MS + 70_500}])
    _post(env, world.a, [{"marker_id": "a1", "label": "eyes_closed_start", "server_t": START_MS + 40_250,
                          "detail": "Ana's laptop"}])
    pod = env.app.test_client().get("/api/v1/sessions/%d/device/%d/markers" % (world.session.id, world.a.id),
                                    headers={"X-Processing-Key": world.a.processing_key})
    assert [m["label"] for m in pod.get_json()] == ["eyes_closed_start"]
    url = "/api/v1/sessions/%d/markers.csv" % world.session.id
    resp = _client(env, world.owner).get(url)
    assert resp.status_code == 200 and resp.headers["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(resp.data.decode("utf-8"))))
    assert rows == [
        ["pod_id", "pod_name", "label", "time_s", "time_ms", "sensors", "detail"],
        [str(world.a.id), "Group A", "eyes_closed_start", "40.25", "40250", "", "Ana&#39;s laptop"],
        [str(world.b.id), "Group B", "eyes_closed_end", "70.5", "70500", "", ""],
    ]
    assert _client(env).get(url).status_code == 401
    assert _client(env, world.stranger).get(url).status_code == 404
    keyed = env.app.test_client().get(url, headers={"X-Processing-Key": world.a.processing_key})
    assert keyed.status_code == 401, "a pod key does not open the whole session's markers"


def test_the_table_matches_its_migration():
    from tables.event_marker import EventMarker
    model = {c.name for c in EventMarker.__table__.columns}
    with open(os.path.join(SERVER, "migrations", "versions", "c7d8e9f0a1b2_event_marker.py"), encoding="utf-8") as f:
        migration = f.read()
    assert set(re.findall(r"sa\.Column\('(\w+)'", migration)) == model
    assert "ondelete='CASCADE'" in migration and "uq_event_marker_device_marker" in migration
    fk = next(iter(EventMarker.__table__.c.session_device_id.foreign_keys))
    assert fk.target_fullname == "session_device.id" and fk.ondelete == "CASCADE"
