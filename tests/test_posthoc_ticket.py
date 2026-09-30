"""Post-hoc websocket tickets (2026-09-30 audit, Phase 1 item 7a).

The audio/video post-hoc sockets used to act on a bare ``sessiondeviceid``:
anyone with the URL could wipe a pod's analysis, cancel a run, or occupy the
GPU. Now the API mints a short-lived per-pod ticket (common/posthoc_ticket)
and every socket message that names a pod must carry it.

The check itself is unit-tested with a fake Redis. The wiring — which message
types each service gates, the loopback bind, the queue's trigger messages and
the API route — is a source/AST contract, since the services and the Flask
app do not run in CI.
"""
import ast
import contextlib
import datetime
import os
import sys
import threading
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
sys.path.insert(0, os.path.join(SRC, "common"))
sys.path.insert(0, os.path.join(SRC, "server"))

import posthoc_ticket as pt  # noqa: E402


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

def test_mint_stores_device_under_prefixed_key_with_ttl():
    r = _FakeRedis()
    t = pt.mint(r, 42)
    assert r.store[pt.make_key(t)] == ("42", r.now + pt.TICKET_TTL)
    assert pt.TICKET_TTL == 900
    assert pt.make_key(t).startswith("posthoc_ticket:")


def test_ticket_allows_only_its_own_pod():
    r = _FakeRedis()
    t = pt.mint(r, 42)
    assert pt.ticket_allows(r, t, 42) is True
    assert pt.ticket_allows(r, t, "42") is True, "ids arrive untyped off the socket"
    assert pt.ticket_allows(r, t, 43) is False
    assert pt.ticket_allows(r, t, "42abc") is False
    assert pt.ticket_allows(r, t, None) is False


def test_unknown_missing_or_malformed_ticket_is_denied():
    r = _FakeRedis()
    pt.mint(r, 42)
    assert pt.ticket_allows(r, "not-a-ticket", 42) is False
    assert pt.ticket_allows(r, None, 42) is False
    assert pt.ticket_allows(r, "", 42) is False
    assert pt.ticket_allows(r, 12345, 42) is False
    assert pt.ticket_allows(r, "x" * 129, 42) is False


def test_ticket_is_reusable_until_it_expires():
    r = _FakeRedis()
    t = pt.mint(r, 7)
    for _ in range(3):  # the trigger UI opens several sockets per page
        assert pt.ticket_allows(r, t, 7) is True
    r.now += pt.TICKET_TTL
    assert pt.ticket_allows(r, t, 7) is False


def test_redis_failure_fails_closed():
    r = _FakeRedis()
    t = pt.mint(r, 7)
    r.fail = True
    assert pt.ticket_allows(r, t, 7) is False


def test_bytes_value_from_a_non_decoding_client_still_matches():
    r = _FakeRedis()
    t = pt.mint(r, 7)
    r.store[pt.make_key(t)] = (b"7", None)
    assert pt.ticket_allows(r, t, 7) is True


def test_tickets_are_unique_and_url_safe():
    r = _FakeRedis()
    tickets = {pt.mint(r, 1) for _ in range(50)}
    assert len(tickets) == 50
    for t in tickets:
        assert all(c.isalnum() or c in "-_" for c in t)


# --- service wiring (AST contract) -----------------------------------------

def _read(*parts):
    with open(os.path.join(SRC, *parts)) as f:
        return f.read()


def _ticketed_vs_pod_naming(source):
    """(declared _TICKETED_TYPES, message types whose process_json handler
    reads 'sessiondeviceid')."""
    tree = ast.parse(source)
    declared = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_TICKETED_TYPES" for t in node.targets):
            declared = {c.value for c in ast.walk(node.value)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    naming = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "process_json"):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
                continue
            left, comps = node.test.left, node.test.comparators
            if not (isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name)
                    and left.value.id == "data" and isinstance(left.slice, ast.Constant)
                    and left.slice.value == "type" and len(comps) == 1
                    and isinstance(comps[0], ast.Constant)):
                continue
            body_consts = {c.value for stmt in node.body for c in ast.walk(stmt)
                           if isinstance(c, ast.Constant)}
            if "sessiondeviceid" in body_consts:
                naming.add(comps[0].value)
    return declared, naming


SERVERS = [("audio_processing", "server_posthoc.py"),
           ("video_processing", "server_posthoc.py")]


