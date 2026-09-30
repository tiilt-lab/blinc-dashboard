"""Source contracts for the gunicorn entry point (infra audit, Phase 2 #13).

The API is served by gunicorn's gthread worker from src/server/wsgi.py, with
the start-up side effects factored into discussion_capture.create_app() behind
a once-per-process guard. These tests read the sources (and execute the pure-
stdlib gunicorn.conf.py) so they run in CI without Flask, gunicorn or a DB.
"""
import ast
import importlib.util
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "src", "server")
UNITS = os.path.join(REPO, "deploy", "instances")


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def _load_conf(monkeypatch, port=None):
    if port is None:
        monkeypatch.delenv("DC_PORT", raising=False)
    else:
        monkeypatch.setenv("DC_PORT", str(port))
    spec = importlib.util.spec_from_file_location(
        "blinc_gunicorn_conf", os.path.join(SERVER, "gunicorn.conf.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _top_level_calls(tree):
    # Names of functions/methods called as bare top-level statements.
    names = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            names.append(ast.unparse(node.value.func))
    return names


# --- wsgi.py ---------------------------------------------------------------

def test_wsgi_module_exposes_app_built_by_create_app():
    tree = ast.parse(_read("src", "server", "wsgi.py"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert any(n.module == "discussion_capture" and
               any(a.name == "create_app" for a in n.names) for n in imports), \
        "wsgi.py must import create_app from discussion_capture"
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)]
    app_assign = [n for n in assigns
                  if any(isinstance(t, ast.Name) and t.id == "app" for t in n.targets)]
    assert app_assign, "wsgi.py must define module-level `app`"
    assert ast.unparse(app_assign[0].value) == "create_app()"


# --- discussion_capture.py --------------------------------------------------

def test_create_app_runs_side_effects_once_per_process():
    src = _read("src", "server", "discussion_capture.py")
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "create_app" in funcs and "main" in funcs

    # Module-level guard: a Lock plus a boolean flag that create_app() sets.
    assert re.search(r"^_init_lock\s*=\s*threading\.Lock\(\)", src, re.M)
    assert re.search(r"^_initialised\s*=\s*False", src, re.M)
    body = ast.unparse(funcs["create_app"])
    assert "global _initialised" in body
    assert "with _init_lock" in body
    assert "if not _initialised" in body
    assert "_initialised = True" in body
    assert body.rstrip().endswith("return app")


def test_side_effects_are_not_module_level():
    tree = ast.parse(_read("src", "server", "discussion_capture.py"))
    calls = _top_level_calls(tree)
    for forbidden in ("app.register_blueprint", "authz.configure",
                      "scheduler.add_job", "device_websockets.run_server",
                      "prewarm_video_cache", "socketio.run"):
        assert not any(c.startswith(forbidden) for c in calls), \
            f"{forbidden} must run inside create_app()/main(), not at import"
    # Only imports, defs, assignments, docstring and the __main__ guard remain.
    for node in tree.body:
        if isinstance(node, ast.If):
            assert "__name__" in ast.unparse(node.test)
        else:
            assert isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                     ast.Assign, ast.Expr)), ast.dump(node)[:80]
            if isinstance(node, ast.Expr):
                assert isinstance(node.value, ast.Constant)  # the docstring


def test_all_blueprints_still_registered_inside_create_app():
    src = _read("src", "server", "discussion_capture.py")
    imported = set(re.findall(r"^from routes\.\w+ import api_routes as (\w+)$", src, re.M))
    registered = set(re.findall(r"app\.register_blueprint\((\w+)\)", src))
    assert imported and imported == registered
    assert "from routes import socket as _socket_handlers" in src


def test_dev_runner_is_unchanged():
    src = _read("src", "server", "discussion_capture.py")
    tree = ast.parse(src)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    body = ast.unparse(main)
    assert "create_app()" in body
    assert "socketio.run(app" in body
    assert "allow_unsafe_werkzeug=True" in body
    assert "use_reloader=False" in body
    assert "os.environ.get('DC_PORT', 5000)" in body
    assert re.search(r"if __name__ == '__main__':\s*\n\s*main\(\)", src)


