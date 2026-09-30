"""Prometheus ``/metrics`` for the API (deploy/monitoring/README.md).

Served by whichever gunicorn worker takes the scrape, on the API port itself
(the workers share one socket, so a separate per-worker exporter port is not
possible). prometheus_client's multiprocess mode makes that answer complete:
each worker writes its counters/histograms/gauges to mmap files in
``PROMETHEUS_MULTIPROC_DIR`` (gunicorn.conf.py creates and wipes it in
``on_starting`` and drops a dead worker's live gauges in ``child_exit``) and
``blinc_metrics.render()`` merges every worker's files on each scrape.
Per-pid collectors were the alternative and do not work here: a scrape lands
on a random worker, so each pid's series would appear in one scrape and be
missing from the next, and Prometheus writes a staleness marker for every
series absent from a scrape, which breaks every rate().

What is measured, and where the data already exists:

* request count/latency by route family — before/after_app_request hooks,
  the route label is the matched blueprint rule (``/api/v1/sessions/<id>``),
  so ids never become label values;
* rate-limit hits — 429 responses (Flask-Limiter's breach status);
* Socket.IO clients and the SQLAlchemy pool — per-worker ``livesum`` gauges
  refreshed by a 5 s sampler, read from ``socketio.server.eio.sockets`` and
  ``db.engine.pool``;
* post-hoc queue depth, live pods, GPU lease, ``posthoc_failed:*`` keys —
  scrape-time callbacks over Redis (``register_shared_gauges``, which the
  coordinator reuses for its own exporter).

Loopback only (``verify_local``), like the callback routes: Prometheus
scrapes 127.0.0.1:<DC_PORT>/metrics directly, nginx never proxies it.
"""
import os
import sys
import time

from flask import Blueprint, Response, g, request

_COMMON = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'common'))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
import blinc_metrics as bm  # noqa: E402
import wrappers  # noqa: E402

api_routes = Blueprint('metrics', __name__)

SAMPLE_SECONDS = 5.0
QUEUE_STATES = ('queued', 'running', 'done', 'error')
GPU_LEASE_SLOTS = 1  # gpu_lease.acquire_for_run default

HTTP_REQUESTS = bm.counter(
    'http_requests_total', 'API requests by matched route rule, method and status',
    ('route', 'method', 'status'))
HTTP_LATENCY = bm.histogram(
    'http_request_duration_seconds', 'API request latency by matched route rule',
    ('route',), buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0))
RATE_LIMIT_HITS = bm.counter(
    'rate_limit_hits_total', 'Requests refused with 429 by route rule', ('route',))
SOCKETIO_CLIENTS = bm.gauge(
    'socketio_clients', 'Browser Socket.IO connections held by the live API workers',
    multiprocess_mode='livesum')
DB_POOL = bm.gauge(
    'db_pool_connections', 'SQLAlchemy pool connections by state, summed over the live workers',
    ('state',), multiprocess_mode='livesum')


def route_label():
    rule = request.url_rule
    return rule.rule if rule is not None else '(unmatched)'


@api_routes.before_app_request
def _start_timer():
    g._blinc_metrics_t0 = time.perf_counter()


@api_routes.after_app_request
def _observe(response):
    if request.path == '/metrics':
        return response
    route = route_label()
    status = str(response.status_code)
    HTTP_REQUESTS.labels(route, request.method, status).inc()
    t0 = getattr(g, '_blinc_metrics_t0', None)
    if t0 is not None:
        HTTP_LATENCY.labels(route).observe(time.perf_counter() - t0)
    if response.status_code == 429:
        RATE_LIMIT_HITS.labels(route).inc()
    return response


# --- per-worker samples ----------------------------------------------------

def socketio_client_count():
    from app import socketio
    return len(socketio.server.eio.sockets)


def db_pool_status():
    from app import app, db
    with app.app_context():
        pool = db.engine.pool
    return {
        'checked_out': pool.checkedout(),
        'checked_in': pool.checkedin(),
        'overflow': max(pool.overflow(), 0),
        'size': pool.size(),
    }


def _sample_worker():
    SOCKETIO_CLIENTS.set(socketio_client_count())
    for state, n in db_pool_status().items():
        DB_POOL.labels(state).set(n)


# --- shared, Redis-derived gauges (API workers and the coordinator) ----------

def queue_depth_by_state(status):
    counts = {(s,): 0 for s in QUEUE_STATES}
    for job in status():
        counts[(job.get('state') or 'unknown',)] = counts.get((job.get('state') or 'unknown',), 0) + 1
    return counts


def gpu_lease_slots(r, slots=GPU_LEASE_SLOTS):
    import gpu_lease
    held = {}
    holder = {}
    for n, owner in enumerate(gpu_lease.GpuLease(r, owner='metrics-probe', slots=slots).holders()):
        held[(str(n),)] = 1 if owner else 0
        if owner:
            holder[(str(n), str(owner).split(':', 1)[0])] = 1
    return held, holder


def posthoc_failed_keys(r):
    out = {}
    for key in r.scan_iter(match='posthoc_failed:*', count=100):
        if isinstance(key, bytes):
            key = key.decode('utf-8', 'replace')
        parts = key.split(':')
        if len(parts) >= 3:
            out[(parts[1], parts[2])] = 1
    return out


def register_shared_gauges(r=None, status=None, live_count=None, registry=None):
    """Scrape-time gauges over Redis; identical from any process, so the API
    workers and the coordinator export the same numbers."""
    if r is None:
        from redis_helper import r as _r
        r = _r
    if status is None:
        import posthoc_queue
        status = posthoc_queue.status
    if live_count is None:
        import live_presence
        live_count = live_presence.live_count
    bm.callback_gauge('live_pods', 'Pods streaming to the live audio server (live_pod:* keys)',
                      lambda: live_count(r), registry=registry)
    bm.callback_gauge('posthoc_queue_jobs', 'Post-hoc queue jobs by state',
                      lambda: queue_depth_by_state(status), labels=('state',), registry=registry)
    bm.callback_gauge('gpu_lease_held', '1 while a post-hoc run holds the GPU lease slot',
                      lambda: gpu_lease_slots(r)[0], labels=('slot',), registry=registry)
    bm.callback_gauge('gpu_lease_holder', '1 for the service holding the GPU lease slot',
                      lambda: gpu_lease_slots(r)[1], labels=('slot', 'service'), registry=registry)
    bm.callback_gauge('posthoc_failed', '1 per posthoc_failed:<device>:<scope> key (set by the '
                      'failure callback, 1 h TTL)',
                      lambda: posthoc_failed_keys(r), labels=('device_id', 'scope'), registry=registry)
    bm.callback_gauge('posthoc_failed_keys', 'Number of posthoc_failed:* keys',
                      lambda: len(posthoc_failed_keys(r)), registry=registry)


def install():
    """Wire the process gauges, the worker sampler and the shared gauges; runs
    once per process when the blueprint is registered (create_app), never at
    import. The service label follows the role: an API worker, the
    coordinator (which also registers the blueprints) or the dev runner."""
    role = os.environ.get('DC_ROLE') or 'api'
    bm.install_process_metrics(role)
    if role != 'coordinator':
        bm.start_sampler('api-worker', _sample_worker, SAMPLE_SECONDS, run_now=False)
    register_shared_gauges()


api_routes.record_once(lambda state: install())


@api_routes.route('/metrics', methods=['GET'])
@wrappers.verify_local
def metrics(**kwargs):
    return Response(bm.render(), mimetype=bm.content_type())
