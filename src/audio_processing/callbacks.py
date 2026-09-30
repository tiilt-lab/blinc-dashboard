import os
import sys
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
import callback_retry  # noqa: E402


def _callback_base():
    # .../api/v1/callback — derived from the transcript callback URL.
    return callbacks_common.callback_base(config.processing_callback())


# Retryable: network errors, 5xx, and the two 4xx that mean "later". A
# 400/404 fails the same way every time, so it is logged and dropped.
def _retryable(status):
    return status is None or status >= 500 or status in (408, 429)


def _post_once(url, payload, headers, name):
    """One attempt with the standard timeout -> (response or None, retryable)."""
    try:
        response = requests.post(url, json=payload, headers=headers,
                                 timeout=callbacks_common.CALLBACK_TIMEOUT)
    except Exception as e:
        logging.warning('%s callback failed: %s', name, e)
        return None, True
    if response.status_code != 200:
        logging.warning('%s callback returned %s', name, response.status_code)
    return response, _retryable(response.status_code)


def _retry_sender(url, payload, headers):
    response, retryable = _post_once(url, payload, headers,
                                     'Retry of ' + headers[callback_retry.IDEMPOTENCY_HEADER])
    return response is not None and (response.status_code == 200 or not retryable)


# Transcript, speaker-metric and connect posts were sent once with no retry,
# so every one that landed in an API restart was lost (audit E.4).
_RETRIES = callback_retry.RetryQueue(_retry_sender)

_NAMES = {'transcript': 'Transcript', 'metrics': 'Speaker Metrics', 'connect': 'connect'}


def _post_with_retry(kind, url, payload, source, start_time):
    """First attempt inline (callers keep their synchronous result); a
    retryable failure goes to the background queue. Replays are safe: the
    API dedupes transcripts on (device, start_time, length) and treats
    connect as idempotent, and every attempt carries the same key."""
    key = callback_retry.idempotency_key(source, kind, start_time)
    response, retryable = _post_once(url, payload,
                                     {callback_retry.IDEMPOTENCY_HEADER: key}, _NAMES[kind])
    if response is None or (response.status_code != 200 and retryable):
        _RETRIES.submit(key, url, payload)
    return response


def post_transcripts(source, start_time, end_time, transcript, doa, questions, keywords, features, topic_id, speaker_tag, speaker_id, voice_features=None):
    result = {
        'source': source,
        'start_time': start_time,
        'end_time': end_time
    }
    if voice_features:
        result['voice_features'] = voice_features
    if transcript:
        result['transcript'] = transcript
        result['questions'] = questions
    if doa:
        result['direction'] = doa
    if keywords:
        result['keywords'] = keywords
    if features:
        result['features'] = features
    if topic_id:
        result['topic_id'] = topic_id
    if speaker_tag:
        result['speaker_tag'] = speaker_tag
    if speaker_id:
        result['speaker_id'] = speaker_id
    response = _post_with_retry('transcript', config.processing_callback(), result, source, start_time)
    if response is None or response.status_code != 200:
        return False, -1
    try:
        return True, response.json()['transcript_id']
    except Exception as e:
        logging.warning('Transcript callback: unreadable reply (%s)', e)
        return True, -1


def post_posthoc_reset(source, scope, wipe=True):
    callbacks_common.post_posthoc_reset(_callback_base(), source, scope, wipe=wipe)


def post_posthoc_failed(source, scope, reason):
    callbacks_common.post_posthoc_failed(_callback_base(), source, scope, reason)


def post_posthoc_completed(source, models=None, scope='audio'):
    callbacks_common.post_posthoc_completed(_callback_base(), source, models, scope)


def post_service_restarted(scope='audio'):
    callbacks_common.post_service_restarted(_callback_base(), scope)


def post_tagging(source, tag, embeddingsFile):
    result = {
        'source': source,
        'tagging': tag,
        'embeddingsFile': embeddingsFile
    }
    return callbacks_common.post_json_ok(config.tagging_callback(), result, 'Tagging')


def post_connect(source):
    # Same payload as callbacks_common.post_connect (the video tree still
    # uses that one); sent through the retry queue here because a lost
    # connect leaves the pod off the dashboard for the whole session.
    connection = {
        'source': source,
        'time': str(datetime.now(timezone.utc).replace(tzinfo=None))
    }
    response = _post_with_retry('connect', config.connect_callback(), connection,
                                source, connection['time'].replace(' ', 'T'))
    logging.info('connect callback for %s: %s', source,
                 response.status_code if response is not None else 'failed')
    return response is not None and response.status_code == 200


def post_transcript_features(source, updates):
    # Persist re-scored E&T feature values onto existing transcript rows
    # (post-hoc style recomputation). `updates` = [{id, features:{...}}].
    payload = {'source': source, 'updates': updates}
    return callbacks_common.post_json_ok(_callback_base() + '/transcript_features',
                                         payload, 'transcript features')


def post_disconnect(source):
    return callbacks_common.post_disconnect(config.disconnect_callback(), source)


#Post speaker metrics with transcript data
def post_speaker_transcript_metrics(transcript_data, speakers, participation_scores, internal_cohesion, responsivity, social_impact, newness, communication_density):
    result = {
        'source': transcript_data['source'],
        'start_time': transcript_data['start_time'],
        'end_time': transcript_data['end_time'],
        'transcript': transcript_data['transcript'],
        'direction': transcript_data['doa'],
        'questions': transcript_data['questions'],
        'keywords': transcript_data['keywords'],
        'features': transcript_data['features'],
        'topic_id': transcript_data['topic_id'],
        'speaker_tag': transcript_data['speaker_tag'],
        'speaker_id':transcript_data['speaker_id'],
        # optional passthrough (e.g. {'contested': [aliases]} on overlap)
        'voice_features': transcript_data.get('voice_features'),
        'speakers':speakers,
        'participation_scores': participation_scores,
        'internal_cohesion': internal_cohesion,
        'responsivity': responsivity,
        'social_impact': social_impact,
        'newness': newness,
        'communication_density': communication_density
    }
    response = _post_with_retry('metrics', config.speaker_metrics_callback(), result,
                                result['source'], result['start_time'])
    return response is not None and response.status_code == 200
