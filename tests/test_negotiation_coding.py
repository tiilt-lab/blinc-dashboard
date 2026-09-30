"""Negotiation coding (src/server/negotiation_coding.py and its plumbing).

A Kellogg negotiation class (the "Viking" case, Pat's team vs Sandy's team)
wants every utterance of a pod's transcript coded on four dimensions by the
local llama-server, rolled up per team and per speaker, plus a de-escalation
timeline. Covered here:

  * the LLM contract (thinking disabled in the request AND /no_think in the
    prompt, temperature 0, codebook text, chunk/context boundaries, stable
    indices, retries);
  * parse and repair of the model's answer (prose, fences, truncation,
    unknown codes -> defaults counted as invalid_codes);
  * rollup and timeline on the 14-line synthetic exchange from the trial;
  * run_coding, the routes and persistence on the in-memory SQLite app stub
    (the pattern of test_api_phase1_ingest.py) with a fake LLM;
  * the post-hoc queue's "coding" leg (FakeRedis) and its idle rules;
  * the session flag, the end-of-session hook and the migration (source).
"""
import ast
import csv
import importlib.util
import io
import itertools
import json
import os
import re
import sys
import textwrap
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
SERVER = os.path.join(SRC, "server")
sys.path.insert(0, SERVER)
sys.path.insert(0, os.path.join(SRC, "common"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.modules.setdefault('posthoc_state', types.SimpleNamespace(is_running=lambda d: False))

import negotiation_coding as nc  # noqa: E402  (import-light: no Flask, no DB)
from fake_redis import FakeRedis  # noqa: E402
import redis_sync  # noqa: E402
import posthoc_queue as q  # noqa: E402

CODEBOOK = nc.load_codebook()
_seq = itertools.count(1)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _func_source(text, name):
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(text, node))
    raise AssertionError("%s not found" % name)


# The 14-line synthetic Viking exchange from the trial, one line every 15 s,
# with the codes a human would give (the fake LLM answers with these).
VIKING = [
    ("Pat", "You went on vacation and left Alex in charge, and now the supplier is unpaid.",
     dict(emotion="neutral", rip="none", frame="past_blame", listening=[])),
    ("Sandy", "That's ridiculous. You're the one who signed off on the change order.",
     dict(emotion="escalating", rip="none", frame="past_blame", listening=[])),
    ("Pat", "Don't you understand that I need the money by Friday?",
     dict(emotion="escalating", rip="interest", frame="none", listening=["closed_question"])),
    ("Sandy", "If you force me into bankruptcy, you may not recover the money.",
     dict(emotion="escalating", rip="power", frame="none", listening=[])),
    ("Pat", "I can call the loan whenever I want.",
     dict(emotion="escalating", rip="power", frame="none", listening=[])),
    ("Sandy", "Can we slow this down for a minute?",
     dict(emotion="defusing", rip="none", frame="none", listening=["closed_question"])),
    ("Pat", "Fine. Tell me what is bothering you most.",
     dict(emotion="defusing", rip="none", frame="none", listening=["open_question"])),
    ("Sandy", "I need to avoid bankruptcy and keep the supplier on board.",
     dict(emotion="neutral", rip="interest", frame="none", listening=[])),
    ("Pat", "So your main concern is getting enough cash quickly for the lakefront property—is that right?",
     dict(emotion="neutral", rip="interest", frame="none", listening=["paraphrase", "check_understanding"])),
    ("Sandy", "Yes. The contract says changes must be in writing, but we both let that slide.",
     dict(emotion="neutral", rip="right", frame="none", listening=[])),
    ("Pat", "What has to happen in the next 15 days?",
     dict(emotion="neutral", rip="none", frame="future_problem_solving", listening=["open_question", "ask_constraint"])),
    ("Sandy", "Which of these three issues matters most to you?",
     dict(emotion="neutral", rip="none", frame="none", listening=["ask_priority", "open_question"])),
    ("Pat", "How can we structure this so you can make the investment and I can pay the supplier?",
     dict(emotion="neutral", rip="interest", frame="future_problem_solving", listening=["open_question"])),
    ("Sandy", "I can see why you're frustrated. Let's write up a schedule.",
     dict(emotion="defusing", rip="none", frame="future_problem_solving", listening=["acknowledge"])),
]
TEAMS = {"Pat": "Pat", "Sandy": "Sandy"}


def _utterances(n=None):
    lines = VIKING if n is None else [VIKING[i % len(VIKING)] for i in range(n)]
    return [{"index": i, "transcript_id": 100 + i, "start_time": 15 * i, "length": 10,
             "speaker_tag": tag, "text": text} for i, (tag, text, _) in enumerate(lines)]


def _gold_codes():
    return [dict(u, **VIKING[u["index"]][2]) for u in _utterances()]


def _code_indices(messages):
    # The indices the prompt asks to code, read off its CODE section.
    user = messages[-1]["content"]
    code_part = user[user.index("CODE ("):]
    return [int(m) for m in re.findall(r"^\[(\d+)\]", code_part, re.M)]


def gold_llm(messages):
    """Answers with the fixture's codes for exactly the indices asked."""
    return json.dumps([dict(i=i, **VIKING[i % len(VIKING)][2]) for i in _code_indices(messages)])


# ------------------------------------------------------------ codebook

def test_codebook_is_viking_v2_with_the_four_dimensions_and_examples():
    assert CODEBOOK["version"] == "viking-v2"
    dims = CODEBOOK["dimensions"]
    assert list(dims) == ["emotion", "rip", "frame", "listening"]
    assert set(dims["emotion"]["codes"]) == {"escalating", "defusing", "neutral"}
    assert set(dims["rip"]["codes"]) == {"interest", "right", "power", "none"}
    assert set(dims["frame"]["codes"]) == {"past_blame", "future_problem_solving", "none"}
    assert set(dims["listening"]["codes"]) == {
        "open_question", "closed_question", "paraphrase", "summarize", "acknowledge",
        "check_understanding", "ask_why", "ask_priority", "ask_constraint", "interrupt"}
    assert dims["listening"]["multi"] is True and dims["listening"]["default"] == []
    assert (dims["emotion"]["default"], dims["rip"]["default"], dims["frame"]["default"]) == ("neutral", "none", "none")
    assert "That's ridiculous." in dims["emotion"]["codes"]["escalating"]["examples"]
    assert "I can call the loan." in dims["rip"]["codes"]["power"]["examples"]
    assert "What has to happen in the next 15 days?" in dims["frame"]["codes"]["future_problem_solving"]["examples"]
    assert "Don't you understand that I need the money?" in dims["listening"]["notes"]["weak"]


def test_codebook_carries_wangs_definitions_and_the_case_facts():
    facts = "\n".join(CODEBOOK["case_facts"])
    for fact in ("developer and landlord", "carpentry", "$1.4M", "15 days", "$200K", "lakefront", "hotel",
                 "Fawn", "Alex", "based on past practice", "changes must be in writing", "first 10 minutes"):
        assert fact in facts
    rip = CODEBOOK["dimensions"]["rip"]
    assert "coerce and harm the other side's interests" in rip["codes"]["power"]["definition"]
    assert "raised voices, insults, table pounding" in rip["codes"]["power"]["definition"]
    assert "If you do not ____, then I will ____." in rip["codes"]["power"]["definition"]
    assert "Independent standards with perceived legitimacy or fairness: contracts, legal rights, precedent, norms" \
        in rip["codes"]["right"]["definition"]
    assert "Needs, desires, concerns, motivations, priorities: WHY someone wants what they want" in rip["codes"]["interest"]["definition"]
    emotion = CODEBOOK["dimensions"]["emotion"]
    for irritator in ("telling the other side what to do", "'unfair', 'unreasonable'", "intentional violations", "blaming",
                      "large demands", "without a counterpoint", "interrupting", "raised intensity"):
        assert irritator in emotion["codes"]["escalating"]["definition"], irritator
    defusing = emotion["codes"]["defusing"]
    assert "I can see that you're angry, and I'm sorry we're in this situation. Tell me what's on your mind." in defusing["examples"]
    assert "What do you want going forward?" in defusing["examples"]
    assert "What would help to put this behind us?" in defusing["examples"]
    assert "taking a break" in defusing["definition"]
    assert "'Calm down' is NOT defusing" in emotion["guidance"]
    listening = CODEBOOK["dimensions"]["listening"]
    for move in ("not interrupting", "'Tell me more'", "'Help me understand'", "paraphrasing", "summing up", "offering solutions"):
        assert move in listening["guidance"], move
    assert len(listening["conventions"]) == 4


# -------------------------------------------------------------- prompt

def test_prompt_carries_the_codebook_the_context_and_ends_with_no_think():
    us = _utterances()
    messages = nc.build_prompt(CODEBOOK, us[5:8], us[:5])
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    for code in ("escalating", "defusing", "interest", "right", "power", "past_blame",
                 "future_problem_solving", "check_understanding", "interrupt"):
        assert code in system
    assert "That's ridiculous." in system and "I can call the loan." in system
    assert "Which of these three issues matters most to you?" in system
    assert "JSON array" in system and '"i"' in system
    assert system.index("CASE FACTS") < system.index("$1.4M") < system.index("emotion (exactly one code)")
    assert "- Fawn" not in system and "Fawn (some materials call this person Alex" in system
    assert "* Every question gets its form label" in system and "'Calm down' is NOT defusing" in system
    assert "(examples of strong listening moves, not codes:" in system
    assert user.rstrip().endswith("/no_think")
    assert user.index("CONTEXT") < user.index("CODE (")
    context_part = user[user.index("CONTEXT"):user.index("CODE (")]
    assert [int(m) for m in re.findall(r"^\[(\d+)\]", context_part, re.M)] == [0, 1, 2, 3, 4]
    assert _code_indices(messages) == [5, 6, 7]
    assert "[5] Sandy: Can we slow this down for a minute?" in user


def test_first_chunk_has_no_context_section():
    user = nc.build_prompt(CODEBOOK, _utterances()[:3], [])[1]["content"]
    assert "CONTEXT" not in user and user.startswith("CODE (")


def test_chunks_are_40_with_5_read_only_predecessors_and_global_indices():
    us = _utterances(100)
    parts = list(nc.chunks(us))
    assert [len(c) for _, c in parts] == [40, 40, 20]
    assert [len(ctx) for ctx, _ in parts] == [0, 5, 5]
    assert [u["index"] for u in parts[1][0]] == [35, 36, 37, 38, 39]
    assert parts[1][1][0]["index"] == 40 and parts[2][1][-1]["index"] == 99
    seen = []
    coded, invalid = nc.code_utterances(CODEBOOK, us, lambda m: seen.append(_code_indices(m)) or gold_llm(m))
    assert seen == [list(range(0, 40)), list(range(40, 80)), list(range(80, 100))]
    assert [c["index"] for c in coded] == list(range(100)) and invalid == 0
    assert coded[42]["transcript_id"] == 142 and coded[42]["emotion"] == VIKING[42 % 14][2]["emotion"]


class _Response:
    def __init__(self, content):
        self.body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_call_llm_request_disables_thinking_at_temperature_0(monkeypatch):
    calls = []

    def urlopen(req, timeout=None):
        calls.append((req, timeout))
        return _Response("[]")

    monkeypatch.setattr(nc.urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("NEGOTIATION_LLM_URL", raising=False)
    monkeypatch.delenv("NEGOTIATION_LLM_MODEL", raising=False)
    messages = nc.build_prompt(CODEBOOK, _utterances()[:2], [])
    assert nc.call_llm(messages) == "[]"
    req, timeout = calls[0]
    body = json.loads(req.data.decode())
    assert req.full_url == "http://127.0.0.1:8080/v1/chat/completions"
    assert req.get_method() == "POST" and req.get_header("Content-type") == "application/json"
    assert timeout == 300
    assert body["model"] == "qwen3.8-27b-uncensored"
    assert body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][-1]["content"].rstrip().endswith("/no_think")
    assert body["messages"] == messages


def test_call_llm_honours_the_env_overrides(monkeypatch):
    seen = {}
    monkeypatch.setattr(nc.urllib.request, "urlopen",
                        lambda req, timeout=None: seen.update(url=req.full_url, body=json.loads(req.data)) or _Response("x"))
    monkeypatch.setenv("NEGOTIATION_LLM_URL", "http://10.0.0.9:9999/v1/chat/completions")
    monkeypatch.setenv("NEGOTIATION_LLM_MODEL", "other-model")
    nc.call_llm([{"role": "user", "content": "hi /no_think"}])
    assert seen["url"] == "http://10.0.0.9:9999/v1/chat/completions" and seen["body"]["model"] == "other-model"


def test_call_llm_retries_twice_then_gives_up(monkeypatch):
    attempts = []
    monkeypatch.setattr(nc.time, "sleep", lambda s: attempts.append(("sleep", s)))

    def flaky(req, timeout=None):
        attempts.append("call")
        if attempts.count("call") < 3:
            raise nc.urllib.error.URLError("connection refused")
        return _Response("ok")

    monkeypatch.setattr(nc.urllib.request, "urlopen", flaky)
    assert nc.call_llm([]) == "ok"
    assert attempts == ["call", ("sleep", 2), "call", ("sleep", 4), "call"]

    attempts.clear()

    def down(req, timeout=None):
        attempts.append("call")
        raise OSError("timed out")

    monkeypatch.setattr(nc.urllib.request, "urlopen", down)
    with pytest.raises(RuntimeError, match="after 3 attempt"):
        nc.call_llm([])
    assert attempts.count("call") == 3


# ------------------------------------------------------ parse and repair

GOOD = [{"i": 0, "emotion": "escalating", "rip": "power", "frame": "past_blame", "listening": ["interrupt"]},
        {"i": 1, "emotion": "defusing", "rip": "none", "frame": "none", "listening": []}]


def test_extract_array_takes_the_outermost_array_through_prose_and_fences():
    assert nc.extract_array(json.dumps(GOOD)) == GOOD
    wrapped = "Sure! Here are the codes [as requested]:\n```json\n%s\n```\nLet me know." % json.dumps(GOOD, indent=1)
    assert nc.extract_array(wrapped) == GOOD
    assert nc.extract_array("no json here") == []
    assert nc.extract_array("") == []


def test_truncated_answer_keeps_the_whole_objects_and_defaults_the_rest():
    text = json.dumps(GOOD)[:-25]          # cut inside the second object
    items = nc.extract_array(text)
    assert items == GOOD[:1]
    by_index, invalid = nc.validate_codes(items, CODEBOOK, [0, 1])
    assert by_index[0] == {"emotion": "escalating", "rip": "power", "frame": "past_blame", "listening": ["interrupt"]}
    assert by_index[1] == {"emotion": "neutral", "rip": "none", "frame": "none", "listening": []}
    assert invalid == 1                     # one skipped utterance


def test_unknown_codes_are_coerced_and_counted():
    items = [{"i": 0, "emotion": "Angry", "rip": "leverage", "frame": "blame",
              "listening": ["nod", "Open Question", "open_question", 7]}]
    by_index, invalid = nc.validate_codes(items, CODEBOOK, [0])
    assert by_index[0] == {"emotion": "neutral", "rip": "none", "frame": "none", "listening": ["open_question"]}
    assert invalid == 5                     # angry, leverage, blame, nod, 7
    # a bare string for the multi dimension is tolerated; a number is not
    assert nc.validate_codes([{"i": 0, "emotion": "Defusing", "rip": "Interest", "frame": "Past-Blame",
                               "listening": "acknowledge"}], CODEBOOK, [0]) == \
        ({0: {"emotion": "defusing", "rip": "interest", "frame": "past_blame", "listening": ["acknowledge"]}}, 0)
    assert nc.validate_codes([{"i": 0, "emotion": "neutral", "rip": "none", "frame": "none", "listening": 3}],
                             CODEBOOK, [0])[1] == 1


def test_index_key_variants_and_foreign_indices():
    items = [{"index": "1", "emotion": "defusing", "rip": "none", "frame": "none", "listening": []},
             {"i": 9, "emotion": "escalating", "rip": "none", "frame": "none", "listening": []},
             "garbage", {"i": "x"}]
    by_index, invalid = nc.validate_codes(items, CODEBOOK, [0, 1])
    assert by_index[1]["emotion"] == "defusing" and by_index[0]["emotion"] == "neutral"
    assert invalid == 1 and 9 not in by_index


def test_code_utterances_fails_loudly_on_an_unparseable_chunk():
    with pytest.raises(RuntimeError, match="no parseable codes for utterances 0-13"):
        nc.code_utterances(CODEBOOK, _utterances(), lambda m: "I cannot do that.")


# --------------------------------------------------- rollup and timeline

def test_rollup_counts_every_code_per_team_and_speaker():
    r = nc.rollup(_gold_codes(), TEAMS, CODEBOOK)
    assert set(r["by_team"]) == {"Pat", "Sandy", "unassigned"}
    pat, sandy, none = r["by_team"]["Pat"], r["by_team"]["Sandy"], r["by_team"]["unassigned"]
    assert (pat["utterances"], sandy["utterances"], none["utterances"]) == (7, 7, 0)
    assert pat["emotion"] == {"escalating": 2, "defusing": 1, "neutral": 4}
    assert sandy["emotion"] == {"escalating": 2, "defusing": 2, "neutral": 3}
    assert pat["rip"] == {"interest": 3, "right": 0, "power": 1, "none": 3}
    assert sandy["rip"] == {"interest": 1, "right": 1, "power": 1, "none": 4}
    assert pat["frame"] == {"past_blame": 1, "future_problem_solving": 2, "none": 4}
    assert pat["listening"]["open_question"] == 3 and pat["listening"]["check_understanding"] == 1
    assert sandy["listening"]["ask_priority"] == 1 and sandy["listening"]["summarize"] == 0
    assert set(pat["listening"]) == set(CODEBOOK["dimensions"]["listening"]["codes"])
    assert r["by_speaker"]["Pat"]["team"] == "Pat" and r["by_speaker"]["Sandy"]["team"] == "Sandy"
    assert r["by_speaker"]["Pat"]["emotion"] == pat["emotion"]


def test_unassigned_speakers_roll_into_unassigned():
    r = nc.rollup(_gold_codes(), {"Pat": "Pat"}, CODEBOOK)
    assert r["by_team"]["Sandy"]["utterances"] == 0
    assert r["by_team"]["unassigned"]["utterances"] == 7
    assert r["by_speaker"]["Sandy"]["team"] == "unassigned"
    assert nc.rollup([], None, CODEBOOK)["by_team"]["Pat"]["utterances"] == 0


def test_timeline_landmarks_on_the_fixture():
    t = nc.timeline(_gold_codes())
    assert t == {"first_escalating": 15, "peak_escalation_window_start": 15, "peak_escalation_count": 4,
                 "first_sustained_deescalation": 75, "first_future_move": 150}
    calm = [dict(c, emotion="neutral") for c in _gold_codes()]
    assert nc.timeline(calm)["first_escalating"] is None
    assert nc.timeline(calm)["first_sustained_deescalation"] is None
    assert nc.timeline(calm)["peak_escalation_count"] == 0
    # escalation recurring inside the window pushes the calm start past it;
    # a flare-up long after the first calm window does not move it
    recur = [dict(c, emotion="escalating" if c["index"] in (1, 7) else "neutral") for c in _gold_codes()]
    assert nc.timeline(recur)["first_sustained_deescalation"] == 120
    late = [dict(c, emotion="escalating" if c["index"] in (1, 12) else "neutral") for c in _gold_codes()]
    assert nc.timeline(late)["first_sustained_deescalation"] == 30
    assert nc.timeline(late)["peak_escalation_window_start"] == 15


def test_summarize_has_the_three_sections():
    s = nc.summarize(_gold_codes(), TEAMS, CODEBOOK)
    assert set(s) == {"by_team", "by_speaker", "timeline"}


def test_team_dynamics_answer_the_debrief_questions():
    dyn = nc.team_dynamics(_gold_codes(), TEAMS)
    assert dyn["Pat"] == {"opening_move": "interest", "rip_sequence": ["interest", "power", "interest"],
                          "last_escalating": 60, "first_interest_question": 30}
    # Sandy's first three minutes: one power, one interest, one right -> the one reached for first
    assert dyn["Sandy"] == {"opening_move": "power", "rip_sequence": ["power", "interest", "right"],
                            "last_escalating": 45, "first_interest_question": None}
    assert dyn["unassigned"] == {"opening_move": "none", "rip_sequence": [], "last_escalating": None,
                                 "first_interest_question": None}
    assert nc.team_dynamics([], TEAMS)["Pat"]["opening_move"] == "none"
    s = nc.summarize(_gold_codes(), TEAMS, CODEBOOK)
    assert s["by_team"]["Sandy"]["opening_move"] == "power" and s["by_team"]["Pat"]["last_escalating"] == 60
    assert "opening_move" not in s["by_speaker"]["Pat"]


def test_validate_teams():
    assert nc.validate_teams(None) == ({}, None)
    assert nc.validate_teams({"A": "pat", "B": "SANDY", "C": None, "D": ""}) == ({"A": "Pat", "B": "Sandy"}, None)
    assert nc.validate_teams({"A": "Coach"})[0] is None
    assert nc.validate_teams(["Pat"])[0] is None
    assert nc.team_of("A", {"A": "Pat"}) == "Pat" and nc.team_of("B", {"A": "Pat"}) == "unassigned"


# ------------------------------------- run_coding, routes and persistence

flask = pytest.importorskip("flask")
flask_sqlalchemy = pytest.importorskip("flask_sqlalchemy")


class _Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


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

    flask_app = flask.Flask("negotiation")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app_stub = _stub_module(
        "app", app=flask_app, db=flask_sqlalchemy.SQLAlchemy(flask_app),
        limiter=types.SimpleNamespace(limit=lambda *a, **k: (lambda f: f)),
        socketio=types.SimpleNamespace(emit=_Recorder()))
    identity = lambda f: f  # noqa: E731
    _stub_module("wrappers", verify_login=lambda public=False: identity,
                 verify_session_access=identity, verify_session_read_access=identity)
    _stub_module("config")
    enqueue_coding = _Recorder(True)
    _stub_module("posthoc_queue", enqueue_coding=enqueue_coding)

    for name in ("database", "negotiation_coding"):
        sys.modules.pop(name, None)  # rebound to the stub app below
    import database  # noqa: E402
    import authz
    authz.configure(authz.Deps(get_device=lambda did: database.get_session_devices(id=did),
                               get_transcript=database.get_transcript,
                               session_access=lambda sid, user, write: True,
                               key_grants=lambda device: False))
    spec = importlib.util.spec_from_file_location(
        "negotiation_routes", os.path.join(SERVER, "routes", "negotiation_coding.py"))
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    engine = sys.modules["negotiation_coding"]

    ctx = flask_app.app_context()
    ctx.push()
    tables = [t for t in app_stub.db.metadata.sorted_tables
              if not any(type(c.type).__module__.startswith("sqlalchemy.dialects.mysql") for c in t.columns)]
    app_stub.db.metadata.create_all(app_stub.db.engine, tables=tables)
    yield types.SimpleNamespace(database=database, routes=routes, engine=engine, flask_app=flask_app,
                                db=app_stub.db, enqueue_coding=enqueue_coding)
    app_stub.db.session.remove()
    ctx.pop()
    for name, module in list(sys.modules.items()):
        if name not in before and _ours(module):
            del sys.modules[name]
    for name, module in before.items():
        if sys.modules.get(name) is not module and _ours(sys.modules.get(name, module)):
            sys.modules[name] = module


def _session(env):
    from tables.session import Session
    from tables.user import User
    n = next(_seq)
    user = User("nego-%d@example.org" % n, "user", password="Abcdef1!")
    env.db.session.add(user)
    env.db.session.flush()
    session = Session(user.id, "S%d" % n)
    env.db.session.add(session)
    env.db.session.commit()
    return session


def _pod(env, session, with_transcripts=True):
    from tables.session_device import SessionDevice
    device = SessionDevice(session.id, None, "Pod %d" % next(_seq))
    env.db.session.add(device)
    env.db.session.flush()
    device.create_key()
    env.db.session.commit()
    if with_transcripts:
        for i, (tag, text, _) in enumerate(VIKING):
            env.database.add_transcript(device.id, 15 * i, 10, text, False, 0, 0, 0, 0, 0, 0, -1, tag, -1)
    return device


@pytest.fixture
def pod(env):
    return _pod(env, _session(env))


def _call(env, view, method, json_body=None, **kwargs):
    with env.flask_app.test_request_context("/api/v1/x", method=method, json=json_body):
        return view(**kwargs)


def test_run_coding_persists_codes_and_summary(env, pod):
    from tables.negotiation_code import NegotiationCode
    calls = []
    run = env.engine.run_coding(pod.id, TEAMS, llm=lambda m: calls.append(m) or gold_llm(m))
    assert len(calls) == 1
    assert run.status == "done" and run.error is None
    assert run.started_at is not None and run.finished_at >= run.started_at
    assert (run.utterances_coded, run.invalid_codes) == (14, 0)
    assert run.model == "qwen3.8-27b-uncensored" and run.codebook_version == "viking-v2"
    rows = env.db.session.query(NegotiationCode).filter_by(run_id=run.id).all()
    assert len(rows) == 14
    transcripts = env.database.get_transcripts(session_device_id=pod.id)
    assert {r.transcript_id for r in rows} == {t.id for t in transcripts}
    summary = run.summary_dict()
    assert summary["by_team"]["Pat"]["utterances"] == 7
    assert summary["timeline"]["first_escalating"] == 15
    assert run.teams_dict() == TEAMS
    assert run.json()["created_at"].endswith(" UTC") and run.json()["status"] == "done"


def test_run_coding_marks_the_run_error_and_reraises(env, pod):
    def broken(messages):
        raise RuntimeError("llama-server down")

    with pytest.raises(RuntimeError, match="llama-server down"):
        env.engine.run_coding(pod.id, TEAMS, llm=broken)
    run = env.database.get_latest_negotiation_run(pod.id)
    assert run.status == "error" and "llama-server down" in run.error and run.finished_at is not None
    with pytest.raises(RuntimeError, match="does not exist"):
        env.engine.run_coding(pod.id, TEAMS, llm=gold_llm, run_id=999999)


def test_run_coding_reports_to_the_given_run_and_reads_its_latest_teams(env, pod):
    d = env.database
    run = d.create_negotiation_run(pod.id, "m", "viking-v2", {"Pat": "Pat"})
    d.add_negotiation_codes(run.id, [])  # nothing yet

    def llm(messages):
        # a PUT /teams while the model is busy: the summary must use it
        d.update_negotiation_run(run.id, teams=TEAMS)
        return gold_llm(messages)

    out = env.engine.run_coding(pod.id, llm=llm, run_id=run.id)
    assert out.id == run.id and out.status == "done"
    assert out.summary_dict()["by_team"]["Sandy"]["utterances"] == 7
    # a second attempt (coordinator restart re-queues the job) replaces, not doubles
    env.engine.run_coding(pod.id, llm=gold_llm, run_id=run.id)
    assert len(d.get_negotiation_codes(run.id)) == 14
    assert d.get_latest_negotiation_run(pod.id).id == run.id


def test_routes_post_get_put_export_and_session_list(env):
    d, r, session = env.database, env.routes, _session(env)
    pod = _pod(env, session)
    empty = _pod(env, session, with_transcripts=False)
    stranger = _pod(env, _session(env))
    ids = dict(session_id=session.id, session_device_id=pod.id)

    assert _call(env, r.get_negotiation_coding, "GET", **ids).status_code == 404
    assert _call(env, r.start_negotiation_coding, "POST", {}, session_id=session.id,
                 session_device_id=stranger.id).status_code == 404
    assert _call(env, r.start_negotiation_coding, "POST", {"teams": {"Pat": "Coach"}}, **ids).status_code == 400

    env.enqueue_coding.calls.clear()
    resp = _call(env, r.start_negotiation_coding, "POST", {"teams": {"Pat": "Pat"}}, **ids)
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "queued" and isinstance(body["run_id"], int)
    assert env.enqueue_coding.calls == [((session.id, pod.id, body["run_id"]), {})]
    # queued again with new teams: the same run, teams updated, no second row
    resp2 = _call(env, r.start_negotiation_coding, "POST", {"teams": TEAMS}, **ids)
    assert resp2.get_json()["run_id"] == body["run_id"]
    assert d.get_latest_negotiation_run(pod.id).teams_dict() == TEAMS
    assert len(d.get_negotiation_runs(session.id)) == 1
    assert len(env.enqueue_coding.calls) == 2

    # GET while queued: the run, no codes, no summary
    payload = _call(env, r.get_negotiation_coding, "GET", **ids).get_json()
    assert payload["run"]["status"] == "queued" and payload["codes"] == [] and payload["summary"] is None

    # the coding leg runs
    env.engine.run_coding(pod.id, llm=gold_llm, run_id=body["run_id"])
    payload = _call(env, r.get_negotiation_coding, "GET", **ids).get_json()
    assert set(payload) == {"run", "teams", "codes", "summary"}
    assert set(payload["run"]) == {"id", "session_device_id", "status", "model", "codebook_version", "created_at",
                                   "started_at", "finished_at", "error", "utterances_coded", "invalid_codes"}
    assert payload["run"]["status"] == "done" and payload["teams"] == TEAMS
    assert len(payload["codes"]) == 14
    first = payload["codes"][0]
    assert set(first) == {"transcript_id", "start_time", "length", "speaker_tag", "text",
                          "emotion", "rip", "frame", "listening"}
    assert (first["start_time"], first["speaker_tag"], first["frame"]) == (0, "Pat", "past_blame")
    assert payload["codes"][8]["listening"] == ["paraphrase", "check_understanding"]
    assert set(payload["summary"]) == {"by_team", "by_speaker", "timeline"}
    assert payload["summary"]["timeline"]["first_future_move"] == 150

    # PUT teams: recomputed from the stored codes, no LLM
    resp = _call(env, r.set_negotiation_teams, "PUT", {"teams": {"Pat": "Sandy", "Sandy": "Pat"}}, **ids)
    assert resp.status_code == 200
    swapped = resp.get_json()
    assert swapped["teams"] == {"Pat": "Sandy", "Sandy": "Pat"}
    assert swapped["summary"]["by_team"]["Sandy"]["frame"]["past_blame"] == 1
    assert swapped["summary"]["by_speaker"]["Pat"]["team"] == "Sandy"
    assert swapped["run"]["status"] == "done" and len(swapped["codes"]) == 14
    assert _call(env, r.set_negotiation_teams, "PUT", {"teams": "x"}, **ids).status_code == 400
    assert _call(env, r.set_negotiation_teams, "PUT", {}, session_id=session.id,
                 session_device_id=empty.id).status_code == 404

    # CSV export
    resp = _call(env, r.export_negotiation_coding, "GET", **ids)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "text/csv; charset=utf-8"
    assert resp.headers["Content-Disposition"] == "attachment; filename=negotiation_coding_session%d_pod%d.csv" % (session.id, pod.id)
    rows = list(csv.reader(io.StringIO(resp.data.decode("utf-8"))))
    assert rows[0] == ["transcript_id", "start_time", "length", "speaker_tag", "team", "text",
                       "emotion", "rip", "frame", "listening"]
    assert len(rows) == 15
    assert rows[9][1:] == ["120", "10", "Pat", "Sandy", VIKING[8][1],
                           "neutral", "interest", "none", "paraphrase;check_understanding"]
    assert int(rows[9][0]) == payload["codes"][8]["transcript_id"]

    # per-session overview: every pod, no codes
    resp = _call(env, r.list_negotiation_coding, "GET", session_id=session.id)
    overview = {e["device_id"]: e for e in resp.get_json()}
    assert set(overview) == {pod.id, empty.id}
    assert set(overview[pod.id]) == {"device_id", "device_name", "run", "teams", "summary"}
    assert overview[pod.id]["run"]["status"] == "done" and overview[pod.id]["summary"]["timeline"]["first_escalating"] == 15
    assert overview[empty.id]["run"] is None and overview[empty.id]["summary"] is None
    assert _call(env, r.export_negotiation_coding, "GET", session_id=session.id,
                 session_device_id=empty.id).status_code == 404


def test_cancel_run_marks_only_pending_runs(env, pod):
    d = env.database
    queued = d.create_negotiation_run(pod.id, "m", "viking-v2")
    env.engine.cancel_run(queued.id, "queue cleared")
    assert d.get_negotiation_run(queued.id).status == "error"
    assert d.get_negotiation_run(queued.id).error == "cancelled: queue cleared"
    done = env.engine.run_coding(pod.id, TEAMS, llm=gold_llm)
    env.engine.cancel_run(done.id, "queue cleared")
    assert d.get_negotiation_run(done.id).status == "done"
    assert d.session_device_transcript_count(pod.id) == 14


def test_queue_coding_run_reuses_a_queued_run_else_creates_one(env, pod):
    d = env.database
    env.enqueue_coding.calls.clear()
    a = env.engine.queue_coding_run(pod.session_id, pod.id, {"Pat": "Pat"})
    b = env.engine.queue_coding_run(pod.session_id, pod.id, None)
    assert a.id == b.id and b.teams_dict() == {"Pat": "Pat"} and b.status == "queued"
    d.update_negotiation_run(a.id, status="running")
    c = env.engine.queue_coding_run(pod.session_id, pod.id)
    assert c.id != a.id and c.status == "queued" and c.codebook_version == "viking-v2"
    assert [call[0][2] for call in env.enqueue_coding.calls] == [a.id, a.id, c.id]


# ------------------------------------------------ the queue's coding leg

@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(redis_sync, "_client", r)
    monkeypatch.delenv("NEGOTIATION_CODING_WAIT_FOR_IDLE", raising=False)
    return r


class _AppStub:
    class app:
        @staticmethod
        def app_context():
            import contextlib
            return contextlib.nullcontext()


def _stub_engine(monkeypatch, **attrs):
    monkeypatch.setitem(sys.modules, "app", _AppStub)
    engine = types.SimpleNamespace(**attrs)
    monkeypatch.setitem(sys.modules, "negotiation_coding", engine)
    return engine


def test_enqueue_coding_carries_the_run_id_and_queues_beside_analysis_jobs(fake):
    assert q.enqueue_coding(1, 5, 77) is True
    assert q.enqueue_coding(1, 5, 77) is False            # the same run, once
    assert q.enqueue(1, [5]) == [5]                        # the pod's GPU job still queues
    assert q.enqueue_coding(1, 5, 78) is True
    assert q.enqueue(1, [5]) == []                         # but only one GPU job per pod
    with pytest.raises(ValueError, match="enqueue_coding"):
        q.enqueue(1, [6], legs=("coding",))
    jobs = q._all_jobs()
    assert [(j["device_id"], j.get("legs"), j.get("run_id")) for j in jobs] == \
        [(5, ["coding"], 77), (5, None, None), (5, ["coding"], 78)]
    assert q.status(1)[0] == {"session_id": 1, "device_id": 5, "state": "queued",
                              "error": None, "started_at": None, "finished_at": None}
    assert q._legs_extra(("coding",)) == {"legs": ["coding"]}
    assert q._legs_extra(("coding", "audio")) == {"legs": ["audio", "coding"]}


def test_coding_leg_runs_in_process_without_socket_ticket_or_base(fake, monkeypatch):
    ran = _Recorder()
    _stub_engine(monkeypatch, run_coding=ran)
    for name in ("_run_leg", "_mint", "_job_base", "_cancel"):
        monkeypatch.setattr(q, name, lambda *a, **k: pytest.fail("%s must not run for a coding leg" % name))
    job = {"session_id": 1, "device_id": 5, "state": "running", "legs": ["coding"], "run_id": 77}
    q._run_job(job)
    assert ran.calls == [((5,), {"run_id": 77})]
    assert job["done_legs"] == ["coding"]
    # a failure in the engine fails the job (the run row was marked error by run_coding)
    def boom(*a, **k):
        raise RuntimeError("LLM refused")
    _stub_engine(monkeypatch, run_coding=boom)
    with pytest.raises(RuntimeError, match="LLM refused"):
        q._run_job({"session_id": 1, "device_id": 5, "state": "running", "legs": ["coding"], "run_id": 78})


def test_needs_idle_only_for_gpu_legs_unless_the_env_says_otherwise(fake, monkeypatch):
    assert q._needs_idle({"legs": ["audio", "video"]}) is True
    assert q._needs_idle({}) is True
    assert q._needs_idle({"legs": ["audio", "coding"]}) is True
    assert q._needs_idle({"legs": ["coding"]}) is False
    monkeypatch.setenv("NEGOTIATION_CODING_WAIT_FOR_IDLE", "1")
    assert q._needs_idle({"legs": ["coding"]}) is True
    assert q._job_legs({}) == ["audio", "video"]           # a legacy job never gains the coding leg


class _Stop(Exception):
    pass


def _driven_loop(monkeypatch, live):
    # The runner on the test thread: a live class that never ends; the
    # first idle sleep stops the loop.
    monkeypatch.setattr(q, "_live_pods", lambda: live)
    monkeypatch.setattr(q, "_maybe_recycle_audio_service", lambda: pytest.fail("no recycle for coding jobs"))
    monkeypatch.setattr(q, "_job_base", lambda job: pytest.fail("no trigger base while the class is on"))

    def stop(s):
        raise _Stop()
    monkeypatch.setattr(q.time, "sleep", stop)
    ran = _Recorder()
    _stub_engine(monkeypatch, run_coding=ran)
    return ran


def test_coding_jobs_run_beside_a_live_class_while_gpu_jobs_hold(fake, monkeypatch, caplog):
    ran = _driven_loop(monkeypatch, live=2)
    q.enqueue(1, [10])                 # the GPU job at the head must hold
    q.enqueue_coding(1, 11, 5)
    q.enqueue_coding(1, 12, 6)
    with pytest.raises(_Stop):
        q._worker_loop()
    assert ran.calls == [((11,), {"run_id": 5}), ((12,), {"run_id": 6})]
    states = {(j["device_id"], j.get("run_id")): j["state"] for j in q._all_jobs()}
    assert states == {(10, None): "queued", (11, 5): "done", (12, 6): "done"}
    done = [j for j in q._all_jobs() if j["state"] == "done"]
    assert all(j["finished_at"] and j["started_at"] and j["error"] is None for j in done)


def test_wait_for_idle_env_holds_coding_jobs_like_the_rest(fake, monkeypatch):
    ran = _driven_loop(monkeypatch, live=1)
    monkeypatch.setenv("NEGOTIATION_CODING_WAIT_FOR_IDLE", "1")
    q.enqueue_coding(1, 11, 5)
    with pytest.raises(_Stop):
        q._worker_loop()
    assert ran.calls == [] and q._all_jobs()[0]["state"] == "queued"


def test_coding_job_needs_no_class_check_when_nothing_is_live(fake, monkeypatch):
    ran = _driven_loop(monkeypatch, live=0)
    q.enqueue_coding(1, 11, 5)
    q._worker_loop()
    assert ran.calls == [((11,), {"run_id": 5})] and q._all_jobs()[0]["state"] == "done"


def test_clear_pending_marks_dropped_coding_runs_cancelled(fake, monkeypatch):
    cancelled = _Recorder()
    _stub_engine(monkeypatch, cancel_run=cancelled)
    q.enqueue_coding(1, 11, 5)
    q.enqueue(1, [10])
    assert q.clear_pending() == 2
    assert cancelled.calls == [((5, "queue cleared"), {})]
    assert q._all_jobs() == []


def test_queue_leg_source_contracts():
    s = _read("src", "server", "posthoc_queue.py")
    assert 'LEGS = ("audio", "video", "coding")' in s and 'DEFAULT_LEGS = ("audio", "video")' in s
    leg = _func_source(s, "_run_coding_leg")
    for forbidden in ("_mint(", "websockets", "gpu_lease", "_run_leg(", "RedisPosthocTicket"):
        assert forbidden not in leg
    assert "negotiation_coding.run_coding(job[\"device_id\"], run_id=job.get(\"run_id\"))" in leg
    loop = _func_source(s, "_worker_loop")
    assert "beside_class = _needs_idle(job) and not _wait_for_class_to_end()" in loop
    assert "if _needs_gpu(job):" in loop and "_maybe_recycle_audio_service()" in loop
    assert "and _needs_gpu(j)}" in _func_source(s, "enqueue")
    assert "NEGOTIATION_CODING_WAIT_FOR_IDLE" in _func_source(s, "_needs_idle")
    run_job = _func_source(s, "_run_job")
    assert run_job.index('if scope == "coding":') < run_job.index("base = _job_base(job)")


# ---------------------------------------- session flag, end hook, migration

HANDLER = ("src", "server", "handlers", "session_handler.py")


def test_create_session_route_reads_negotiation_coding_defaulting_off():
    create = _func_source(_read("src", "server", "routes", "session.py"), "create_session")
    assert "request.json.get('negotiationCoding', False)" in create
    assert "if not isinstance(negotiation_coding, bool):" in create
    assert "negotiation_coding_enabled=negotiation_coding" in create
    assert create.index("negotiationCoding") < create.index("session_handler.create_session(")


def test_handler_stores_the_flag_and_queues_coding_at_session_end():
    s = _read(*HANDLER)
    create = _func_source(s, "create_session")
    assert "negotiation_coding_enabled=False" in create.splitlines()[0]
    assert "session.negotiation_coding = bool(negotiation_coding_enabled)" in create
    end = _func_source(s, "end_session")
    assert "if session.negotiation_coding:" in end
    assert end.index("database.save_changes()") < end.index("queue_negotiation_coding(session, session_devices)") \
        < end.index("# Ping pod devices")
    queue = _func_source(s, "queue_negotiation_coding")
    assert "negotiation_coding.queue_coding_run(session.id, session_device.id)" in queue
    assert "database.session_device_transcript_count(session_device.id)" in queue


def test_queue_negotiation_coding_skips_empty_pods_and_is_best_effort():
    src = _read(*HANDLER)
    queued = []

    def queue_coding_run(session_id, device_id, teams=None):
        if device_id == 3:
            raise RuntimeError("redis down")
        queued.append(device_id)
        return types.SimpleNamespace(id=100 + device_id)

    ns = {"logging": __import__("logging"),
          "database": types.SimpleNamespace(session_device_transcript_count=lambda d: 0 if d == 2 else 5),
          "negotiation_coding": types.SimpleNamespace(queue_coding_run=queue_coding_run)}
    exec(_func_source(src, "queue_negotiation_coding"), ns)
    session = types.SimpleNamespace(id=9)
    pods = [types.SimpleNamespace(id=i) for i in (1, 2, 3, 4)]
    assert ns["queue_negotiation_coding"](session, pods) == [(1, 101), (4, 104)]
    assert queued == [1, 4]


def test_session_model_column_json_and_blueprint_registration():
    s = _read("src", "server", "tables", "session.py")
    assert "negotiation_coding = db.Column(db.Boolean, nullable=True)" in s
    assert "negotiation_coding=bool(self.negotiation_coding)" in s
    assert "self.negotiation_coding = bool(negotiation_coding)" in s
    dc = _read("src", "server", "discussion_capture.py")
    assert "from routes.negotiation_coding import api_routes as negotiation_coding_api" in dc
    assert "app.register_blueprint(negotiation_coding_api)" in dc


def test_routes_are_guarded_and_rate_limited():
    s = _read("src", "server", "routes", "negotiation_coding.py")
    tree = ast.parse(s)
    guards = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.endswith("negotiation_coding") or \
                isinstance(node, ast.FunctionDef) and node.name == "set_negotiation_teams":
            guards[node.name] = [ast.unparse(d) for d in node.decorator_list]
    assert "wrappers.verify_session_access" in guards["start_negotiation_coding"]
    assert "wrappers.verify_session_access" in guards["set_negotiation_teams"]
    for name in ("get_negotiation_coding", "export_negotiation_coding", "list_negotiation_coding"):
        assert "wrappers.verify_session_read_access" in guards[name]
    assert any("limiter.limit('30 per hour'" in d for d in guards["start_negotiation_coding"])
    assert all(any(d.startswith("wrappers.verify_login(") for d in ds) for ds in guards.values())
    assert s.count("missing = _pod_or_404(session_device_id, session_id)") == 4
    assert "authz.device_in_session(session_device_id, session_id)" in s


def test_models_cascade_from_pod_run_and_transcript():
    run = _read("src", "server", "tables", "negotiation_coding_run.py")
    assert "db.ForeignKey('session_device.id', ondelete='CASCADE')" in run
    assert "STATUSES = ('queued', 'running', 'done', 'error')" in run
    code = _read("src", "server", "tables", "negotiation_code.py")
    assert "db.ForeignKey('negotiation_coding_run.id', ondelete='CASCADE')" in code
    assert "db.ForeignKey('transcript.id', ondelete='CASCADE')" in code
    d = _read("src", "server", "database.py")
    for helper in ("create_negotiation_run", "get_negotiation_run", "get_latest_negotiation_run",
                   "get_negotiation_runs", "update_negotiation_run", "add_negotiation_codes",
                   "get_negotiation_codes", "session_device_transcript_count"):
        assert "def %s(" % helper in d


def test_migration_chains_after_live_video_and_is_guarded_both_ways():
    versions = os.path.join(ROOT, "src", "server", "migrations", "versions")
    fn = [f for f in os.listdir(versions) if f.startswith("4c5d6e7f8091_")]
    assert len(fn) == 1, fn
    s = _read("src", "server", "migrations", "versions", fn[0])
    assert "revision = '4c5d6e7f8091'" in s and "down_revision = '3b4c5d6e7f80'" in s
    up = s[s.index("def upgrade():"):s.index("def downgrade():")]
    down = s[s.index("def downgrade():"):]
    assert "if not _has_column('session', 'negotiation_coding'):" in up
    assert "op.add_column('session', sa.Column('negotiation_coding', sa.Boolean(), nullable=True))" in up
    for table in ("negotiation_coding_run", "negotiation_code"):
        assert "if not _has_table('%s'):" % table in up
        assert "op.create_table(\n            '%s'," % table in up
        assert "if _has_table('%s'):" % table in down and "op.drop_table('%s')" % table in down
    assert "sa.ForeignKeyConstraint(['session_device_id'], ['session_device.id'], ondelete='CASCADE')" in up
    assert "sa.ForeignKeyConstraint(['run_id'], ['negotiation_coding_run.id'], ondelete='CASCADE')" in up
    assert "sa.ForeignKeyConstraint(['transcript_id'], ['transcript.id'], ondelete='CASCADE')" in up
    assert "op.drop_column('session', 'negotiation_coding')" in down
    assert down.index("negotiation_code'") < down.index("negotiation_coding_run'")   # child first
