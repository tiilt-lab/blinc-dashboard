"""The overview's Download dialog (src/server/exports.py and
src/server/routes/session_export.py): one ZIP of CSV files for the chosen
datasets, and per-pod video downloads that only signed-in session readers
may use (a pod's processing key does not open them).

Runs the real routes and access wrappers on an in-memory SQLite database. The
recording helpers of routes/session.py are replaced by a stand-in over a temp
folder, so no ffmpeg runs.
"""
import csv
import importlib.util
import io
import itertools
import os
import sys
import threading
import types
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SERVER = os.path.join(SRC, "server")
sys.path.insert(0, SERVER)
sys.path.insert(0, os.path.join(SRC, "common"))

import flask  # noqa: E402
import flask_sqlalchemy  # noqa: E402

_seq = itertools.count(1)


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


class _Recordings:
    """Stand-in for routes/session.py's recording helpers: originals are temp
    files, the remux 'build' copies them unless told to fail."""

    def __init__(self, folder):
        self.folder = folder
        self.files = {}          # device id -> [original paths]
        self.fail = set()        # device ids whose build falls back to a raw file
        self.release = threading.Event()
        self.release.set()
        self.segment_calls = []

    def add(self, device_id, *payloads):
        paths = []
        for i, payload in enumerate(payloads):
            path = os.path.join(self.folder, "%d-rec%d-%d.webm" % (device_id, i, len(payloads)))
            with open(path, "wb") as f:
                f.write(payload)
            paths.append(path)
        self.files[device_id] = paths

    def _find_original_videos(self, device_id):
        return list(self.files.get(device_id, []))

    def _fixed_video_target(self, device_id):
        originals = self.files.get(device_id)
        if not originals:
            return None
        fixed = os.path.join(self.folder, "fixed-%d-%d.webm" % (device_id, len(originals)))
        return types.SimpleNamespace(originals=originals, fixed=fixed, seg_json=fixed + ".segments.json",
                                     ext=".webm")

    def _fixed_video_ready(self, target):
        return os.path.exists(target.fixed)

    def _fixed_video_path(self, device_id):
        self.release.wait(5)
        target = self._fixed_video_target(device_id)
        if device_id in self.fail:
            return target.originals[0]
        with open(target.fixed + ".tmp", "wb") as out:
            for p in target.originals:
                with open(p, "rb") as f:
                    out.write(f.read())
        os.replace(target.fixed + ".tmp", target.fixed)   # atomic, as the real remux
        return target.fixed

    def _video_segments(self, session_id, device_id, prepare=True):
        self.segment_calls.append(prepare)
        return [{"start": 10.0 + 100 * i, "video_start": 50.0 * i, "duration": 50.0}
                for i in range(len(self.files.get(device_id, [])))]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    loaded = sys.modules.get("app")
    if loaded is not None and not getattr(loaded, "_phase1_stub", False):
        pytest.skip("the real app module is loaded in this process")
    before = dict(sys.modules)
    flask_app = flask.Flask("session_export")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.secret_key = "test"
    app_stub = _stub_module(
        "app", app=flask_app, db=flask_sqlalchemy.SQLAlchemy(flask_app),
        limiter=types.SimpleNamespace(limit=lambda *a, **k: (lambda f: f)),
        socketio=types.SimpleNamespace(emit=lambda *a, **k: None))
    recordings = _Recordings(str(tmp_path_factory.mktemp("recordings")))
    _stub_module("routes.session", **{name: getattr(recordings, name) for name in (
        "_find_original_videos", "_fixed_video_target", "_fixed_video_ready", "_fixed_video_path",
        "_video_segments")})

    for name in ("database", "wrappers", "exports", "utility", "folder_access", "authz"):
        sys.modules.pop(name, None)   # bind to the stub app above
    import database
    import authz
    authz.configure(authz.Deps(get_device=lambda did: database.get_session_devices(id=did),
                               get_transcript=database.get_transcript,
                               session_access=lambda sid, user, write: False,
                               key_grants=lambda device: False))
    spec = importlib.util.spec_from_file_location(
        "session_export_routes", os.path.join(SERVER, "routes", "session_export.py"))
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    flask_app.register_blueprint(routes.api_routes)

    ctx = flask_app.app_context()
    ctx.push()
    tables = [t for t in app_stub.db.metadata.sorted_tables
              if not any(type(c.type).__module__.startswith("sqlalchemy.dialects.mysql") for c in t.columns)]
    app_stub.db.metadata.create_all(app_stub.db.engine, tables=tables)
    yield types.SimpleNamespace(database=database, db=app_stub.db, routes=routes, app=flask_app,
                                recordings=recordings)
    app_stub.db.session.remove()
    ctx.pop()
    for name, module in list(sys.modules.items()):
        if name not in before and _ours(module):
            del sys.modules[name]
    for name, module in before.items():
        if sys.modules.get(name) is not module and _ours(sys.modules.get(name, module)):
            sys.modules[name] = module


