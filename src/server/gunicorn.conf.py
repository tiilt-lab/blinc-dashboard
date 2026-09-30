"""gunicorn settings for the BLINC API (docs/infra-audit-2026-09-30.md, A).

Loaded with ``-c src/server/gunicorn.conf.py`` from the unit's WorkingDirectory
(the repo root). Only the standard library is used here so the file also
imports cleanly in tests, without gunicorn or Flask.
"""
import os

# Import the app from src/server, as the dev runner does from the script's own
# directory. --chdir on the command line sets the same value; either alone is
# enough (gunicorn inserts this at sys.path[0] and os.chdir()s into it).
chdir = os.path.dirname(os.path.abspath(__file__))

# nginx is the only client (it proxies /api and /socket.io here), so bind to
# loopback only. The unit sets DC_PORT=5001; the dev default stays 5000.
bind = '127.0.0.1:%s' % os.environ.get('DC_PORT', '5000')

# Exactly one worker. The app keeps live state in process memory — the device
# ConnectionManager and its Twisted reactor, watchers._last_watched, the
# image_queue_dict and _remux_locks in routes/session.py, the APScheduler job —
# and a second worker would see none of it (and nginx has no ip_hash).
workers = 1

# Plain OS threads, no monkey patching: the same process runs a Twisted
# reactor (device websocket server) and torch/ML code, which eventlet/gevent
# patching breaks. Matches async_mode='threading' in app.py; simple-websocket
# performs WebSocket upgrades under this worker via environ['gunicorn.socket'].
worker_class = 'gthread'

# One thread per in-flight request. Every Socket.IO WebSocket pins a thread
# for its whole life (simple-websocket runs the connection inside the WSGI
# call) and every 25 s long-poll pins one until it returns, so this must cover
# all connected browser tabs plus the real request concurrency.
threads = 200

# For gthread `timeout` is NOT a request timeout. The worker's main loop
# heartbeats (notify()) at least once a second on its own thread; the arbiter
# kills the worker (SIGABRT, then SIGKILL) only if that heartbeat stops for
# this many seconds, i.e. when the whole process is wedged (deadlock, GIL held
# by a C call, OOM thrash). Request threads running a 600–900 s LLM/ffmpeg
# call (nginx's proxy_read_timeout ceiling) are untouched. Killing the single
# worker drops every websocket and all the in-process state above, so the
# value is generous: a healthy worker is never killed, a truly dead one is
# replaced within 15 min.
timeout = 900

# On SIGTERM the worker stops accepting and gives in-flight requests this
# long before exiting. WebSockets never "finish", so a restart with clients
# connected always takes the full 30 s; the unit's TimeoutStopSec stays above.
graceful_timeout = 30

# Idle keepalive connections wait in the poller, not on a thread.
keepalive = 5

# nginx already writes the access log; a second copy would only double the
# I/O. Errors go to stderr, which the unit hands to journald.
accesslog = None
errorlog = '-'
loglevel = 'info'
# Redirect the app's own stdout/stderr into errorlog. With errorlog='-' this
# is a no-op (gunicorn checks errorlog != '-') and journald captures both
# streams anyway; kept so a future file errorlog also gets app output.
capture_output = True

# Heartbeat file on tmpfs, so a stalled disk cannot look like a dead worker.
worker_tmp_dir = '/dev/shm'

# Never recycle the worker (0 = off): it holds the device connections, the
# live socket.io rooms, the image queues and the remux locks.
max_requests = 0
max_requests_jitter = 0

# Shows up in ps/pgrep as the service, not as "gunicorn: worker [wsgi:app]".
proc_name = 'blinc-discussion-capture'
