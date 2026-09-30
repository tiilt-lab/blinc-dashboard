"""Accessors behind the live audio server's Prometheus exporter (port 9111).

server.py wires this up at start (``install(cm, retry_queue)``) and folds a
pod's ingest totals in when it ends (``retire(counter)``); everything else is
read at scrape time from objects that already exist: the ConnectionManager's
protocols, each protocol's ``IngestCounter`` and ASR connector, the shared
CrisperWhisper worker pool and the callback ``RetryQueue``. Nothing here
runs on the reactor thread and nothing writes to the protocols.

The received/dropped totals are per connection, so summing the live ones
would fall every time a pod leaves (which rate() reads as a counter reset).
``retire`` keeps the totals of ended connections, so the exported counters
are monotonic: retired + live.

``pool_stats()`` reads the pool through getattr with defaults, so the ASR
workstream can rename internals without breaking the exporter; it is the
hook to extend when the pool grows a proper stats() method.
"""
import os
import sys
import threading

_COMMON = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
import blinc_metrics as bm  # noqa: E402

WINDOW_SECONDS_DEFAULT = 12.0

_cm = None
_retry_queue = None
_lock = threading.Lock()
_retired = {'chunks_received': 0, 'chunks_dropped': 0,
            'samples_received': 0, 'samples_dropped': 0}


def _protocols():
    """Snapshot of the manager's connections (its lock is an RLock)."""
    if _cm is None:
        return []
    with _cm.lock:
        return list(_cm.connections)


def live_protocols():
    """Connections that are streaming audio (running, past the start message)."""
    return [p for p in _protocols()
            if getattr(p, 'running', False) and getattr(p, 'stream_data', None) == 'audio']


def pod_key(protocol):
    config = getattr(protocol, 'config', None)
    return str(getattr(config, 'auth_key', None) or id(protocol))


def retire(counter):
    """Fold an ended connection's IngestCounter totals into the running sums."""
    if counter is None:
        return
    with _lock:
        _retired['chunks_received'] += int(getattr(counter, 'chunks_received', 0))
        _retired['chunks_dropped'] += int(getattr(counter, 'chunks_dropped', 0))
        _retired['samples_received'] += int(getattr(counter, 'samples_received', 0))
        _retired['samples_dropped'] += int(getattr(counter, 'samples_dropped', 0))


def ingest_totals():
    """Monotonic received/dropped totals: retired connections + live ones."""
    with _lock:
        totals = dict(_retired)
    for p in live_protocols():
        c = getattr(p, '_ingest', None)
        if c is None:
            continue
        for field in totals:
            totals[field] += int(getattr(c, field, 0))
    return totals


def ingest_queue_chunks():
    """Chunks waiting in each pod's reactor->ASR queue."""
    out = {}
    for p in live_protocols():
        q = getattr(p, 'asr_audio_queue', None)
        out[(pod_key(p),)] = q.qsize() if q is not None else 0
    return out


def window_backlog_seconds():
    """Per pod: windows waiting for a worker x the window length (the
    caption-lag proxy; the load generator measures the real lag)."""
    out = {}
    for p in live_protocols():
        asr = getattr(p, 'asr', None)
        q = getattr(asr, '_window_queue', None)
        seconds = float(getattr(asr, 'WINDOW_SECONDS', WINDOW_SECONDS_DEFAULT) or WINDOW_SECONDS_DEFAULT)
        out[(pod_key(p),)] = (q.qsize() if q is not None else 0) * seconds
    return out


def asr_degraded_by_pod():
    """1 while the pod was last told asr_status=degraded (server.py records it)."""
    return {(pod_key(p),): 1 if getattr(p, 'asr_state', 'ok') == 'degraded' else 0
            for p in live_protocols()}