def _user(env, role="user"):
    from tables.user import User
    user = User("export-%d@example.org" % next(_seq), role, password="Abcdef1!")
    env.db.session.add(user)
    env.db.session.commit()
    return user


def _pod(env, session, name):
    from tables.session_device import SessionDevice
    pod = SessionDevice(session.id, None, name)
    env.db.session.add(pod)
    env.db.session.flush()
    pod.create_key()
    env.db.session.commit()
    return pod


@pytest.fixture
def world(env):
    """A session with two pods: Group A spoke, wore heart-rate straps and was
    filmed; Group B only spoke. Plus a stranger, an admin and another
    session's pod."""
    from tables.session import Session
    from tables.speaker import Speaker
    from tables.speaker_video_metrics import SpeakerVideoMetrics
    d = env.database
    owner = _user(env)
    session = Session(owner.id, "Chem lab 3")
    env.db.session.add(session)
    env.db.session.commit()
    a, b = _pod(env, session, "Group A"), _pod(env, session, "Group B")
    for alias in ("Ana", "Ben"):
        env.db.session.add(Speaker(a.id, alias))
    env.db.session.commit()
    d.add_transcript(a.id, 5, 3, "What if we heat it?", True, 0, 0, 0, 0, 0, 0, -1, "Ana", -1)
    d.add_transcript(a.id, 12, 4, "Then it boils.", False, 0, 0, 0, 0, 0, 0, -1, "Ben", -1)
    d.add_transcript(b.id, 7, 2, "Okay.", False, 0, 0, 0, 0, 0, 0, -1, "unknown", -1)
    # two notifications in the same second: RR order must follow insertion
    d.add_speaker_hr_metrics_batch(a.id, [
        {"speaker_id": None, "speaker_alias": "Ana", "sensor_name": "Polar H10 A1", "time_stamp": 30,
         "heart_rate": 72, "rr_ms": "830,845"},
        {"speaker_id": None, "speaker_alias": "Ana", "sensor_name": "Polar H10 A1", "time_stamp": 30,
         "heart_rate": 71, "rr_ms": "850"},
        {"speaker_id": None, "speaker_alias": "Ana", "sensor_name": "Polar H10 A1", "time_stamp": 29,
         "heart_rate": 74, "rr_ms": ""},
    ])
    for ts, ana, ben in [(100, "laptop", "laptop"), (101, "person:Ben", "Nothing"), (102, "laptop", None)]:
        env.db.session.add(SpeakerVideoMetrics(a.id, "Ana", ts, "happy", 80, ana))
        if ben is not None:
            env.db.session.add(SpeakerVideoMetrics(a.id, "Ben", ts, "neutral", 60, ben))
    env.db.session.commit()
    env.recordings.add(a.id, b"part-one|", b"part-two")
    other_session = Session(_user(env).id, "Someone else's")
    env.db.session.add(other_session)
    env.db.session.commit()
    elsewhere = _pod(env, other_session, "Their pod")
    env.recordings.add(elsewhere.id, b"theirs")
    return types.SimpleNamespace(owner=owner, session=session, a=a, b=b, stranger=_user(env),
                                 admin=_user(env, "admin"), elsewhere=elsewhere)


def _client(env, user=None):
    client = env.app.test_client()
    if user is not None:
        with client.session_transaction() as s:
            s["user"] = {"id": user.id}
    return client


def _zip(resp):
    assert resp.status_code == 200, resp.get_json()
    return zipfile.ZipFile(io.BytesIO(resp.data))


def _rows(archive, name):
    text = archive.read(name).decode("utf-8")
    assert text.startswith("﻿"), "CSV files carry a BOM so Excel reads UTF-8"
    return list(csv.reader(io.StringIO(text[1:])))


def _export(env, world, items, user="owner", window=None):
    url = "/api/v1/sessions/%d/export.zip?items=%s" % (world.session.id, items)
    if window is not None:
        url += "&window=%s" % window
    return _client(env, getattr(world, user) if user else None).get(url)


# ---------------------------------------------------------------- data ZIP

