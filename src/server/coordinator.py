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

# Fixed before any sibling import: device_commands reads DC_ROLE to pick the
# Twisted manager over the Redis proxy.
os.environ['DC_ROLE'] = 'coordinator'

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main():
    from discussion_capture import create_app
    from app import scheduler
    create_app(role='coordinator')
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
