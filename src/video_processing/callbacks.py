import os
import sys
import time
import threading
import collections
import config
import requests
import logging
from datetime import datetime, timezone

# Shared payloads + retry policy live in src/common/callbacks_common.py
# (same shim pattern as connection_manager.py / redis_helper.py).
_COMMON = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
import callbacks_common  # noqa: E402


def _callback_base():
    # .../api/v1/callback — derived once from the video-metrics callback URL.
    # (Video config has no processing_callback accessor — a typo here once made
    # the restart callback fail every time, leaving dead runs flagged as
    # running for the 3h TTL.)
    return callbacks_common.callback_base(config.video_metrics_callback())


# ---- session-key check (audit E.1) ------------------------------------------
# The API answers 400 for a key it does not know. Anything else — connection
# refused while the API restarts (~6.5 s, longer than the 5 s sweep), a 5xx
# from a Redis hiccup, a timeout — says nothing about the key. Collapsing all
# of them to None let the sweep close every live pod on each API restart.
KEY_REVOKED_STATUSES = (400, 401, 403, 404)


def classify_key_response(status_code, body):
    """Pure: (outcome, session_key), outcome in 'valid' | 'revoked' | 'unknown'.
    A 200 without a usable redis_key is 'unknown', not 'revoked': only an
    explicit 4xx is treated as the API saying the key is gone."""
    if status_code == 200:
        key = body.get('redis_key') if isinstance(body, dict) else None
        return ('valid', key) if key else ('unknown', None)
    if status_code in KEY_REVOKED_STATUSES:
        return 'revoked', None
    return 'unknown', None


def check_session_key(auth_key):
    """(outcome, session_key, detail). Never raises; see classify_key_response."""
    try:
        response = requests.post(config.redis_session_key_callback(), json={'auth_key': auth_key},
                                 timeout=callbacks_common.CALLBACK_TIMEOUT)
    except Exception as e:
        return 'unknown', None, '{0}: {1}'.format(type(e).__name__, e)
    try:
        body = response.json()
    except Exception:
        body = None
    outcome, key = classify_key_response(response.status_code, body)
    return outcome, key, 'HTTP {0}'.format(response.status_code)


def get_redis_session_key(auth_key):
    outcome, key, detail = check_session_key(auth_key)
    if outcome != 'valid':
        logging.info('get_redis_session_key failed (%s): %s', outcome, detail)
    return key


