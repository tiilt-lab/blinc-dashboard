from app import app, socketio, scheduler
import scheduled_tasks
import config as cf
import device_websockets
import os
import logging
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

# Wire the resource-centric authorization layer (src/server/authz.py). Deps are
# injected here — at the one place that already imports the DB, wrappers, and
# the request context — so authz itself stays Flask-free and unit-testable.
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


def main():

	# Set device connection status to false. Flask-SQLAlchemy 3 requires an
	# app context for any session use outside a request.
	with app.app_context():
		devices = database.get_devices(connected=True)
		for device in devices:
			device.connected = False
		database.save_changes()

	# Schedule tasks. max_instances=1 + coalesce: if a run overruns the 60s
	# interval (slow DB, many active sessions) the global job_defaults would
	# otherwise start a 2nd/3rd concurrent instance, and two of them can both
	# see a session as still-open and each fire end_session — duplicate 'end'
	# pings to every pod. This job must never overlap itself.
	scheduler.add_job(scheduled_tasks.check_transcripts, 'interval', seconds=60,
	                  max_instances=1, coalesce=True)

	# Pre-warm the video remux cache so first views never wait.
	from routes.session import prewarm_video_cache
	prewarm_video_cache()

	device_websockets.run_server()
	logging.info('Discussion Capture Server running...')
	# Threaded Werkzeug behind nginx is fine at this instance's scale
	# (research tool, a handful of concurrent users); the flag is Flask-
	# SocketIO's required acknowledgment of that choice.
	socketio.run(app, debug=cf.debug(), host="0.0.0.0", port=int(os.environ.get('DC_PORT', 5000)), use_reloader=False, allow_unsafe_werkzeug=True)
	scheduler.shutdown(wait=False)

if __name__ == '__main__':
    main()