def pool_stats():
    """The CrisperWhisper worker pool as it exists today, read defensively.

    Returns ``{'size', 'busy', 'alive', 'degraded', 'waiting', 'state'}``;
    all zero / 'unknown' when the connector is not importable here. The ASR
    workstream can point this at a pool.stats() method when one exists.
    """
    stats = {'size': 0, 'busy': 0, 'alive': 0, 'degraded': 0, 'waiting': 0, 'state': 'unknown'}
    try:
        from asr_connectors import crisperwhisper_asr as cw
    except Exception:
        return stats
    pool = getattr(cw, '_pool', None)
    if pool is None:
        return stats
    slots = list(getattr(pool, 'slots', []) or [])
    busy = getattr(pool, '_busy', ()) or ()
    stats['size'] = int(getattr(pool, 'size', len(slots)) or 0)
    stats['busy'] = sum(1 for s in slots if s in busy)
    stats['alive'] = sum(1 for s in slots if getattr(s, 'alive', False))
    stats['degraded'] = sum(1 for s in slots if getattr(s, 'state', 'ok') != 'ok')
    stats['waiting'] = int(getattr(pool, 'waiting', 0) or 0)
    stats['state'] = str(getattr(pool, 'state', 'unknown'))
    return stats


def _slots_by_state():
    s = pool_stats()
    return {('busy',): s['busy'], ('free',): max(s['size'] - s['busy'], 0),
            ('alive',): s['alive'], ('degraded',): s['degraded']}


def retry_stats():
    if _retry_queue is None or not hasattr(_retry_queue, 'stats'):
        return {'size': 0, 'delivered': 0, 'gave_up': 0, 'dropped': 0}
    return _retry_queue.stats()


def install(cm, retry_queue=None, registry=None):
    """Register every gauge/counter over ``cm`` and start the exporter."""
    global _cm, _retry_queue
    _cm = cm
    _retry_queue = retry_queue
    bm.install_process_metrics('audio', registry=registry)
    bm.callback_gauge('audio_connections', 'Open websocket connections on the live audio server',
                      lambda: cm.get_number_of_connections(), registry=registry)
    bm.callback_gauge('audio_live_pods', 'Pods streaming audio right now',
                      lambda: len(live_protocols()), registry=registry)
    bm.callback_counter('audio_chunks_received_total', 'Audio chunks received for ASR ingest',
                        lambda: ingest_totals()['chunks_received'], registry=registry)
    bm.callback_counter('audio_chunks_dropped_total', 'Audio chunks evicted from a full ingest queue',
                        lambda: ingest_totals()['chunks_dropped'], registry=registry)
    bm.callback_counter('audio_seconds_dropped_total', 'Seconds of audio lost to ingest-queue eviction',
                        lambda: ingest_totals()['samples_dropped'] / 16000.0, registry=registry)
    bm.callback_gauge('audio_ingest_queue_chunks', 'Chunks waiting in the pod\'s reactor->ASR queue',
                      ingest_queue_chunks, labels=('pod',), registry=registry)
    bm.callback_gauge('audio_window_backlog_seconds',
                      'Windows waiting for an ASR worker x window length (caption-lag proxy)',
                      window_backlog_seconds, labels=('pod',), registry=registry)
    bm.callback_gauge('audio_asr_degraded', '1 while the pod was last told asr_status=degraded',
                      asr_degraded_by_pod, labels=('pod',), registry=registry)
    bm.callback_gauge('asr_pool_degraded', '1 while every CrisperWhisper slot is degraded',
                      lambda: 1 if pool_stats()['state'] == 'degraded' else 0, registry=registry)
    bm.callback_gauge('asr_pool_size', 'CrisperWhisper worker slots (DC_ASR_WORKERS)',
                      lambda: pool_stats()['size'], registry=registry)
    bm.callback_gauge('asr_slots', 'CrisperWhisper slots by state', _slots_by_state,
                      labels=('state',), registry=registry)
    bm.callback_gauge('asr_windows_waiting', 'Windows queued for a free CrisperWhisper slot',
                      lambda: pool_stats()['waiting'], registry=registry)
    bm.callback_gauge('audio_callback_retry_queue', 'API callbacks waiting for a retry',
                      lambda: retry_stats()['size'], registry=registry)
    bm.callback_counter('audio_callback_retries_total', 'Retried API callbacks by outcome',
                        lambda: {('delivered',): retry_stats()['delivered'],
                                 ('gave_up',): retry_stats()['gave_up'],
                                 ('dropped',): retry_stats()['dropped']},
                        labels=('outcome',), registry=registry)
    return bm.start_exporter(bm.port_for('audio'), registry=registry)
