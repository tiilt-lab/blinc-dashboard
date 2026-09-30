from flask import session
from app import socketio
from flask_socketio import join_room, leave_room, emit, disconnect
import database
import json
import wrappers

# ------------------------------
# /session
# ------------------------------
@socketio.on('connect', namespace='/session')
def connect_handler():
    if session.get('user', None):
        return True
    else:
        disconnect()
        return False

def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ignore_queue on the per-sid emits below: each goes to exactly one client on
# this process, and with message_queue set every emit is otherwise pickled
# through Redis for a consumer that does not exist (single-process server).
@socketio.on('join_room', namespace='/session')
def join_session(message):
    room = message.get('room', None)
    user = session.get('user', None)
    # Optional last-seen ids from a reconnecting client: replay only rows
    # newer than these instead of the pod's full history on every join.
    last_transcript_id = _int_or_none(message.get('last_transcript_id'))
    last_video_metric_id = _int_or_none(message.get('last_video_metric_id'))
    if not (room and user):
        emit('room_joined', json.dumps({'success': False}), ignore_queue=True)
        return

    # Same reach as the REST read guard (wrappers._session_for): owners see
    # their own sessions, admins read everyone's, supers see all. The old
    # owner-only check made the pod page's transcript/metrics digest silently
    # empty for admins even though every REST endpoint answered them.
    session_model = wrappers._session_for(room, user, write=False)
    if not session_model:
        emit('room_joined', json.dumps({'success': False}), ignore_queue=True)
        return

    join_room(str(room))

    # Signal readiness immediately. The overview page only needs the device
    # list (already fetched over REST) and gates on room_joined, so it should
    # not wait for the transcript/metrics stream below. The pod-detail pages
    # consume the digests progressively as they arrive after this event.
    # The ids are echoed so the client knows whether the digests that follow
    # are a full replay (clear state) or a delta (append).
    emit('room_joined', json.dumps({'success': True,
                                    'last_transcript_id': last_transcript_id,
                                    'last_video_metric_id': last_video_metric_id}),
         ignore_queue=True)

    transcripts = database.get_transcripts(session_id=room, after_id=last_transcript_id)
    videoMetrics = database.get_speaker_video_metrics(session_id=room, after_id=last_video_metric_id)
    # Small first page so the pod page renders content immediately; larger
    # pages after that for throughput.
    first_page_size = 200
    page_size = first_page_size

    # Fetch every speaker-transcript metric for the session in one query
    # and group by transcript id, instead of one query per transcript.
    metrics_by_transcript = {}
    if not transcripts:
        metric_rows = []
    elif last_transcript_id is None:
        metric_rows = database.get_speaker_transcript_metrics(session_id=room)
    else:
        # Delta replay: only the metrics of the rows being sent (a short IN
        # list; the session-wide join would re-read the whole history).
        metric_rows = database.get_speaker_transcript_metrics(transcript_ids=[t.id for t in transcripts])
    for metric in metric_rows:
        metrics_by_transcript.setdefault(metric.transcript_id, []).append(metric.json())

    transcript_speaker_metrics = []
    speaker_video_metrics = []
    for transcript in transcripts:
        if len(transcript_speaker_metrics) == page_size:
            emit('transcript_metrics_digest', json.dumps(transcript_speaker_metrics), ignore_queue=True)
            transcript_speaker_metrics = []
            page_size = 1000
        transcript_speaker_metrics.append({'transcript' : transcript.json(),
                                           'speaker_metrics' : metrics_by_transcript.get(transcript.id, [])})
    emit('transcript_metrics_digest', json.dumps(transcript_speaker_metrics), ignore_queue=True)

    for videometric in videoMetrics:
        if len(speaker_video_metrics) == page_size:
            emit('video_metrics_digest', json.dumps(speaker_video_metrics), ignore_queue=True)
            speaker_video_metrics = []
        speaker_video_metrics.append({'speaker_video_metrics' : videometric.json()})
    emit('video_metrics_digest', json.dumps(speaker_video_metrics), ignore_queue=True)
            

@socketio.on('leave_room', namespace='/session')
def leave_session(message):
    room = message.get('room', None)
    if room:
        leave_room(room)
    emit('room_left', json.dumps({'success': True if room else False}), ignore_queue=True)
