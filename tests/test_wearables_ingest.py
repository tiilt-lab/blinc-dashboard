"""Wearable ingest (routes/wearables.py) and transcript ms stamps on an
in-memory SQLite, with the same stub `app` as test_api_phase1_ingest.py.
Skipped where Flask is not installed (CI), like the other server-side tests.

Covers: heart-rate samples placed on the session clock from a synced
server_t (and the legacy client_now skew), EEG band-power rows, sync
markers, the E4 import, raw native-rate blocks and their CSV export, and
transcripts keeping their float stamps in ms.
"""
import base64
import importlib.util
import itertools
import struct
import os
import sys
import types
from datetime import datetime, timezone

import pytest

flask = pytest.importorskip("flask")
flask_sqlalchemy = pytest.importorskip("flask_sqlalchemy")

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
SERVER = os.path.join(SRC, "server")
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


@pytest.fixture(scope="module")
def env():
    if SERVER not in sys.path:
        sys.path.insert(0, SERVER)
    loaded = sys.modules.get("app")
    if loaded is not None and not getattr(loaded, "_phase1_stub", False):
        pytest.skip("the real app module is loaded in this process")
    before = dict(sys.modules)

    flask_app = flask.Flask("wearables")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app_stub = _stub_module("app", app=flask_app, db=flask_sqlalchemy.SQLAlchemy(flask_app))
    _stub_module("wrappers", verify_device_read_access=lambda f: f,
                 verify_login=lambda *a, **k: (lambda f: f),
                 verify_session_read_access=lambda f: f,
                 session_write_allowed=lambda session_id, user: True)

    import database  # noqa: E402  (resolves the stub app)
    import authz  # noqa: E402
    authz.configure(types.SimpleNamespace(get_device=lambda i: database.get_session_devices(id=i)))
    spec = importlib.util.spec_from_file_location("wearables_routes", os.path.join(SERVER, "routes", "wearables.py"))
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)

    ctx = flask_app.app_context()
    ctx.push()
    tables = [t for t in app_stub.db.metadata.sorted_tables
              if not any(type(c.type).__module__.startswith("sqlalchemy.dialects.mysql") for c in t.columns)]
    app_stub.db.metadata.create_all(app_stub.db.engine, tables=tables)
    yield types.SimpleNamespace(database=database, routes=routes, flask_app=flask_app, db=app_stub.db)
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
    user = User("wear-%d@example.org" % n, "user", password="Abcdef1!")
    env.db.session.add(user)
    env.db.session.flush()
    session = Session(user.id, "W%d" % n)
    session.creation_date = datetime(2026, 11, 3, 15, 0, 0)
    env.db.session.add(session)
    env.db.session.flush()
    device = SessionDevice(session.id, None, "Pod %d" % n)
    env.db.session.add(device)
    env.db.session.commit()
    return device


START_MS = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1000.0


def _post(env, view, payload, **kwargs):
    with env.flask_app.test_request_context("/x", method="POST", json=payload):
        return view(**kwargs).get_json()


def test_heart_rate_from_a_synced_page_lands_at_its_server_time(env, pod):
    body = {"client_now": 0, "samples": [
        {"speaker_id": 3, "alias": "Ana", "sensor": "Polar H10 1", "hr": 72, "rr": [812, 798],
         "t": 1.0, "server_t": START_MS + 63_729.6, "server_beat": START_MS + 63_310.2},
    ]}
    assert _post(env, env.routes.add_device_heart_rate, body, device_id=pod.id) == {"added": 1}
    row = env.database.get_speaker_hr_metrics(session_device_id=pod.id)[0]
    assert (row.time_stamp, row.time_ms, row.beat_ms, row.rr_ms) == (63, 63_730, 63_310, "812,798")


def test_heart_rate_from_an_old_page_uses_the_batch_skew(env, pod, monkeypatch):
    monkeypatch.setattr(env.routes, "_server_now_ms", lambda: START_MS + 10_000.0)
    # The laptop runs 4 s behind the server.
    body = {"client_now": START_MS + 6_000.0, "samples": [
        {"speaker_id": 3, "alias": "", "hr": 70, "t": START_MS + 5_000.0}]}
    _post(env, env.routes.add_device_heart_rate, body, device_id=pod.id)
    row = env.database.get_speaker_hr_metrics(session_device_id=pod.id)[0]
    assert (row.time_stamp, row.time_ms, row.beat_ms, row.speaker_alias) == (9, 9_000, None, "Unknown")


def test_eeg_rows_keep_channel_bands_and_contact(env, pod):
    body = {"samples": [
        {"speaker_id": 4, "alias": "Bo", "sensor": "Muse-1A2B", "channel": "AF7",
         "server_t": START_MS + 2_004.0, "std": 11.5,
         "bands": {"delta": 1.2, "theta": 0.9, "alpha": 1.4, "beta": 0.3, "gamma": "nan"}},
        {"speaker_id": 4, "channel": "FZ", "server_t": START_MS, "bands": {}},
    ]}
    assert _post(env, env.routes.add_device_eeg, body, device_id=pod.id) == {"added": 1}
    row = env.database.get_speaker_eeg_metrics(pod.id)[0].json()
    assert (row["time_ms"], row["channel"], row["alpha"], row["gamma"], row["std_uv"]) == (2_004, "AF7", 1.4, None, 11.5)
    assert env.database.session_device_has_data(pod.id)