def get_redis_session_config(session_key):
    payload = {
    'session_key': session_key
    }
    try:
        response = requests.post(config.redis_session_config_callback(), json=payload, timeout=callbacks_common.CALLBACK_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            return data['redis_session_key']
        else:
            logging.info('get_redis_session_config callback  failed: {0}'.format(response))
            return None
    except Exception as e:
        logging.info('get_redis_session_config callback  failed: {0}'.format(e))
        return None


def post_service_restarted(scope='video'):
    callbacks_common.post_service_restarted(_callback_base(), scope)


# ---- bounded retry for fire-and-forget callbacks (audit E.4) ----------------
# Connect/disconnect/metric posts were single-shot: one that raced an API
# restart was lost for good (pod shown connected forever, metrics gone). The
# first attempt still runs inline on the caller's thread (call sites are off
# the reactor: defer_blocking or the analytics worker); a retryable failure
# (connection error, timeout, 5xx) parks the post here and one daemon thread
# replays it with backoff. Every attempt carries the same X-Idempotency-Key so
# the API can drop replays of a post that did land.
RETRY_MAX_ENTRIES = 2000
RETRY_GIVE_UP_SECONDS = 600
RETRY_BACKOFF_MIN = 1.0
RETRY_BACKOFF_MAX = 60.0
RETRY_SUMMARY_INTERVAL = 60.0
IDEMPOTENCY_HEADER = 'X-Idempotency-Key'


def idempotency_key(processing_key, kind, time_stamp):
    # kind = videometrics | connect | disconnect
    return '{0}:{1}:{2}'.format(processing_key, kind, time_stamp)


def metrics_time_stamp(video_metrics):
    """Earliest time_stamp in a video_metrics payload (each batch covers a
    disjoint time range, so it identifies the batch); wall clock if none."""
    stamps = [row[0] for rows in (video_metrics or {}).values() for row in rows if row]
    return min(stamps) if stamps else int(time.time())


def _http_sender(url, payload, headers, timeout):
    return requests.post(url, json=payload, headers=headers, timeout=timeout).status_code


class CallbackRetryQueue:
    def __init__(self, sender=None, clock=time.monotonic, max_entries=RETRY_MAX_ENTRIES,
                 give_up=RETRY_GIVE_UP_SECONDS, timeout=None, start_thread=True):
        self._sender = sender or _http_sender
        self._clock = clock
        self._max = max_entries
        self._give_up = give_up
        self._timeout = callbacks_common.CALLBACK_TIMEOUT if timeout is None else timeout
        self._start_thread = start_thread
        self._lock = threading.Lock()
        self._items = collections.deque()
        self._wake = threading.Event()
        self._thread = None
        self._last_summary = None
        self._last_drop_warn = None
        self._dropped_since_warn = 0
        self.delivered = 0   # replays that landed
        self.gave_up = 0
        self.dropped = 0     # evicted unsent because the queue was full

    def __len__(self):
        with self._lock:
            return len(self._items)

    def post(self, url, payload, name, key):
        """Inline first attempt; True iff it landed now. A retryable failure is
        queued and replayed in the background (the caller cannot tell the
        difference from a permanent one, and never needs to)."""
        item = {'url': url, 'payload': payload, 'name': name, 'key': key,
                'headers': {IDEMPOTENCY_HEADER: key}, 'attempts': 0,
                'first_at': self._clock(), 'backoff': RETRY_BACKOFF_MIN}
        ok, retryable = self._attempt(item)
        if not ok and retryable:
            item['next_at'] = self._clock() + item['backoff']
            self._enqueue(item)
        return ok

    def _attempt(self, item):
        """(delivered, retryable). 2xx landed; 3xx/4xx is a permanent rejection
        (retrying cannot fix the payload); only errors and 5xx are retried."""
        item['attempts'] += 1
        n = item['attempts']
        level = logging.WARNING if n == 1 else logging.DEBUG  # replays are summarised
        try:
            status = self._sender(item['url'], item['payload'], item['headers'], self._timeout)
        except Exception as e:
            logging.log(level, '%s callback failed (attempt %d, will retry): %s', item['name'], n, e)
            return False, True
        if 200 <= status < 300:
            return True, False
        if status >= 500:
            logging.log(level, '%s callback returned HTTP %d (attempt %d, will retry)', item['name'], status, n)
            return False, True
        logging.warning('%s callback rejected with HTTP %d; not retrying', item['name'], status)
        return False, False

    def _enqueue(self, item):
        with self._lock:
            while len(self._items) >= self._max:
                oldest = self._items.popleft()  # bounded: oldest goes first
                self.dropped += 1
                self._dropped_since_warn += 1
                now = self._clock()
                if self._last_drop_warn is None or now - self._last_drop_warn >= RETRY_SUMMARY_INTERVAL:
                    logging.warning('callback retry queue full (%d): dropped oldest %s %s; %d dropped in the last %ds, %d total',
                                    self._max, oldest['name'], oldest['key'], self._dropped_since_warn,
                                    int(RETRY_SUMMARY_INTERVAL), self.dropped)
                    self._last_drop_warn = now
                    self._dropped_since_warn = 0
            self._items.append(item)
            if self._start_thread and self._thread is None:
                self._thread = threading.Thread(target=self._loop, name='callback-retry', daemon=True)
                self._thread.start()
        self._wake.set()

    def run_due(self, now=None):
        """Replay every due item once. Returns seconds until the next item is
        due, or None when the queue is empty. Called by the thread; tests
        drive it directly with an injected clock."""
        now = self._clock() if now is None else now
        with self._lock:
            due = [it for it in self._items if it['next_at'] <= now]
            for it in due:
                self._items.remove(it)
        for it in due:
            ok, retryable = self._attempt(it)
            if ok:
                self.delivered += 1
                logging.info('%s callback delivered on retry %d (%s)', it['name'], it['attempts'], it['key'])
            elif retryable:
                if self._clock() - it['first_at'] >= self._give_up:
                    self.gave_up += 1
                    logging.warning('%s callback gave up after %d attempts over %ds (%s)',
                                    it['name'], it['attempts'], int(self._give_up), it['key'])
                else:
                    it['backoff'] = min(it['backoff'] * 2, RETRY_BACKOFF_MAX)
                    it['next_at'] = self._clock() + it['backoff']
                    self._enqueue(it)
        with self._lock:
            pending = len(self._items)
            next_at = min((it['next_at'] for it in self._items), default=None)
        if pending and (self._last_summary is None or now - self._last_summary >= RETRY_SUMMARY_INTERVAL):
            self._last_summary = now
            logging.warning('callback retry queue: %d pending, %d delivered on retry, %d gave up, %d dropped',
                            pending, self.delivered, self.gave_up, self.dropped)
        return None if next_at is None else max(next_at - self._clock(), 0.0)

    def _loop(self):
        while True:
            try:
                delay = self.run_due()
            except Exception:
                logging.exception('callback retry loop error')
                delay = RETRY_BACKOFF_MIN
            self._wake.wait(RETRY_BACKOFF_MAX if delay is None else max(delay, 0.05))
            self._wake.clear()


_retry_queue = CallbackRetryQueue()


def _post_with_retry(url, payload, name, kind, source, time_stamp):
    return _retry_queue.post(url, payload, name, idempotency_key(source, kind, time_stamp))


def _utc_now_str():
    return str(datetime.now(timezone.utc).replace(tzinfo=None))


def post_connect(source):
    stamp = _utc_now_str()
    return _post_with_retry(config.connect_callback(), {'source': source, 'time': stamp},
                            'connect', 'connect', source, stamp)


def post_disconnect(source):
    # Was callbacks_common.post_disconnect (8 tries, 10 s timeout, own thread);
    # the shared queue gives it the same 10-minute window as everything else.
    stamp = _utc_now_str()
    return _post_with_retry(config.disconnect_callback(), {'source': source, 'time': stamp},
                            'disconnect', 'disconnect', source, stamp)


def post_video_metrics(source, video_metrics):
    result = {
        'source': source,
        'video_metrics': video_metrics
    }
    return _post_with_retry(config.video_metrics_callback(), result, 'video metric',
                            'videometrics', source, metrics_time_stamp(video_metrics))


def post_gaze_overlays(source, records, reset=False):
    # Overlay geometry (head/gaze/object boxes) for the dashboard's video
    # overlay toggles. The server appends to a per-pod JSONL; reset=True on a
    # run's first batch replaces the previous run's file.
    return callbacks_common.post_json_ok(
        _callback_base() + '/gaze_overlays',
        {'source': source, 'records': records, 'reset': reset}, 'gaze overlay')


def post_posthoc_reset(source, scope):
    callbacks_common.post_posthoc_reset(_callback_base(), source, scope)


def post_posthoc_completed(source, models=None, scope='video'):
    callbacks_common.post_posthoc_completed(_callback_base(), source, models, scope)
