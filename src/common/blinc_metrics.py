"""Tiny wrapper over prometheus_client for every BLINC service.

Every process gets the same shape for free:

* ``start_exporter(port)``   an HTTP ``/metrics`` listener on 127.0.0.1 only,
                             idempotent per port, a logged no-op when
                             prometheus_client is missing or the port is 0.
* ``install_process_metrics(service)``
                             ``blinc_process_{rss_bytes,threads,open_fds,
                             uptime_seconds,cpu_seconds_total}`` and
                             ``blinc_service_up`` labelled by service (and
                             pid), refreshed by a daemon sampler thread.
* ``counter/gauge/histogram/callback_gauge``
                             per-service metrics, names prefixed ``blinc_``,
                             created once per name (a second call returns the
                             same object), no-op stand-ins without the library.

Multiprocess (the gunicorn API): when ``PROMETHEUS_MULTIPROC_DIR`` is set the
library keeps counters/histograms/gauges in mmap files per worker and
``render()`` merges every worker's files, so any worker can answer a scrape
for all of them (gunicorn.conf.py owns the directory and the child_exit
bookkeeping). Callback gauges are computed at scrape time in the answering
process; they are for values that live outside the process (Redis) or are
identical in every worker.

The processing services scrape targets are documented in
deploy/monitoring/README.md; ``DEFAULT_PORTS`` is the one place the numbers
live and ``BLINC_METRICS_PORT`` overrides them per unit (0 disables).
"""
import logging
import os
import threading
import time

try:
    import prometheus_client as _pc
    from prometheus_client import CollectorRegistry, REGISTRY
    from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
except ImportError:  # pragma: no cover - exercised only where the venv lacks it
    _pc = None
    CollectorRegistry = REGISTRY = None
    CounterMetricFamily = GaugeMetricFamily = None

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

PREFIX = 'blinc_'
LOOPBACK = '127.0.0.1'
ENV_PORT = 'BLINC_METRICS_PORT'
MULTIPROC_ENV = 'PROMETHEUS_MULTIPROC_DIR'

# Standalone exporter ports, one per unit (the API serves /metrics on its own
# gunicorn port instead). deploy/monitoring/prometheus.yml scrapes these.
DEFAULT_PORTS = {
    'coordinator': 9110,
    'audio': 9111,
    'video': 9112,
    'audio_posthoc': 9113,
    'video_posthoc': 9114,
    'gpu': 9115,
}

PROCESS_SAMPLE_SECONDS = 10.0

_lock = threading.Lock()
_started = {}       # port -> (server, thread) | None (disabled)
_metrics = {}       # (registry id, full name) -> metric object
_callbacks = {}     # registry id -> _CallbackCollector
_samplers = {}      # name -> thread
_process_started = time.time()

if _pc is not None:
    # ``*_created`` twins double the series count for nothing we chart.
    try:
        _pc.disable_created_metrics()
    except AttributeError:  # pragma: no cover - older library
        pass


def available():
    """True when prometheus_client is importable (everything else is a no-op otherwise)."""
    return _pc is not None


def multiprocess_dir(environ=os.environ):
    """The gunicorn multiprocess directory, or None in a single-process service."""
    return environ.get(MULTIPROC_ENV) or None


def port_for(service, environ=os.environ):
    """Exporter port for ``service``: ``BLINC_METRICS_PORT`` if set, else the
    documented default. 0 (or a non-number) means "no exporter"."""
    raw = environ.get(ENV_PORT)
    if raw is None or not str(raw).strip():
        return DEFAULT_PORTS[service]
    try:
        return int(str(raw).strip())
    except ValueError:
        logging.warning('%s=%r is not a port; metrics exporter disabled', ENV_PORT, raw)
        return 0


def full_name(name):
    return name if name.startswith(PREFIX) else PREFIX + name


