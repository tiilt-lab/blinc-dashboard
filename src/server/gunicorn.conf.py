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

# DC_API_WORKERS interchangeable workers (default 3). Nothing live is
# process-local any more: the device ConnectionManager, the APScheduler job
# and the post-hoc runner run in the coordinator (coordinator.py); watchers,
# last-connect times, LLM slots, remux/synthesis locks and the image queues
# are in Redis; Socket.IO is websocket-only over a Redis message queue, so a
# session never has to land on the same worker twice.
workers = int(os.environ.get('DC_API_WORKERS', '3'))

# Plain OS threads, no monkey patching: the same process runs a Twisted
# reactor (device websocket server) and torch/ML code, which eventlet/gevent
# patching breaks. Matches async_mode='threading' in app.py; simple-websocket
# performs WebSocket upgrades under this worker via environ['gunicorn.socket'].
worker_class = 'gthread'

# One thread per in-flight request. Every Socket.IO WebSocket pins a thread
# for its whole life (simple-websocket runs the connection inside the WSGI
# call), so workers x threads must cover all connected browser tabs plus the
# real request concurrency: 3 x 64 = 192 concurrent, the single worker's 200
# of before spread over three processes (and three GILs).
threads = 64

# For gthread `timeout` is NOT a request timeout. The worker's main loop
# heartbeats (notify()) at least once a second on its own thread; the arbiter
# kills the worker (SIGABRT, then SIGKILL) only if that heartbeat stops for
# this many seconds, i.e. when the whole process is wedged (deadlock, GIL held
# by a C call, OOM thrash). Request threads running a 600–900 s LLM/ffmpeg
# call (nginx's proxy_read_timeout ceiling) are untouched. Killing a worker
# drops its websockets (clients reconnect to another), so the value is
# generous: a healthy worker is never killed, a truly dead one is replaced
# within 15 min.
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

# Never recycle workers (0 = off): each holds live socket.io connections and
# a recycle would drop them all at once for nothing.
max_requests = 0
max_requests_jitter = 0

# Shows up in ps/pgrep as the service, not as "gunicorn: worker [wsgi:app]".
proc_name = 'blinc-discussion-capture'

# --- Prometheus multiprocess bookkeeping (src/server/routes/metrics.py) ------
#
# The workers share one listening socket, so /metrics is served by whichever
# worker takes the scrape; prometheus_client's multiprocess mode keeps every
# worker's counters in mmap files under PROMETHEUS_MULTIPROC_DIR and the
# answering worker merges them. The directory must be private to this master
# (a second instance beside production uses another DC_PORT) and empty at
# start (pids get reused), and a dead worker's live gauges must be dropped.
# Both hooks run in the master; on_starting runs before the first fork, so
# the workers inherit the variable before they import prometheus_client.
# Set at hook time, not at import: this file is also exec'd by the tests.

def _multiproc_dir():
    return os.environ.get('PROMETHEUS_MULTIPROC_DIR') or os.path.join(
        '/dev/shm', 'blinc-prometheus-%s' % os.environ.get('DC_PORT', '5000'))


def on_starting(server):
    path = _multiproc_dir()
    os.environ['PROMETHEUS_MULTIPROC_DIR'] = path
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        try:
            os.remove(os.path.join(path, name))
        except OSError:
            pass


def child_exit(server, worker):
    try:
        # Loaded lazily by name: this module stays stdlib-only at import
        # (tests exec it without prometheus_client on the path).
        multiprocess = __import__('prometheus_client.multiprocess', fromlist=['mark_process_dead'])
        multiprocess.mark_process_dead(worker.pid, _multiproc_dir())
    except Exception as e:  # never let bookkeeping take the arbiter down
        server.log.warning('prometheus mark_process_dead(%s) failed: %s', worker.pid, e)
