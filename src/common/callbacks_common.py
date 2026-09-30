"""Callback POSTs shared by the audio and video processing services.

Each tree's callbacks.py injects its own config-derived URLs; the payload
shapes and the disconnect retry policy live here once instead of being
copy-pasted per tree.
"""
import time
import threading
import logging
from datetime import datetime, timezone

import requests

# Every callback runs on the websocket reactor/worker threads; without a
# timeout a stalled or restarting Flask blocks that thread indefinitely,
# pinning the pod's in-RAM buffers (the OOM profile the post-hoc service
# was already fighting). requests defaults to no timeout, so set one always.
CALLBACK_TIMEOUT = 30


def _post_best_effort(url, payload, name):
    try:
        requests.post(url, json=payload, timeout=CALLBACK_TIMEOUT)
    except Exception as e:
        logging.warning('%s callback failed: %s', name, e)


def callback_base(url):
    """The /api/v1/callback base, derived by stripping the last path segment.
    Each tree fed a different config URL into the same rsplit; that lived in
    two _callback_base copies."""
    return url.rsplit('/', 1)[0]


def post_json_ok(url, payload, name):
    """POST json and return whether the server answered 200; log-and-return
    False on any error. This is the try/post/status==200/except idiom that was
    hand-written in every simple poster across both trees."""
    try:
        response = requests.post(url, json=payload, timeout=CALLBACK_TIMEOUT)
        return response.status_code == 200
    except Exception as e:
        logging.warning('%s callback failed: %s', name, e)
        return False


def post_connect(connect_url, source):
    connection = {
        'source': source,
        'time': str(datetime.now(timezone.utc).replace(tzinfo=None))
    }
    try:
        response = requests.post(connect_url, json=connection, timeout=CALLBACK_TIMEOUT)
        logging.info('connect callback for %s: %s', source, response.status_code)
        return response.status_code == 200
    except Exception as e:
        logging.info('connect callback failed: {0}'.format(e))
        return False


def post_disconnect(disconnect_url, source):
    disconnection = {
        'source': source,
        'time': str(datetime.now(timezone.utc).replace(tzinfo=None))
    }
    # Retried from a background thread (the caller is the websocket reactor
    # thread, which must not block): this callback races the exact moments
    # the Flask server is down for a restart, and a lost disconnect leaves
    # the pod shown as connected forever — nothing ever re-sends it.
    def _send():
        for attempt in range(8):
            try:
                response = requests.post(disconnect_url, json=disconnection, timeout=10)
                if response.status_code == 200:
                    return
                logging.info('disconnect callback returned {0} (attempt {1})'.format(response.status_code, attempt + 1))
            except Exception as e:
                logging.info('disconnect callback failed (attempt {0}): {1}'.format(attempt + 1, e))
            time.sleep(min(2 ** attempt, 30))
        logging.warning('disconnect callback gave up for {0}'.format(source))
    threading.Thread(target=_send, daemon=True).start()
    return True


def post_posthoc_reset(callback_base, source, scope, wipe=True):
    # Ask the server to wipe this pod's previous results before a full re-run.
    # wipe=False only marks the run as running (the audio service now keeps
    # the old results until its new ones are complete; audit C.3).
    _post_best_effort(callback_base + '/posthoc_reset',
                      {'source': source, 'scope': scope, 'wipe': bool(wipe)}, 'posthoc_reset')


def post_posthoc_failed(callback_base, source, scope, reason):
    # The run did NOT finish (ASR crash, cancel, pre-emption, timeout): clear
    # the running flag but leave the pod's results and analysed status alone.
    _post_best_effort(callback_base + '/posthoc_failed',
                      {'source': source, 'scope': scope, 'reason': str(reason)[:1000]},
                      'posthoc_failed')


def post_posthoc_completed(callback_base, source, models, scope):
    # Mark a pod's post-hoc run complete server-side, so it persists even if
    # the triggering browser disconnected.
    _post_best_effort(callback_base + '/posthoc_completed',
                      {'source': source, 'models': models, 'scope': scope},
                      'posthoc_completed')


def post_service_restarted(callback_base, scope):
    # Announce a service (re)start so the server clears stale running flags
    # from runs that died with the previous process.
    _post_best_effort(callback_base + '/posthoc_service_restarted',
                      {'scope': scope}, 'service_restarted')
