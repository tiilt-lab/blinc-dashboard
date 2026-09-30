# Registry of "someone has this session's dashboard open". The session
# overview polls GET /sessions/<id>/devices every 2s while the page is open,
# so a recent poll means an instructor is actively watching.
#
# The poll lands on any gunicorn API worker while the reader (the
# session-timeout job, scheduled_tasks.check_transcripts) runs in the
# coordinator process, so the stamp lives in Redis: key ``watched:<session_id>``
# holding the epoch time, with a TTL past every window a caller asks about.
# The old dict stays as the fallback when Redis is unreachable.
import logging
import time

import redis_sync

WATCH_TTL = 15 * 60  # > scheduled_tasks.WATCH_TIMEOUT (5 min)
_PREFIX = 'watched:'
_last_watched = {}


def _key(session_id):
    return _PREFIX + str(int(session_id))


def mark_watched(session_id):
    now = time.time()
    try:
        redis_sync.client().set(_key(session_id), now, ex=WATCH_TTL)
    except Exception as e:
        logging.debug('watchers: Redis mark failed (%s); local only', e)
        _last_watched[int(session_id)] = now


def watched_within(session_id, seconds):
    ts = None
    try:
        value = redis_sync.client().get(_key(session_id))
        ts = float(value) if value is not None else None
    except Exception as e:
        logging.debug('watchers: Redis read failed (%s); local only', e)
    if ts is None:
        ts = _last_watched.get(int(session_id))
    return ts is not None and (time.time() - ts) < seconds
