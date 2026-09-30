"""Phase 1 / audit E.1: an API restart must not end video pods.

The 5 s auth sweep closes a pod when ProcessingConfig.is_valid_key() is
False. That used to be "the key lookup returned None", which covered
connection refused / timeouts / 5xx as well as a real revocation — so every
API restart (~6.5 s) closed every live pod. Now only an explicit 4xx is a
revocation; anything else keeps the pod for a 10-minute grace window.

callbacks.py imports requests (not installed in CI) and a project config at
module load; both are stubbed if missing, none of their functions are called.
"""
import logging
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_processing"))

try:
    import requests  # noqa: F401
except ImportError:  # CI: pytest+numpy only
    sys.modules["requests"] = types.ModuleType("requests")
sys.modules.setdefault("config", types.SimpleNamespace(initialize=lambda: None))

import callbacks  # noqa: E402
import processing_config  # noqa: E402
from processing_config import decide_key_check, KEY_CHECK_GRACE_SECONDS  # noqa: E402
from callbacks import classify_key_response  # noqa: E402


# ---- pure decision -----------------------------------------------------------

def test_valid_keeps_and_clears_the_clock():
    assert decide_key_check("valid", None, 100.0) == (True, None)
    assert decide_key_check("valid", 40.0, 100.0) == (True, None)


def test_revoked_closes_immediately():
    keep, since = decide_key_check("revoked", None, 100.0)
    assert keep is False
    keep, since = decide_key_check("revoked", 90.0, 100.0)
    assert keep is False


def test_unknown_keeps_the_pod_and_starts_the_clock():
    keep, since = decide_key_check("unknown", None, 100.0)
    assert keep is True and since == 100.0


def test_unknown_closes_only_after_ten_minutes_continuous():
    since = 100.0
    assert decide_key_check("unknown", since, 100.0 + 599.9)[0] is True
    keep, since2 = decide_key_check("unknown", since, 100.0 + KEY_CHECK_GRACE_SECONDS)
    assert keep is False and since2 == since
    assert KEY_CHECK_GRACE_SECONDS == 600


def test_recovery_resets_the_clock():
    # Unknown from t=0 ... valid at t=500 ... unknown again at t=700: the new
    # run of failures gets its own full grace window.
    keep, since = decide_key_check("unknown", None, 0.0)
    keep, since = decide_key_check("unknown", since, 500.0)
    assert keep is True
    keep, since = decide_key_check("valid", since, 550.0)
    assert (keep, since) == (True, None)
    keep, since = decide_key_check("unknown", since, 700.0)
    assert keep is True and since == 700.0
    assert decide_key_check("unknown", since, 700.0 + 599.0)[0] is True
    assert decide_key_check("unknown", since, 700.0 + 600.0)[0] is False


# ---- HTTP classification -----------------------------------------------------

def test_200_with_key_is_valid():
    assert classify_key_response(200, {"redis_key": "sess:abc"}) == ("valid", "sess:abc")


def test_explicit_4xx_is_revoked():
    for code in (400, 401, 403, 404):
        assert classify_key_response(code, {"message": "key cannot be authenticated"}) == ("revoked", None)


def test_5xx_and_odd_bodies_are_unknown():
    for code in (500, 502, 503, 504):
        assert classify_key_response(code, None) == ("unknown", None)
    # a 200 without a usable key is not an explicit revocation
    assert classify_key_response(200, {"redis_key": None}) == ("unknown", None)
    assert classify_key_response(200, "<html>") == ("unknown", None)


def test_connection_error_is_unknown(monkeypatch):
    def refused(*a, **k):
        raise OSError("Connection refused")
    monkeypatch.setattr(callbacks, "requests", types.SimpleNamespace(post=refused))
    monkeypatch.setattr(callbacks, "config",
                        types.SimpleNamespace(redis_session_key_callback=lambda: "http://api/key"))
    outcome, key, detail = callbacks.check_session_key("pod-1")
    assert (outcome, key) == ("unknown", None)
    assert "refused" in detail


# ---- is_valid_key() as the sweep sees it ---------------------------------------

def _config():
    return processing_config.ProcessingConfig(
        "pod-1", "sess-1", None, 0.0, 16000, "pcm_i16le", 1, None, 1, 1, False, True, "webm")


def _drive(monkeypatch, cfg, outcomes, times):
    results = []
    it = iter(zip(outcomes, times))
    state = {}

    def fake_check(auth_key):
        state["outcome"], state["t"] = next(it)
        return state["outcome"], None, "stub"

    monkeypatch.setattr(processing_config.callbacks, "check_session_key", fake_check)
    monkeypatch.setattr(processing_config.time, "monotonic", lambda: state["t"])
    for _ in outcomes:
        results.append(cfg.is_valid_key())
    return results


def test_api_restart_keeps_the_pod(monkeypatch):
    cfg = _config()
    # 5 s sweeps across a ~6.5 s restart, then the API is back.
    assert _drive(monkeypatch, cfg, ["valid", "unknown", "unknown", "valid"],
                  [0, 5, 10, 15]) == [True, True, True, True]
    assert cfg.key_unknown_since is None


def test_revocation_still_closes_at_once(monkeypatch):
    cfg = _config()
    assert _drive(monkeypatch, cfg, ["valid", "revoked"], [0, 5]) == [True, False]


def test_outage_closes_after_grace(monkeypatch):
    cfg = _config()
    outcomes = ["unknown"] * 4
    times = [0, 300, 599, 600]
    assert _drive(monkeypatch, cfg, outcomes, times) == [True, True, True, False]


def test_unknown_warning_is_rate_limited(monkeypatch, caplog):
    cfg = _config()
    caplog.set_level(logging.WARNING)
    _drive(monkeypatch, cfg, ["unknown"] * 14, [i * 5 for i in range(14)])  # 0..65 s
    warns = [r for r in caplog.records if "unverifiable" in r.getMessage()]
    assert len(warns) == 2  # once at t=0, once at t>=60
    assert all(r.levelno == logging.WARNING for r in warns)


def test_sweep_still_keeps_connections_when_the_check_raises():
    # The shared sweep is unchanged (audio behaviour identical): an exception
    # from is_valid_key keeps the connection; only False closes it.
    path = os.path.join(os.path.dirname(__file__), "..", "src", "common", "connection_manager_impl.py")
    with open(path) as f:
        s = f.read()
    assert "if not connection.config.is_valid_key():" in s
    assert "key validation errored; keeping connection" in s
