"""Enrollment tokens (2026-09-30 audit, Phase 1 item 7b).

The live audio socket and the post-hoc video socket used to accept
``save-audio-video-fingerprinting`` plus media for any alias with no
credential: anyone could replace or delete a student's voice/face print or
burn CPU/GPU decoding media. Now /student/addstudent mints a short-lived
per-alias token (common/enrollment_token) and both enrollment handlers verify
it before storing any state -- and therefore before any binary is accepted.

The check itself is unit-tested with a fake Redis. The wiring -- gate
placement in both handlers, the fingerprint-force guard, the API route, the
sign-up page -- is a source/AST contract, since the services and the Flask
app do not run in CI.
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
sys.path.insert(0, os.path.join(SRC, "common"))

import enrollment_token as et  # noqa: E402

DENIED = "Enrollment not authorised. Start again from the sign-up page."
GATE_TEST = "not enrollment_allows(redis_client._redis(), data.get('token'), data.get('alias'))"


class _FakeRedis:
    """SET ... EX / GET with a manual clock and a raise switch."""
    def __init__(self):
        self.store = {}
        self.now = 1000.0
        self.fail = False

    def set(self, key, val, ex=None, nx=False):
        self.store[key] = (val, None if ex is None else self.now + ex)
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        item = self.store.get(key)
        if item is None:
            return None
        val, exp = item
        if exp is not None and self.now >= exp:
            del self.store[key]
            return None
        return val


# --- the check -------------------------------------------------------------

def test_mint_stores_username_under_prefixed_key_with_ttl():
    r = _FakeRedis()
    t = et.mint(r, "alice")
    assert r.store[et.make_key(t)] == ("alice", r.now + et.TOKEN_TTL)
    assert et.TOKEN_TTL == 30 * 60
    assert et.make_key(t).startswith("enroll_token:")


def test_token_allows_only_its_own_alias():
    r = _FakeRedis()
    t = et.mint(r, "alice")
    assert et.enrollment_allows(r, t, "alice") is True
    assert et.enrollment_allows(r, t, "bob") is False
    assert et.enrollment_allows(r, t, "Alice") is False, "bound to the stored spelling"
    assert et.enrollment_allows(r, t, "alice/../x") is False
    assert et.enrollment_allows(r, t, "") is False
    assert et.enrollment_allows(r, t, None) is False
    assert et.enrollment_allows(r, t, 42) is False, "alias arrives untyped off the socket"


def test_unknown_missing_or_malformed_token_is_denied():
    r = _FakeRedis()
    et.mint(r, "alice")
    assert et.enrollment_allows(r, "not-a-token", "alice") is False
    assert et.enrollment_allows(r, None, "alice") is False
    assert et.enrollment_allows(r, "", "alice") is False
    assert et.enrollment_allows(r, 12345, "alice") is False
    assert et.enrollment_allows(r, "x" * 129, "alice") is False


def test_token_is_reusable_until_it_expires():
    r = _FakeRedis()
    t = et.mint(r, "alice")
    for _ in range(2):  # the sign-up page sends it on the audio AND video socket
        assert et.enrollment_allows(r, t, "alice") is True
    r.now += et.TOKEN_TTL
    assert et.enrollment_allows(r, t, "alice") is False


def test_redis_failure_fails_closed():
    r = _FakeRedis()
    t = et.mint(r, "alice")
    r.fail = True
    assert et.enrollment_allows(r, t, "alice") is False


def test_bytes_value_from_a_non_decoding_client_still_matches():
    r = _FakeRedis()
    t = et.mint(r, "alice")
    r.store[et.make_key(t)] = (b"alice", None)
    assert et.enrollment_allows(r, t, "alice") is True


def test_tokens_are_unique_and_url_safe():
    r = _FakeRedis()
    tokens = {et.mint(r, "alice") for _ in range(50)}
    assert len(tokens) == 50
    for t in tokens:
        assert all(c.isalnum() or c in "-_" for c in t)


# --- service wiring (AST contract) -----------------------------------------

def _read(*parts):
    with open(os.path.join(REPO, *parts)) as f:
        return f.read()


def _function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _type_handler(fn, type_name):
    """The ``if data['type'] == <type_name>:`` node inside ``fn``."""
    for node in ast.walk(fn):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
            continue
        left, comps = node.test.left, node.test.comparators
        if (isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name)
                and left.value.id == "data" and isinstance(left.slice, ast.Constant)
                and left.slice.value == "type" and len(comps) == 1
                and isinstance(comps[0], ast.Constant) and comps[0].value == type_name):
            return node
    return None


def _self_writes(stmts):
    """Attributes assigned on ``self`` anywhere inside ``stmts``, in order."""
    names = []
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        names.append(t.attr)
    return names


def _consts(stmts):
    return {c.value for st in stmts for c in ast.walk(st) if isinstance(c, ast.Constant)}


SERVERS = [("src", "audio_processing", "server.py"),
           ("src", "video_processing", "server_posthoc.py")]


def test_both_enrollment_handlers_verify_the_token_before_storing_anything():
    for parts in SERVERS:
        s = _read(*parts)
        name = "/".join(parts)
        assert "from enrollment_token import enrollment_allows" in s, name
        handler = _type_handler(_function(ast.parse(s), "process_json"),
                                "save-audio-video-fingerprinting")
        assert handler is not None, name
        gate = handler.body[0]
        assert isinstance(gate, ast.If), f"{name}: the token check must be the first statement"
        assert ast.unparse(gate.test) == GATE_TEST, name
        assert isinstance(gate.body[-1], ast.Return), f"{name}: a denied enrollment must be ignored"
        assert DENIED in _consts(gate.body), name
        assert _self_writes([gate]) == [], f"{name}: nothing may be stored on denial"
        # process_binary keys enrollment media on stream_data, which is only
        # set (below the gate) once the token checked out.
        assert "stream_data" in _self_writes(handler.body[1:]), name


def test_binary_media_is_only_accepted_once_the_gate_set_stream_data():
    for parts in SERVERS:
        s = _read(*parts)
        name = "/".join(parts)
        pb = _function(ast.parse(s), "process_binary")
        src = ast.get_source_segment(s, pb)
        assert "self.stream_data == 'audio-video-fingerprint'" in src, name
        # Only the (gated) enrollment handler may put stream_data into the
        # enrollment mode; the 'start' branch copies the client's streamdata
        # but sits behind the pod processing-key check.
        handler = _type_handler(_function(ast.parse(s), "process_json"),
                                "save-audio-video-fingerprinting")
        assert "'audio-video-fingerprint'" in ast.unparse(handler), name


def test_fingerprint_force_needs_an_authorised_enrollment_in_progress():
    s = _read("src", "audio_processing", "server.py")
    handler = _type_handler(_function(ast.parse(s), "process_json"), "fingerprint-force")
    assert handler is not None
    guard = handler.body[0]
    assert isinstance(guard, ast.If) and isinstance(guard.body[-1], ast.Return)
    assert ast.unparse(guard.test) == "getattr(self, 'stream_data', None) != 'audio-video-fingerprint'"
    assert "fingerprint_force" not in _self_writes([guard])
    assert "fingerprint_force" in _self_writes(handler.body[1:])


def test_posthoc_ticket_gate_is_untouched_by_the_enrollment_gate():
    # The pod-message ticket gate (item 7a) still precedes every handler.
    s = _read("src", "video_processing", "server_posthoc.py")
    body = s[s.index("def process_json"):]
    assert body.index("if data['type'] in _TICKETED_TYPES and not self._authorised(data):") \
        < body.index("if data['type'] ==")


# --- API side ----------------------------------------------------------------

def test_addstudent_mints_on_create_and_on_name_matched_reenrol_only():
    s = _read("src", "server", "routes", "student.py")
    assert "from redis_helper import RedisEnrollmentToken" in s
    fn = _function(ast.parse(s), "add_students")
    mints = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "RedisEnrollmentToken.mint"]
    assert len(mints) == 2, "create path + re-enrol path"
    for m in mints:
        assert ast.unparse(m.args[0]) == "student.username", "bind to the stored spelling the page echoes back"
    guarded = [n for n in ast.walk(fn) if isinstance(n, ast.If) and ast.unparse(n.test) == "name_matches"
               and any(isinstance(c, ast.Call) and ast.unparse(c.func) == "RedisEnrollmentToken.mint"
                       for st in n.body for c in ast.walk(st))]
    assert len(guarded) == 1, "the re-enrol mint must sit under `if name_matches:`"
    assert "enrollment_token" in _consts(fn.body)


def test_redis_helper_mints_with_the_shared_module():
    s = _read("src", "server", "redis_helper.py")
    assert "import enrollment_token as _enrollment_token" in s
    assert "class RedisEnrollmentToken" in s
    assert "_enrollment_token.mint(r, username)" in s
    assert "TTL = _enrollment_token.TOKEN_TTL" in s


# --- sign-up page ------------------------------------------------------------

def test_signup_page_sends_the_token_on_both_sockets_and_stops_on_denial():
    jsx = _read("frontend", "src", "profile-creation", "profile-creation-component.jsx")
    msg = jsx[jsx.index('type: "save-audio-video-fingerprinting"'):]
    msg = msg[:msg.index("}")]
    assert "token: studentObject.enrollment_token" in msg
    assert 'const ENROLLMENT_DENIED = "%s"' % DENIED in jsx
    assert jsx.count("if (message['message'] === ENROLLMENT_DENIED)") == 2, "audio + video onmessage"
    assert "stopEnrollment(message['message'])" in jsx
    js = _read("frontend", "src", "services", "auth-service.js")
    assert 'student.enrollment_token = userobj["enrollment_token"]' in js
    assert 'student.enrollment_token = err["enrollment_token"]' in js