def test_zip_holds_a_readme_and_one_csv_per_chosen_dataset(env, world):
    resp = _export(env, world, "heart_rate,pods")
    assert resp.headers["Content-Type"] == "application/zip"
    assert "session%d_Chem_lab_3_data.zip" % world.session.id in resp.headers["Content-Disposition"]
    archive = _zip(resp)
    assert archive.namelist() == ["README.txt", "pods.csv", "heart_rate.csv"]
    readme = archive.read("README.txt").decode("utf-8")
    assert "Chem lab 3" in readme and "seconds since the session was created" in readme
    assert "heart_rate.csv" in readme and "Columns: pod_id, pod_name, row_id" in readme


def test_heart_rate_and_rr_intervals_keep_the_strap_order(env, world):
    archive = _zip(_export(env, world, "heart_rate,rr_intervals"))
    hr = _rows(archive, "heart_rate.csv")
    assert hr[0] == ["pod_id", "pod_name", "row_id", "speaker_id", "speaker_alias", "sensor_name",
                     "time_s", "heart_rate", "rr_ms"]
    assert [(r[6], r[7], r[8]) for r in hr[1:]] == [("29", "74", ""), ("30", "72", "830,845"), ("30", "71", "850")]
    rr = _rows(archive, "rr_intervals.csv")
    assert [(r[5], r[6], r[7]) for r in rr[1:]] == [("30", "0", "830"), ("30", "1", "845"), ("30", "0", "850")]
    assert rr[1][2] == hr[2][2], "row_id links a beat to its notification"


def test_pods_participants_and_every_transcript_line(env, world):
    archive = _zip(_export(env, world, "pods,participants,transcripts"))
    pods = _rows(archive, "pods.csv")
    assert pods[1:] == [[str(world.a.id), "Group A", "0", "11", "2", "2"],
                        [str(world.b.id), "Group B", "0", "2", "0", "0"]]
    assert [r[3] for r in _rows(archive, "participants.csv")[1:]] == ["Ana", "Ben"]
    lines = _rows(archive, "transcripts.csv")
    assert [(r[1], r[3], r[4], r[6], r[8], r[10]) for r in lines[1:]] == [
        ("Group A", "5", "8", "Ana", "1", "What if we heat it?"),
        ("Group A", "12", "16", "Ben", "0", "Then it boils."),
        ("Group B", "7", "9", "unknown", "0", "Okay.")]


def test_markers_list_each_laptops_protocol_moments_in_time_order(env, world):
    env.database.add_event_markers(world.b.id, [
        {"marker_id": "b1", "label": "eyes_open_start", "time_stamp": 61, "time_ms": 61500, "sensors": None}])
    env.database.add_event_markers(world.a.id, [
        {"marker_id": "a1", "label": "session_joined", "time_stamp": 4, "time_ms": 4020,
         "sensors": "polar:Polar H10 A1 (Ana)"}])
    rows = _rows(_zip(_export(env, world, "markers")), "markers.csv")
    assert rows == [["pod_id", "pod_name", "label", "time_s", "time_ms", "sensors", "detail"],
                    [str(world.a.id), "Group A", "session_joined", "4.02", "4020", "polar:Polar H10 A1 (Ana)", ""],
                    [str(world.b.id), "Group B", "eyes_open_start", "61.5", "61500", "", ""]]


def test_joint_attention_lists_each_moment_and_shared_target(env, world):
    archive = _zip(_export(env, world, "joint_attention"))
    rows = _rows(archive, "joint_attention.csv")[1:]
    # 102: only Ana tracked, not a moment; 101: no shared target
    assert [r[2:] for r in rows] == [["100", "2", "1", "laptop", "Ana;Ben"], ["101", "2", "0", "", ""]]


def test_video_segments_never_start_a_conversion(env, world):
    env.recordings.segment_calls.clear()
    archive = _zip(_export(env, world, "video_segments"))
    rows = _rows(archive, "video_segments.csv")[1:]
    assert [r[:3] + r[4:] for r in rows] == [[str(world.a.id), "Group A", "1", "10.0", "0.0", "50.0"],
                                             [str(world.a.id), "Group A", "2", "110.0", "50.0", "50.0"]]
    assert rows[0][3].startswith("%d-rec0" % world.a.id)
    assert env.recordings.segment_calls == [False]


@pytest.mark.parametrize("window", [0, 10])
def test_the_older_metric_exports_ride_along_with_a_window(env, world, window):
    archive = _zip(_export(env, world, "transcript_metrics,video_metrics,transcript_video_metrics", window=window))
    video = _rows(archive, "video_metrics.csv")
    assert video[0][:2] == (["Device ID", "Device Name"] if window == 0 else ["Group ID", "Group Name"])
    if window == 0:
        assert len(video) == 1 + 5 and video[1][2] == "0:01:40"
    assert _rows(archive, "transcript_metrics.csv")[0]
    readme = archive.read("README.txt").decode("utf-8")
    assert ("Window size for the metric files: %s" % ("none" if window == 0 else "10 s")) in readme


