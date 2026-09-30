"""BLINC coordinator: the single-instance half of the API (DC_ROLE=coordinator).

gunicorn serves HTTP and Socket.IO from several interchangeable workers
(wsgi.py, gunicorn.conf.py); everything that must exist exactly once runs
here instead, sharing the same app/db/config through ``create_app``:

* the Twisted device websocket server (DC_DEVICE_WS_PORT) and its
  ConnectionManager, fed by device commands the workers publish to Redis
  (device_commands.py);
* the APScheduler session-timeout job (scheduled_tasks.check_transcripts);
* the post-hoc queue runner (posthoc_queue.start_runner);
* the one-shot boot tasks (device connected-flag reset, video cache pre-warm).

No HTTP is served. The same environment knobs as the API apply
(DC_DISABLE_SCHEDULER, DC_SKIP_BOOT_TASKS, DC_DISABLE_POSTHOC_QUEUE), so a
probe beside production can run one with every side effect off.
"""
import logging
import os
import signal
import sys
import threading
import time

# Fixed before any sibling import: device_commands reads DC_ROLE to pick the
# Twisted manager over the Redis proxy.
os.environ['DC_ROLE'] = 'coordinator'

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
COMMON = os.path.abspath(os.path.join(HERE, '..', 'common'))
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)


def install_metrics():
    """Exporter on BLINC_METRICS_PORT (default 9110, loopback): the device
    websocket connections, the scheduler's job runs, the post-hoc queue
    runner, plus the Redis-derived gauges the API workers also export."""
    import blinc_metrics as bm
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
    from app import scheduler
    import device_websockets
    import posthoc_queue
    from routes.metrics import register_shared_gauges

    # create_app() registered the metrics blueprint, whose install() already
    # started this process's gauges under service="coordinator" (DC_ROLE)
    # and the shared Redis gauges; the call below is an idempotent no-op that
    # keeps the dependency explicit.
    register_shared_gauges()
    bm.callback_gauge('device_connections', 'Pods connected to the device websocket server',
                      lambda: len(getattr(device_websockets.ConnectionManager.instance, 'connections', ())))
    bm.callback_gauge('posthoc_runner_alive', '1 while the post-hoc queue runner thread is alive',
                      lambda: 1 if (posthoc_queue._worker is not None and posthoc_queue._worker.is_alive()) else 0)
    runs = bm.counter('scheduler_job_runs_total', 'APScheduler job executions by outcome',
                      ('job', 'outcome'))
    last_run = bm.gauge('scheduler_job_last_run_timestamp_seconds',
                        'Unix time of the last execution of each scheduler job', ('job',))
    outcomes = {EVENT_JOB_EXECUTED: 'ok', EVENT_JOB_ERROR: 'error', EVENT_JOB_MISSED: 'missed'}

    def _on_job_event(event):
        job = scheduler.get_job(event.job_id)
        name = job.name if job is not None else str(event.job_id)
        runs.labels(name, outcomes.get(event.code, 'other')).inc()
        last_run.labels(name).set(time.time())

    scheduler.add_listener(_on_job_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    return bm.start_exporter(bm.port_for('coordinator'))


def main():
    from discussion_capture import create_app
    from app import scheduler
    create_app(role='coordinator')
    try:
        install_metrics()
    except Exception:
        logging.exception('metrics exporter not started (coordinator keeps running)')
    stop = threading.Event()

    def _terminate(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    logging.info('BLINC coordinator running (device websocket port %s)...',
                 os.environ.get('DC_DEVICE_WS_PORT', 9001))
    while not stop.is_set():
        stop.wait(1)
    scheduler.shutdown(wait=False)
    try:
        from twisted.internet import reactor
        reactor.callFromThread(reactor.stop)
    except Exception:
        pass
    logging.info('BLINC coordinator stopped.')


if __name__ == '__main__':
    main()
