"""BLINC API entry point.

Two ways to serve it:

* development: ``python src/server/discussion_capture.py`` runs threaded
  Werkzeug through Flask-SocketIO, exactly as it always has (``main()``).
* production: gunicorn loads ``wsgi:app`` (src/server/wsgi.py), which calls
  ``create_app()`` here. Settings live in src/server/gunicorn.conf.py.

Everything with a side effect (blueprint registration, the authz wiring, the
device connected-flag reset, the session-timeout job, the video-cache pre-warm
and the Twisted device-websocket thread) runs inside ``create_app()`` behind a
once-per-process guard, so importing this module is inert and the app is
initialised exactly once whichever runner loads it.

Environment knobs, for a second instance that shares this config.ini/DB
(a probe or a staging copy beside production):

* ``DC_DISABLE_SCHEDULER=1`` — do not register the 60 s session-timeout job
  (it would otherwise end sessions it cannot see being watched).
* ``DC_SKIP_BOOT_TASKS=1`` — skip the one-shot boot mutations: the
  ``device.connected = False`` reset and the ffmpeg video-cache pre-warm.
"""
from app import app, socketio, scheduler
import scheduled_tasks
import config as cf
import device_websockets
import os
import logging
import threading
import database

from routes.auth import api_routes as auth_api
from routes.session import api_routes as session_api
from routes.device import api_routes as device_api
from routes.keyword import api_routes as keyword_api
from routes.callback import api_routes as callback_api
from routes.admin import api_routes as admin_api
from routes.folder import api_routes as folder_api
from routes.topic_model import api_routes as topicmodel_api
from routes.speaker import api_routes as speaker_api
from routes.student import api_routes as student_api
from routes.llm_query import api_routes as llm_api
from routes.health import api_routes as health_api
from routes.data_quality import api_routes as data_quality_api
from routes.posthoc_ticket import api_routes as posthoc_ticket_api

# Side-effect import: routes/socket.py registers the /session socketio event
# handlers via @socketio.on decorators AT IMPORT TIME, and is referenced by no
# name — so a linter sees it as "unused". Do NOT remove it: without it the live
# socket namespace is unregistered and every client's room_joined never fires
# (the whole overview loads forever).
from routes import socket as _socket_handlers  # noqa: F401

# Once-per-process guard for create_app(). Flask raises on a second
# register_blueprint of the same blueprint and a second device-websocket
# listener would fail to bind, so the side effects below must never run twice
# in one process — however many times create_app() is called.
_init_lock = threading.Lock()
_initialised = False


def _env_flag(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _register_blueprints():
    app.register_blueprint(admin_api)
    app.register_blueprint(auth_api)
    app.register_blueprint(session_api)
    app.register_blueprint(device_api)
    app.register_blueprint(keyword_api)
    app.register_blueprint(callback_api)
    app.register_blueprint(folder_api)
    app.register_blueprint(topicmodel_api)
    app.register_blueprint(speaker_api)
    app.register_blueprint(student_api)
    app.register_blueprint(llm_api)
    app.register_blueprint(health_api)
    app.register_blueprint(data_quality_api)
    app.register_blueprint(posthoc_ticket_api)


def _configure_authz():
    # Wire the resource-centric authorization layer (src/server/authz.py). Deps
    # are injected here — at the one place that already imports the DB,
    # wrappers, and the request context — so authz itself stays Flask-free and
    # unit-testable.
    import authz
    import wrappers
    from flask import request
    authz.configure(authz.Deps(
        get_device=lambda did: database.get_session_devices(id=did),
        get_transcript=lambda tid: database.get_transcript(tid),
        session_access=lambda sid, user, write: bool(wrappers._session_for(sid, user, write)),
        key_grants=lambda device: bool(device.processing_key) and (
            request.headers.get('X-Processing-Key') or request.args.get('key')
        ) == device.processing_key,
    ))


def _start_background_services():
    skip_boot_tasks = _env_flag('DC_SKIP_BOOT_TASKS')

    if skip_boot_tasks:
        logging.warning('DC_SKIP_BOOT_TASKS set: not resetting device connected flags, not pre-warming the video cache')
    else:
        # Set device connection status to false. Flask-SQLAlchemy 3 requires an
        # app context for any session use outside a request.
        with app.app_context():
            devices = database.get_devices(connected=True)
            for device in devices:
                device.connected = False
            database.save_changes()

    if _env_flag('DC_DISABLE_SCHEDULER'):
        logging.warning('DC_DISABLE_SCHEDULER set: session-timeout job not scheduled')
    else:
        # Schedule tasks. max_instances=1 + coalesce: if a run overruns the 60s
        # interval (slow DB, many active sessions) the global job_defaults would
        # otherwise start a 2nd/3rd concurrent instance, and two of them can both
        # see a session as still-open and each fire end_session — duplicate 'end'
        # pings to every pod. This job must never overlap itself.
        scheduler.add_job(scheduled_tasks.check_transcripts, 'interval', seconds=60,
                          max_instances=1, coalesce=True)

    if not skip_boot_tasks:
        # Pre-warm the video remux cache so first views never wait.
        from routes.session import prewarm_video_cache
        prewarm_video_cache()

    device_websockets.run_server()


def create_app():
    """Return the fully initialised Flask app.

    Safe to call any number of times; the side effects run exactly once per
    process. The guard flag is set BEFORE the work so that a boot failure (DB
    down during the device reset, device-websocket port taken) can never lead
    to a second attempt double-registering blueprints — the exception
    propagates and ends the process, which is what both the dev runner and
    gunicorn's arbiter (which respawns the worker) expect.
    """
    global _initialised
    with _init_lock:
        if not _initialised:
            _initialised = True
            _register_blueprints()
            _configure_authz()
            _start_background_services()
    return app


def main():
    create_app()
    logging.info('Discussion Capture Server running...')
    # Development runner. Threaded Werkzeug behind nginx was fine at this
    # instance's scale (research tool, a handful of concurrent users); the
    # flag is Flask-SocketIO's required acknowledgment of that choice. The
    # production unit serves wsgi:app with gunicorn instead (gunicorn.conf.py).
    socketio.run(app, debug=cf.debug(), host="0.0.0.0", port=int(os.environ.get('DC_PORT', 5000)), use_reloader=False, allow_unsafe_werkzeug=True)
    scheduler.shutdown(wait=False)


if __name__ == '__main__':
    main()