def test_bad_requests_are_explained(env, world):
    for items, window, message in [("", None, "Choose at least one"), ("heart_rate,secrets", None, "Unknown data: secrets"),
                                   ("pods", "abc", "whole number"), ("pods", "99999", "between 0 and 3600")]:
        resp = _export(env, world, items, window=window)
        assert resp.status_code == 400 and message in resp.get_json()["message"]


def test_only_session_readers_get_the_zip(env, world):
    assert _export(env, world, "pods", user=None).status_code == 401
    assert _export(env, world, "pods", user="stranger").status_code == 404
    assert _export(env, world, "pods", user="admin").status_code == 200
    key = _client(env).get("/api/v1/sessions/%d/export.zip?items=pods" % world.session.id,
                           headers={"X-Processing-Key": world.a.processing_key})
    assert key.status_code == 401, "a pod's processing key does not open the session's data"


# ---------------------------------------------------------------- videos

def _video(env, world, pod, suffix="", user="owner", **kw):
    url = "/api/v1/sessions/%d/device/%d/video/download%s" % (world.session.id, pod.id, suffix)
    return _client(env, getattr(world, user) if user else None).get(url, **kw)


def _wait_ready(env, world, pod):
    for _ in range(200):
        status = _video(env, world, pod, "/status").get_json()
        if status["state"] != "preparing":
            return status
        threading.Event().wait(0.01)
    raise AssertionError("still preparing")


def test_a_video_is_prepared_in_the_background_then_downloaded(env, world):
    env.recordings.release.clear()          # hold the "ffmpeg" run
    first = _video(env, world, world.a, "/status")
    assert first.status_code == 200 and first.get_json() == {"state": "preparing", "segments": 2}
    early = _video(env, world, world.a)
    assert early.status_code == 409 and early.get_json()["state"] == "preparing"
    env.recordings.release.set()
    status = _wait_ready(env, world, world.a)
    assert status == {"state": "ready", "segments": 2, "bytes": len(b"part-one|part-two"),
                      "filename": "session%d_Group_A.webm" % world.session.id}
    resp = _video(env, world, world.a)
    assert resp.status_code == 200 and resp.data == b"part-one|part-two"
    assert resp.headers["Content-Type"] == "video/webm"
    assert 'attachment; filename=session%d_Group_A.webm' % world.session.id in resp.headers["Content-Disposition"]
    resumed = _video(env, world, world.a, headers={"Range": "bytes=9-"})
    assert resumed.status_code == 206 and resumed.data == b"part-two"


def test_a_failed_build_offers_the_raw_parts(env, world):
    env.recordings.fail.add(world.a.id)
    env.recordings.add(world.a.id, b"one", b"two", b"three")     # a new segment: a new target
    try:
        status = _wait_ready(env, world, world.a)
        assert status == {"state": "failed", "segments": 3,
                          "parts": [{"part": 1, "bytes": 3}, {"part": 2, "bytes": 3}, {"part": 3, "bytes": 5}]}
        assert _video(env, world, world.a).status_code == 409
        part = _video(env, world, world.a, "?part=3")
        assert part.status_code == 200 and part.data == b"three"
        assert "session%d_Group_A_part3of3.webm" % world.session.id in part.headers["Content-Disposition"]
        assert _video(env, world, world.a, "?part=4").status_code == 404
    finally:
        env.recordings.fail.discard(world.a.id)


def test_a_pod_without_a_recording_says_so(env, world):
    for suffix in ("/status", ""):
        resp = _video(env, world, world.b, suffix)
        assert resp.status_code == 404 and resp.get_json()["message"] == "No recording."


def test_only_session_readers_get_videos(env, world):
    for suffix in ("/status", "", "?part=1"):
        assert _video(env, world, world.a, suffix, user=None).status_code == 401
        assert _video(env, world, world.a, suffix, user="stranger").status_code == 404
        keyed = _video(env, world, world.a, suffix, user=None,
                       headers={"X-Processing-Key": world.a.processing_key})
        assert keyed.status_code == 401, "a pod's processing key does not download its video"
        keyed = _client(env).get("/api/v1/sessions/%d/device/%d/video/download%s?key=%s" % (
            world.session.id, world.a.id, suffix, world.a.processing_key))
        assert keyed.status_code == 401
    # a pod of another session, named under this one, is not found
    for suffix in ("/status", "", "?part=1"):
        assert _video(env, world, world.elsewhere, suffix).status_code == 404