def test_scheduler_and_boot_tasks_have_env_kill_switches():
    src = _read("src", "server", "discussion_capture.py")
    assert "DC_DISABLE_SCHEDULER" in src
    assert "DC_SKIP_BOOT_TASKS" in src
    # The scheduled job is guarded by the flag, not registered unconditionally.
    tree = ast.parse(src)
    start = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "_start_background_services")
    guarded = [n for n in ast.walk(start) if isinstance(n, ast.If)
               and "DC_DISABLE_SCHEDULER" in ast.unparse(n.test)]
    assert guarded and "scheduler.add_job" in ast.unparse(guarded[0])


# --- gunicorn.conf.py -------------------------------------------------------

def test_gunicorn_conf_is_multi_worker_gthread(monkeypatch):
    # Multi-worker step: 3 x 64 (tests/test_multiworker.py has the rest).
    monkeypatch.delenv("DC_API_WORKERS", raising=False)
    conf = _load_conf(monkeypatch, port=5123)
    assert conf.bind == "127.0.0.1:5123"
    assert conf.workers == 3
    assert conf.worker_class == "gthread"
    assert conf.threads == 64 and conf.workers * conf.threads >= 150   # websockets each pin one
    assert conf.timeout >= 900          # heartbeat, must exceed nginx's 600-900 s
    assert conf.graceful_timeout == 30
    assert conf.keepalive == 5
    assert conf.max_requests == 0       # live websockets must survive
    assert conf.max_requests_jitter == 0
    assert conf.worker_tmp_dir == "/dev/shm"
    assert conf.accesslog is None       # nginx has the access log
    assert conf.errorlog == "-"
    assert conf.capture_output is True
    assert os.path.samefile(conf.chdir, SERVER)


def test_gunicorn_conf_defaults_to_5000_and_never_binds_all_interfaces(monkeypatch):
    conf = _load_conf(monkeypatch, port=None)
    assert conf.bind == "127.0.0.1:5000"
    assert "0.0.0.0" not in _read("src", "server", "gunicorn.conf.py")


def test_gunicorn_conf_uses_only_the_stdlib():
    tree = ast.parse(_read("src", "server", "gunicorn.conf.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name == "os" for a in node.names)
        assert not isinstance(node, ast.ImportFrom)


# --- deployment ---------------------------------------------------------------

def test_gunicorn_is_pinned():
    assert re.search(r"^gunicorn==\d+\.\d+\.\d+$", _read("src", "requirements-unified.txt"), re.M)


def test_units_serve_with_gunicorn():
    units = [os.path.join(UNITS, d, "blinc-discussion-capture.service")
             for d in os.listdir(UNITS)]
    units = [u for u in units if os.path.exists(u)]
    assert units
    for unit in units:
        with open(unit) as f:
            s = f.read()
        execstart = re.search(r"^ExecStart=(.*)$", s, re.M).group(1)
        assert execstart.endswith(
            "/src/venv-unified/bin/gunicorn -c src/server/gunicorn.conf.py --chdir src/server wsgi:app"), execstart
        assert "discussion_capture.py" not in execstart
        wd = re.search(r"^WorkingDirectory=(.*)$", s, re.M).group(1).rstrip("/")
        assert execstart.startswith(wd + "/"), "gunicorn binary must live under WorkingDirectory"
        assert re.search(r'^Environment="?DC_PORT=\d+"?$', s, re.M), "gunicorn.conf.py binds from DC_PORT"
        assert re.search(r"^TimeoutStopSec=(\d+)$", s, re.M) and \
            int(re.search(r"^TimeoutStopSec=(\d+)$", s, re.M).group(1)) > 30  # > graceful_timeout