class _Noop:
    """Stands in for a metric when prometheus_client is missing."""

    def labels(self, *args, **kwargs):
        return self

    def inc(self, *args, **kwargs):
        return None

    def dec(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None

    def observe(self, *args, **kwargs):
        return None


NOOP = _Noop()


def _registry(registry):
    return registry if registry is not None else REGISTRY


def _get_or_create(kind, name, factory, registry):
    key = (id(_registry(registry)), full_name(name))
    with _lock:
        existing = _metrics.get(key)
        if existing is not None:
            return existing
        metric = factory()
        _metrics[key] = metric
        return metric


def counter(name, doc, labels=(), registry=None):
    """A monotonically increasing counter; name it ``..._total``."""
    if _pc is None:
        return NOOP
    return _get_or_create('counter', name, lambda: _pc.Counter(
        full_name(name), doc, list(labels), registry=_registry(registry)), registry)


def gauge(name, doc, labels=(), multiprocess_mode='livesum', registry=None):
    """A settable value. ``multiprocess_mode`` matters only under gunicorn:
    ``livesum`` adds the live workers' values (connection counts), ``liveall``
    keeps one series per live worker pid (process gauges), ``livemax`` etc."""
    if _pc is None:
        return NOOP
    return _get_or_create('gauge', name, lambda: _pc.Gauge(
        full_name(name), doc, list(labels), multiprocess_mode=multiprocess_mode,
        registry=_registry(registry)), registry)


def histogram(name, doc, labels=(), buckets=None, registry=None):
    if _pc is None:
        return NOOP
    kwargs = {}
    if buckets is not None:
        kwargs['buckets'] = tuple(buckets)
    return _get_or_create('histogram', name, lambda: _pc.Histogram(
        full_name(name), doc, list(labels), registry=_registry(registry), **kwargs), registry)


class _CallbackCollector:
    """One collector per registry holding every scrape-time callback.

    A callback returns a number (no labels) or a dict ``{label values tuple:
    number}``. A callback that raises is skipped for that scrape and logged
    at most once a minute, so a Redis blip never fails the whole scrape."""

    def __init__(self):
        self._entries = []
        self._entries_lock = threading.Lock()
        self._last_error = {}

    def add(self, name, doc, fn, labels, kind):
        with self._entries_lock:
            for entry in self._entries:
                if entry[0] == name:
                    return False
            self._entries.append((name, doc, fn, tuple(labels), kind))
        return True

    def names(self):
        with self._entries_lock:
            return [e[0] for e in self._entries]

    def describe(self):
        # Lets the registry register us without calling the callbacks.
        with self._entries_lock:
            entries = list(self._entries)
        for name, doc, _fn, labels, kind in entries:
            family = CounterMetricFamily if kind == 'counter' else GaugeMetricFamily
            yield family(name, doc, labels=list(labels))

    def collect(self):
        with self._entries_lock:
            entries = list(self._entries)
        for name, doc, fn, labels, kind in entries:
            try:
                value = fn()
            except Exception as e:
                now = time.time()
                if now - self._last_error.get(name, 0) > 60:
                    self._last_error[name] = now
                    logging.warning('metrics callback %s failed: %s', name, e)
                continue
            family = CounterMetricFamily if kind == 'counter' else GaugeMetricFamily
            metric = family(name, doc, labels=list(labels))
            if isinstance(value, dict):
                for label_values, v in value.items():
                    if not isinstance(label_values, (tuple, list)):
                        label_values = (label_values,)
                    metric.add_metric([str(x) for x in label_values], float(v))
            elif value is not None:
                metric.add_metric([], float(value))
            yield metric


def _callbacks_for(registry):
    reg = _registry(registry)
    with _lock:
        collector = _callbacks.get(id(reg))
        if collector is None:
            collector = _CallbackCollector()
            _callbacks[id(reg)] = collector
            reg.register(collector)
        return collector


def callback_gauge(name, doc, fn, labels=(), registry=None, kind='gauge'):
    """A gauge (or, with ``kind='counter'``, a counter) computed by ``fn`` at
    scrape time. Returns True when newly registered, False for a repeat."""
    if _pc is None:
        return False
    return _callbacks_for(registry).add(full_name(name), doc, fn, labels, kind)


def callback_counter(name, doc, fn, labels=(), registry=None):
    """Scrape-time counter for totals a module already keeps monotonic."""
    return callback_gauge(name, doc, fn, labels, registry=registry, kind='counter')


def start_sampler(name, fn, interval, run_now=True):
    """Call ``fn`` every ``interval`` s on a daemon thread (once per name)."""
    with _lock:
        if name in _samplers and _samplers[name].is_alive():
            return _samplers[name]

        def _loop():
            while True:
                time.sleep(interval)
                try:
                    fn()
                except Exception as e:
                    logging.debug('metrics sampler %s: %s', name, e)

        thread = threading.Thread(target=_loop, name='metrics-' + name, daemon=True)
        _samplers[name] = thread
    if run_now:
        try:
            fn()
        except Exception as e:
            logging.debug('metrics sampler %s: %s', name, e)
    thread.start()
    return thread


def _process_snapshot():
    """rss, threads, fds, cpu seconds for this process (psutil, else /proc)."""
    if psutil is not None:
        p = psutil.Process()
        with p.oneshot():
            cpu = p.cpu_times()
            return {
                'rss': p.memory_info().rss,
                'threads': p.num_threads(),
                'fds': p.num_fds() if hasattr(p, 'num_fds') else 0,
                'cpu': cpu.user + cpu.system,
            }
    rss = threads = 0
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith('Threads:'):
                    threads = int(line.split()[1])
    except OSError:
        pass
    try:
        fds = len(os.listdir('/proc/self/fd'))
    except OSError:
        fds = 0
    t = os.times()
    return {'rss': rss, 'threads': threads, 'fds': fds, 'cpu': t.user + t.system}


def install_process_metrics(service, interval=PROCESS_SAMPLE_SECONDS, registry=None):
    """The standard per-process gauges, refreshed every ``interval`` s.

    Under gunicorn the multiprocess collector adds the worker ``pid`` label
    itself (liveall mode, cleared by child_exit), so the label is only added
    here for single-process services."""
    if _pc is None:
        return None
    labels = ('service',) if multiprocess_dir() else ('service', 'pid')
    values = (service,) if multiprocess_dir() else (service, str(os.getpid()))
    kw = dict(labels=labels, multiprocess_mode='liveall', registry=registry)
    rss = gauge('process_rss_bytes', 'Resident set size of the service process', **kw)
    threads = gauge('process_threads', 'Threads in the service process', **kw)
    fds = gauge('process_open_fds', 'Open file descriptors in the service process', **kw)
    uptime = gauge('process_uptime_seconds', 'Seconds since the service process started', **kw)
    cpu = gauge('process_cpu_seconds_total', 'CPU seconds (user+system) consumed by the service process', **kw)
    up = gauge('service_up', '1 for every live service process', **kw)

    def _sample():
        snap = _process_snapshot()
        rss.labels(*values).set(snap['rss'])
        threads.labels(*values).set(snap['threads'])
        fds.labels(*values).set(snap['fds'])
        uptime.labels(*values).set(time.time() - _process_started)
        cpu.labels(*values).set(snap['cpu'])
        up.labels(*values).set(1)

    return start_sampler('process-' + service, _sample, interval)


def render(registry=None):
    """The exposition text for a scrape (bytes). In multiprocess mode the
    workers' files are merged and this process's callback gauges added."""
    if _pc is None:
        return b''
    path = multiprocess_dir()
    if path and registry is None:
        from prometheus_client import multiprocess
        merged = CollectorRegistry()
        multiprocess.MultiProcessCollector(merged, path=path)
        with _lock:
            collector = _callbacks.get(id(REGISTRY))
        if collector is not None:
            merged.register(collector)
        return _pc.generate_latest(merged)
    return _pc.generate_latest(_registry(registry))


def content_type():
    return _pc.CONTENT_TYPE_LATEST if _pc is not None else 'text/plain'


def start_exporter(port, addr=LOOPBACK, registry=None):
    """Serve ``/metrics`` on ``addr:port`` from a daemon thread. Idempotent
    per port; False (and a log line, never an exception) when disabled,
    unavailable or the port is taken."""
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    if port <= 0:
        logging.info('metrics exporter disabled (port %r)', port)
        return False
    if _pc is None:
        logging.warning('prometheus_client not installed; no metrics exporter on %s:%s', addr, port)
        return False
    with _lock:
        if port in _started:
            return _started[port] is not None
        try:
            _started[port] = _pc.start_http_server(port, addr=addr, registry=_registry(registry))
        except OSError as e:
            _started[port] = None
            logging.warning('metrics exporter could not bind %s:%s: %s', addr, port, e)
            return False
    logging.info('metrics exporter listening on http://%s:%s/metrics', addr, port)
    return True


def exporter_running(port):
    with _lock:
        return _started.get(port) is not None


def _reset_for_tests():
    """Forget cached metrics/callbacks (the registries themselves are the
    caller's to discard). Only tests call this."""
    with _lock:
        _metrics.clear()
        _callbacks.clear()