def test_every_pod_naming_message_is_ticketed_in_both_services():
    for parts in SERVERS:
        declared, naming = _ticketed_vs_pod_naming(_read(*parts))
        name = "/".join(parts)
        assert declared, f"{name}: _TICKETED_TYPES missing"
        assert naming, f"{name}: no handler reads sessiondeviceid? test is stale"
        assert declared == naming, f"{name}: gated {declared} != pod-naming {naming}"


def test_services_gate_before_any_handler_and_refuse_with_the_agreed_error():
    for parts in SERVERS:
        s = _read(*parts)
        name = "/".join(parts)
        assert "from posthoc_ticket import ticket_allows" in s, name
        gate = "if data['type'] in _TICKETED_TYPES and not self._authorised(data):"
        assert gate in s, name
        # The gate must precede the first handler in process_json.
        body = s[s.index("def process_json"):]
        assert body.index(gate) < body.index("if data['type'] =="), name
        assert "'Not authorised for this pod.'" in s, name
        assert "ticket_allows(redis_client._redis(), data.get('ticket'), data.get('sessiondeviceid'))" in s, name


def test_services_listen_on_loopback_only():
    for parts in SERVERS:
        s = _read(*parts)
        line = next(l for l in s.splitlines() if "reactor.listenTCP(" in l)
        assert "interface='127.0.0.1'" in line, "/".join(parts)


# --- API side ----------------------------------------------------------------

def test_route_mints_after_session_write_and_pod_membership_checks():
    s = _read("server", "routes", "posthoc_ticket.py")
    assert "/api/v1/sessions/<int:session_id>/devices/<int:session_device_id>/posthoc_ticket" in s
    assert "@wrappers.verify_session_access" in s
    assert "authz.device_in_session(session_device_id, session_id)" in s
    assert "@limiter.limit(" in s
    assert "RedisPosthocTicket.mint(session_device_id)" in s
    assert "from routes.posthoc_ticket import api_routes as posthoc_ticket_api" in _read("server", "discussion_capture.py")
    assert "app.register_blueprint(posthoc_ticket_api)" in _read("server", "discussion_capture.py")


def test_redis_helper_mints_with_the_shared_module():
    s = _read("server", "redis_helper.py")
    assert "class RedisPosthocTicket" in s
    assert "_posthoc_ticket.mint(r, session_device_id)" in s
    assert "TTL = _posthoc_ticket.TICKET_TTL" in s


# --- queue: server-triggered runs must keep working --------------------------

class _NoStartThread(threading.Thread):
    def start(self):  # the worker would fire real service triggers
        pass


def _import_queue():
    sys.modules.setdefault("posthoc_state", types.SimpleNamespace(is_running=lambda d: False))
    real = threading.Thread
    threading.Thread = _NoStartThread
    try:
        import posthoc_queue
        return posthoc_queue
    finally:
        threading.Thread = real


def test_queue_trigger_messages_carry_a_ticket_for_the_pod(monkeypatch):
    q = _import_queue()
    assert q.AUDIO_WS.startswith("ws://127.0.0.1:") and q.VIDEO_WS.startswith("ws://127.0.0.1:")

    class _Session:
        creation_date = datetime.datetime(2026, 9, 30, 12, 0, 0)

        def json(self):
            return {"keywords": ["k"]}

    fake_app = types.SimpleNamespace(app=types.SimpleNamespace(app_context=contextlib.nullcontext))
    fake_db = types.SimpleNamespace(
        get_sessions=lambda id: _Session(),
        get_speakers=lambda session_device_id: [],
        get_pod_duration=lambda did: 12.0)
    minted = []

    class _Ticket:
        TTL = 900

        @staticmethod
        def mint(did):
            minted.append(did)
            return "TICKET-%s" % did

    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setitem(sys.modules, "database", fake_db)
    monkeypatch.setitem(sys.modules, "redis_helper", types.SimpleNamespace(RedisPosthocTicket=_Ticket))

    sent = []
    # Phase 2: the queue runs the legs in sequence through _run_leg and
    # mints a fresh ticket per leg (an audio leg can outlive the 15 min TTL).
    monkeypatch.setattr(q, "_run_leg",
                        lambda url, init, start, *a: sent.append((url, init, start)))
    monkeypatch.setattr(q.time, "sleep", lambda s: None)

    q._run_job({"session_id": 5, "device_id": 77, "models": None})

    assert minted == [77, 77]
    assert [s[0] for s in sent] == [q.AUDIO_WS, q.VIDEO_WS]
    for _url, init, _start in sent:
        assert init["sessiondeviceid"] == 77
        assert init["ticket"] == "TICKET-77", "queued runs must authenticate like the UI"
