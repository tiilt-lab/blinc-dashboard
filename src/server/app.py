
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
import logging
import os
import redis
from datetime import timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import config as cf

# Initialize config file
cf.initialize()
base_dir = os.path.dirname(os.path.abspath(__file__))

# Create logger
log_format = logging.Formatter('%(asctime)s - %(name)s(%(levelname)s): %(message)s')
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('apscheduler.executors.default').setLevel(logging.CRITICAL) # Suppresses output from APScheduler
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('socketio.server').setLevel(logging.WARNING)
logging.getLogger('engineio.server').setLevel(logging.WARNING)
dir_path = os.path.dirname(os.path.realpath(__file__))
log_file = logging.FileHandler(os.path.join(dir_path, 'discussion_capture_server.log'))
log_file.setFormatter(log_format)
logger.addHandler(log_file)
log_console = logging.StreamHandler()
log_console.setFormatter(log_format)
logger.addHandler(log_console)

# Create app
app = Flask(__name__)

 
# Session-signing key. Set DC_SECRET_KEY (e.g. in /etc/blinc/secrets.env) so
# sessions survive restarts; the random fallback logs everyone out on reboot.
app.config['SECRET_KEY'] = os.environ.get('DC_SECRET_KEY') or os.urandom(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = cf.https()
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.config['SESSION_COOKIE_NAME'] = 'DCSession'
# 30 minutes was far too aggressive: with a sliding window (refresh-each-
# request below), a user reading a dashboard or writing for half an hour with
# no request firing got logged out. 7 days of INACTIVITY is the threshold now
# — the cookie stays HttpOnly + Secure + SameSite=Strict, so the longer window
# is low-risk, and instructors stay signed in across a week of class sessions.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

# Cap request bodies. The largest legitimate payload is a session-video
# upload (~2h at the 5 Mbps recording ceiling ≈ 4.5 GB); without a cap a
# single request could fill the disk. Everything else is far smaller.
app.config['MAX_CONTENT_LENGTH'] = 6 * 1024 * 1024 * 1024

# Trust exactly as many trailing X-Forwarded-For entries as there are proxies in
# front of this instance (see config.proxy_count). Trusting one too many hands
# remote_addr to the caller, since everything left of the proxies' own entries is
# client-supplied; trusting one too few leaves every client looking like the
# proxy. Set proxy_count in config.ini when the chain is longer than nginx alone.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=cf.proxy_count(), x_proto=1)

# Redis
r = redis.Redis(host=cf.redis_host(), port=cf.redis_port(), db=cf.redis_db())

# Set API Limiter. Redis-backed so counters survive restarts and are shared
# if the app ever runs more than one worker.
limiter = Limiter(get_remote_address, app=app, storage_uri=cf.redis_url())

# Create SocketIO app (engineio_logger=True for advance debug)
# threading mode (simple-websocket) replaces deprecated eventlet; the
# in-process Twisted device-websocket server gets real sockets again.
# websocket-only (forwarded to engineio.Server(transports=...)): with several
# gunicorn workers a long-polling session's requests would land on different
# workers and be unknown there; one upgraded socket stays on one worker. The
# Redis message_queue is what lets any worker emit into any room.
socketio = SocketIO(app, async_mode='threading', log=logger, cors_allowed_origins=cf.domains() + ["127.0.0.1:5000", "localhost"], manage_session=False, message_queue=cf.redis_url(), transports=['websocket'])

# Create database
DATABASE_FILE = os.path.dirname(os.path.abspath(__file__)) + '/discussion_capture.db'
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+mysqlconnector://{0}@localhost:3306/{1}'.format(cf.database_user(), cf.database_name())
# Pool sized for the threaded server. The defaults (5 + 10 overflow, 30 s wait,
# no pre-ping) time out once ~15 slow requests hold connections, and hand out a
# dead connection after a MySQL restart. MySQL max_connections is 151.
# See docs/infra-audit-2026-09-30.md, "API serving stack".
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 20,
    'max_overflow': 30,
    'pool_timeout': 10,
    'pool_recycle': 1800,
    'pool_pre_ping': True,
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Create scheduler (job functions live in scheduled_tasks.py; jobs are
# registered in discussion_capture.py)
scheduler = BackgroundScheduler({
	'apscheduler.jobstores.default': {
		'type': 'memory',
	},
	'apscheduler.executors.default': {
		'class': 'apscheduler.executors.pool:ThreadPoolExecutor',
		'max_workers': '20'
	},
	'apscheduler.executors.processpool': {
		'type': 'processpool',
		'max_workers': '5'
	},
	'apscheduler.job_defaults.coalesce': 'false',
	'apscheduler.job_defaults.max_instances': '3',
	'apscheduler.timezone': 'UTC',
})
scheduler.start()
