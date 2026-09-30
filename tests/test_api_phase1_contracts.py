"""Phase 1 API source contracts: the index migration's revision chain, and the
shapes other workstreams depend on (?after_id=, join_room partial replay,
per-sid emits bypassing the Redis queue, idempotent ingest, anonymous-route
rate limits). Source-level like test_schema_integrity.py, so they run under
CI without Flask.
"""
import ast
import os
import re

SERVER = os.path.join(os.path.dirname(__file__), "..", "src", "server")
VERSIONS = os.path.join(SERVER, "migrations", "versions")

NEW_REVISION = "f8ae4e72c79c"
PREVIOUS_HEAD = "b7e4d2a1c9f0"
# index name -> (model file, declaration the model must carry)
INDEXES = {
    "ux_sd_processing_key": ("session_device.py", "db.Index('ux_sd_processing_key', 'processing_key', unique=True)"),
    "ux_user_email": ("user.py", "db.Index('ux_user_email', 'email', unique=True)"),
    "ix_transcript_speaker_id": ("transcript.py", "speaker_id = db.Column(db.Integer, index=True)"),
    "ix_session_passcode": ("session.py", "db.Index('ix_session_passcode', 'passcode')"),
    "ix_session_owner_created": ("session.py", "db.Index('ix_session_owner_created', 'owner_id', 'creation_date')"),
}


def _read(*parts):
    with open(os.path.join(SERVER, *parts)) as f:
        return f.read()


def _revisions():
    # {revision: (filename, down_revision)} parsed without importing alembic.
    out = {}
    for fn in os.listdir(VERSIONS):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(VERSIONS, fn)) as f:
            tree = ast.parse(f.read())
        rev = down = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == "revision":
                    rev = ast.literal_eval(node.value)
                elif node.targets[0].id == "down_revision":
                    down = ast.literal_eval(node.value)
        assert rev, f"{fn}: no revision id"
        assert rev not in out, f"duplicate revision id {rev} ({fn} and {out[rev][0]})"
        out[rev] = (fn, down)
    return out


def test_new_migration_extends_the_previous_head_and_is_the_only_head():
    revs = _revisions()
    assert NEW_REVISION in revs
    assert revs[NEW_REVISION][1] == PREVIOUS_HEAD
    referenced = set()
    for _fn, down in revs.values():
        for d in (down if isinstance(down, (tuple, list)) else (down,)):
            if d is not None:
                referenced.add(d)
    assert referenced <= set(revs), f"dangling down_revision(s): {referenced - set(revs)}"
    assert set(revs) - referenced == {NEW_REVISION}


def test_migration_index_names_are_unique_new_and_fully_reverted():
    fn = _revisions()[NEW_REVISION][0]
    src = _read("migrations", "versions", fn)
    created = re.findall(r"op\.create_index\('([^']+)'", src)
    dropped = re.findall(r"op\.drop_index\('([^']+)'", src)
    assert sorted(created) == sorted(INDEXES)
    assert len(set(created)) == len(created)
    assert sorted(dropped) == sorted(created), "downgrade must drop every index upgrade creates"
    # Names must not already exist in any earlier migration.
    for other in os.listdir(VERSIONS):
        if other.endswith(".py") and other != fn:
            text = _read("migrations", "versions", other)
            for name in created:
                assert name not in text, f"{name} already used in {other}"


def test_models_declare_the_same_indexes():
    for name, (model, declaration) in INDEXES.items():
        assert declaration in _read("tables", model), f"{model} must declare {name}: {declaration}"


# ---------------------------------------------------------------- shapes

def _block_after(src, marker, lines=10):
    i = src.index(marker)
    return "\n".join(src[i:].split("\n")[:lines])


def test_client_routes_take_after_id_and_are_rate_limited():
    src = _read("routes", "session.py")
    for path in ("/api/v1/devices/<int:device_id>/transcripts/client",
                 "/api/v1/devices/<int:device_id>/transcriptspeakermetrics/client",
                 "/api/v1/devices/<int:device_id>/videometrics/client",
                 "/api/v1/sessions/<int:session_id>/devices/<int:device_id>/transcripts"):
        block = _block_after(src, "@api_routes.route('%s'" % path)
        assert "after_id=_after_id()" in block, path
    for path in ("/api/v1/devices/<int:device_id>/transcripts/client",
                 "/api/v1/devices/<int:device_id>/transcriptspeakermetrics/client",
                 "/api/v1/devices/<int:device_id>/videometrics/client",
                 "/api/v1/sessions/student/passcode/<string:passcode>",
                 "/api/v1/sessions/student/sessionid/<int:session_id>",
                 "/api/v1/sessions/student/alias/<string:alias>",
                 "/api/v1/students/<username>/longitudinal",
                 "/api/v1/devices/<int:session_device_id>/session_device"):
        block = _block_after(src, "@api_routes.route('%s'" % path, lines=4)
        assert "@limiter.limit(" in block, f"{path} must carry a per-IP limit"
    assert "request.args.get('after_id', type=int)" in src


def test_database_helpers_accept_after_id():
    src = _read("database.py")
    assert re.search(r"def get_transcripts\([^)]*after_id=None", src)
    assert re.search(r"def get_speaker_video_metrics\([^)]*after_id=None", src)
    assert "Transcript.id > after_id" in src
    assert "SpeakerVideoMetrics.id > after_id" in src
    assert ".exists())" in _block_after(src, "def get_session_ids_with_video", lines=12)


def test_join_room_replays_only_newer_rows_and_bypasses_the_queue():
    src = _read("routes", "socket.py")
    assert "message.get('last_transcript_id')" in src
    assert "message.get('last_video_metric_id')" in src
    assert "after_id=last_transcript_id" in src
    assert "after_id=last_video_metric_id" in src
    # Every per-sid emit in the handlers skips the Redis message queue.
    assert src.count("emit(") >= 6
    assert src.count("ignore_queue=True") == src.count("emit(")


def test_callbacks_use_the_idempotent_batched_helpers():
    src = _read("routes", "callback.py")
    assert "database.get_or_add_transcript(" in src
    assert "database.add_speaker_video_metrics_batch(" in src
    assert "database.add_speaker_transcript_metrics_batch(" in src
    assert "database.add_keyword_usage(" not in src
    assert "database.add_speaker_video_metrics(" not in src
    assert "_last_connect_time" in src and "def _event_time(" in src


def test_stop_posthoc_queue_sends_a_pod_ticket():
    src = _read("routes", "session.py")
    assert "from redis_helper import RedisSessions, RedisPosthocTicket" in src
    block = _block_after(src, "def stop_posthoc_queue", lines=22)
    assert "RedisPosthocTicket.mint(did)" in block
    assert "'ticket': ticket" in block