def test_sync_markers_and_an_e4_import(env, pod):
    body = {"markers": [
        {"sync_id": "abc", "kind": "prompt", "speaker_id": 4, "sensor_kind": "muse", "sensor": "Muse-1A2B",
         "server_t": START_MS + 1_000.0},
        {"sync_id": "abc", "kind": "tap", "speaker_id": 4, "sensor_kind": "muse", "sensor": "Muse-1A2B",
         "server_t": START_MS + 1_700.0},
        {"sync_id": "abc", "kind": "bogus", "server_t": START_MS},
    ]}
    assert _post(env, env.routes.add_device_sync_markers, body, device_id=pod.id) == {"added": 2}
    imported = {"sync_id": "e4-start", "speaker_id": 5, "sensor_kind": "e4", "sensor": "E4 A01234",
                "epoch_ms": [START_MS + 3_000.0, START_MS + 3_450.0]}
    assert _post(env, env.routes.import_sync_taps, imported,
                 session_id=pod.session_id, session_device_id=pod.id, user={}) == {"added": 2}
    rows = [m.json() for m in env.database.get_sync_markers(pod.id)]
    assert [(m["kind"], m["source"], m["time_ms"]) for m in rows] == [
        ("prompt", "live", 1_000), ("tap", "live", 1_700), ("tap", "import", 3_000), ("tap", "import", 3_450)]


def test_transcripts_keep_their_float_stamps_in_ms(env, pod):
    t = env.database.add_transcript(pod.id, 12.3456, 2.5004, "hello there", False, 0, 0, 0, 0, 0, 0, -1, "A", -1)
    assert (t.start_time, t.length, t.start_ms, t.length_ms) == (12, 3, 12_346, 2_500)
    out = env.database.publish_transcripts_batch(pod.id, [
        {"start_time": 20.25, "length": 1.75, "transcript": "batch row", "speaker_tag": "A", "speaker_id": -1}])
    stored = out[0]["transcript"]
    assert (stored.start_ms, stored.length_ms) == (20_250, 1_750)
    assert stored.json()["start_ms"] == 20_250


def _block(counts):
    return base64.b64encode(struct.pack("<%dh" % len(counts), *counts)).decode()


def test_raw_blocks_store_counts_and_export_at_the_native_rate(env, pod):
    eeg = [i % 7 - 3 for i in range(256 * 4)]
    eeg[5] = -32768  # sample 1, AF7: lost in Bluetooth
    acc = [100, -200, 16384] * 52
    base = {"speaker_id": 6, "alias": "Cy", "sensor": "Muse-9F", "sensor_kind": "muse"}
    body = {"blocks": [
        dict(base, stream="eeg", index=512, server_t=START_MS + 4_000.0, data=_block(eeg)),
        dict(base, stream="acc", index=104, server_t=START_MS + 4_010.0, data=_block(acc)),
        dict(base, stream="eeg", index=768, server_t=START_MS + 5_000.0, data=_block(eeg[:-4])),  # short
        dict(base, stream="ppg", index=0, server_t=START_MS, data=_block(acc)),  # unknown stream
    ]}
    assert _post(env, env.routes.add_device_raw_blocks, body, device_id=pod.id) == {"added": 2}
    assert env.database.session_device_has_data(pod.id)

    with env.flask_app.test_request_context("/x"):
        summary = env.routes.get_pod_raw_summary(session_id=pod.session_id, session_device_id=pod.id).get_json()
        assert sorted((r["stream"], r["seconds"], r["rate"]) for r in summary) == [("acc", 1, 52), ("eeg", 1, 256)]
        resp = env.routes.export_pod_raw(session_id=pod.session_id, session_device_id=pod.id,
                                         sensor_kind="muse", stream="eeg")
        lines = resp.get_data(as_text=True).splitlines()
    assert lines[0] == "speaker_id,speaker_alias,sensor_name,sample_index,time_ms,TP9_uV,AF7_uV,AF8_uV,TP10_uV"
    assert len(lines) == 1 + 256
    assert lines[1] == "6,Cy,Muse-9F,512,4000.0,-1.465,-0.977,-0.488,0.0"
    assert lines[2].split(",")[3:7] == ["513", "4003.9", "0.488", ""]
    with env.flask_app.test_request_context("/x"):
        resp = env.routes.export_pod_raw(session_id=pod.session_id, session_device_id=pod.id,
                                         sensor_kind="muse", stream="acc")
        acc_lines = resp.get_data(as_text=True).splitlines()
    assert len(acc_lines) == 1 + 52
    assert acc_lines[2] == "6,Cy,Muse-9F,105,4029.2,0.006104,-0.012207,1.000001"
