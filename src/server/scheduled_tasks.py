# Jobs here run on the scheduler app.py creates and starts (registered in
# discussion_capture.py). This module holds only the job functions.
from datetime import datetime, timedelta, timezone
from handlers import session_handler
import database
import logging
import watchers

TIMEOUT = 10 * 60 # Time in seconds without transcripts before timeout occurs
WATCH_TIMEOUT = 5 * 60 # Dashboard poll recency that still counts as "watched"
TOKEN_RETENTION = timedelta(days=30) # Used/expired account_token rows older than this are purged
_last_token_purge = None

# Verifies if session is still active.
def check_transcripts():
	# Flask-SQLAlchemy 3: background jobs need an app context for db access.
	from app import app
	with app.app_context():
		_check_transcripts()
		_purge_account_tokens_daily()


def _purge_account_tokens_daily():
	# Rides on the minute job (jobs are registered in discussion_capture.py)
	# and runs once a day: account_token was never purged.
	global _last_token_purge
	now = datetime.now(timezone.utc)
	if _last_token_purge is not None and now - _last_token_purge < timedelta(days=1):
		return
	_last_token_purge = now
	try:
		count = database.delete_expired_account_tokens(TOKEN_RETENTION)
		if count:
			logging.info('Purged {0} used/expired account tokens older than {1} days'.format(count, TOKEN_RETENTION.days))
	except Exception:
		logging.exception('Account token purge has failed')
	finally:
		database.close_session()


def _check_transcripts():
	try:
		active_sessions = database.get_sessions(active=True)
		for session in active_sessions:
			devices = database.get_session_devices(session_id=session.id)
			# Lobby: joining is open and nobody has joined yet — an
			# instructor may have set up ahead of class; wait indefinitely.
			if session.passcode is not None and len(devices) == 0:
				continue
			# A connected group is activity, speaking or not.
			if any(device.connected for device in devices):
				continue
			# Someone has the session dashboard open (it polls every 2s).
			if watchers.watched_within(session.id, WATCH_TIMEOUT):
				continue
			length = session.get_length()
			if length > TIMEOUT:
				transcripts = database.get_transcripts(session_id=session.id, start_time=max(length - TIMEOUT, 0), end_time=-1)
				if len(transcripts) == 0:
					logging.info('Session {0}: no transcripts, no connected devices, and unwatched for {1} minutes - stopping.'.format(session.id, int(TIMEOUT / 60)))
					session_handler.end_session(session.id)
	except Exception:
		# The auto-close job failing silently means sessions never end; log
		# with the stack so the cause is visible in the journal.
		logging.exception('Session timeout scheduled task has failed')
	finally:
		database.close_session()
