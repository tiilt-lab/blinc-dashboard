import logging
import os
import database
import socketio_helper
from datetime import datetime, timezone
from app import socketio
import json
from device_commands import ConnectionManager
from redis_helper import RedisSessions
import posthoc_queue
import negotiation_coding

def create_session(user_id, name, devices, keyword_list_id, topic_model_id, byod, features, doa, folder, asr=None, live_video_analytics=True, negotiation_coding_enabled=False):
    session, keywords = database.create_session(user_id, keyword_list_id, topic_model_id, name, folder)
    # "Record now, analyse later" for video: only an explicit False turns the
    # live pipeline off; None (older callers) keeps the historic behaviour.
    live_video_analytics = live_video_analytics is not False
    if session.live_video_analytics != live_video_analytics:
        session.live_video_analytics = live_video_analytics
        database.save_changes()
    # Negotiation class: end_session queues an LLM coding run per pod.
    if bool(negotiation_coding_enabled) != bool(session.negotiation_coding):
        session.negotiation_coding = bool(negotiation_coding_enabled)
        database.save_changes()
    if byod:
        session = database.generate_session_passcode(session.id)
    keywords = [keyword.keyword for keyword in keywords]
    config = {
        'server_start': str(session.creation_date),
        'transcribe': True,
        'features': features,
        'keywords': keywords,
        'doa': doa,
        'topic_model': topic_model_id,
        'owner': user_id,
        # Live ASR engine, locked for the session's lifetime (this blob is
        # written once here and deleted at end_session). None -> deployment
        # default (audio service falls back to its config.ini asr=).
        'asr': asr,
        # Read by the video service at 'start' (ProcessingConfig.from_json via
        # the getredissessionconfig callback): False = record every chunk but
        # start no decoder/analytics for the pod; end_session then queues the
        # post-hoc video leg per recorded pod (queue_deferred_video_analysis).
        'live_video_analytics': live_video_analytics,
    }
    RedisSessions.create_session(session.id, config)
    if devices:
        for device in devices:
            pod_join_session(session.id, device)
    return session

def end_session(session_id):
    session = database.get_sessions(id=session_id)
    if not session:
        return False, 'Session does not exist.'
    if session.end_date is not None:
        return False, 'Session is already closed.'
    session.end_date = datetime.now(timezone.utc).replace(tzinfo=None)
    database.save_changes()
    RedisSessions.delete_session(session.id)
    socketio_helper.update_session(session)
    socketio.emit('session_update', json.dumps(session.json()), room=str(session.id), namespace="/session")
    session_devices = database.get_session_devices(session_id=session_id)

    # Update session_devices
    for session_device in session_devices:
        session_device.button_pressed = False
        session_device.removed = True
        # An ended session has no connected pods by definition. Clearing it
        # here self-heals the stale-flag case where a pod died ungracefully
        # and its post_disconnect callback never fired.
        session_device.connected = False
        RedisSessions.delete_device_key(session_device.processing_key)
    database.save_changes()

    # Record-only video sessions: the analysis nobody ran during class is
    # queued now, one post-hoc VIDEO leg per pod that has a recording (no
    # audio leg - live ASR ran as usual). Best effort: a queue problem must
    # not stop the session from ending.
    if session.live_video_analytics is False:
        queue_deferred_video_analysis(session, session_devices)
    # Negotiation classes: the per-utterance LLM coding is queued now, one
    # coding leg per pod with transcripts (no GPU: it runs beside the next
    # class). Best effort, like the video legs.
    if session.negotiation_coding:
        queue_negotiation_coding(session, session_devices)

    # Ping pod devices to stop session
    devices_to_ping = database.get_devices(ids=[session_device.device_id for session_device in session_devices if session_device.device_id is not None])
    for device in devices_to_ping:
        try:
            ConnectionManager.instance.send_command(device.id, {'cmd': 'end'})
        except Exception:
            logging.critical('Session End: Pod ' + str(device.id) + ' was unreachable or failed to respond.')
    return True, session

def video_recordings_dir():
    # src/video_processing/videorecordings, where the live video service
    # writes "<key>_<session>_<pod>_(<ctime>)_orig.<webm|mp4>" (same
    # resolution as routes/session._video_dirs).
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', '..', 'video_processing', 'videorecordings')


def pods_with_video_recordings(session_id, device_ids, recordings_dir):
    """session_device ids (from device_ids) that have a raw video recording
    for session_id on disk. The live recorder names files
    ``<key>_<session_id>_<device_id>_(<ctime>)_orig.<ext>`` (mp4 pods append
    ``_<n>``), so the ``_<session>_<pod>_(`` run is the exact match; the
    numeric prefix of the key is the pod id again and is checked too."""
    try:
        names = os.listdir(recordings_dir)
    except OSError as e:
        logging.warning('deferred video analysis: cannot list %s (%s)', recordings_dir, e)
        return []
    found = set()
    for device_id in {int(d) for d in device_ids}:
        marker = '_{0}_{1}_('.format(int(session_id), device_id)
        prefix = '{0}-'.format(device_id)
        for fn in names:
            if (fn.endswith('.webm') or fn.endswith('.mp4')) and fn.startswith(prefix) and marker in fn:
                found.add(device_id)
                break
    return sorted(found)


def queue_deferred_video_analysis(session, session_devices, recordings_dir=None):
    """End of a live_video_analytics=False session: enqueue a post-hoc VIDEO
    leg (only) for every pod that recorded video. Returns the pod ids queued."""
    pods = pods_with_video_recordings(
        session.id, [sd.id for sd in session_devices],
        recordings_dir or video_recordings_dir())
    if not pods:
        logging.info('session %s ended record-only with no video recordings; nothing to analyse', session.id)
        return []
    try:
        added = posthoc_queue.enqueue(session.id, pods, legs=('video',))
    except Exception:
        logging.exception('session %s: could not queue deferred video analysis for pods %s', session.id, pods)
        return []
    logging.info('session %s ended record-only: queued post-hoc video analysis for pods %s (already queued: %s)',
                 session.id, added, sorted(set(pods) - set(added)))
    return added


def queue_negotiation_coding(session, session_devices):
    """End of a negotiation_coding session: a coding run per pod that has
    transcripts (legs=('coding',) on the post-hoc queue). Returns
    [(pod id, run id)] queued."""
    queued = []
    for session_device in session_devices:
        try:
            if not database.session_device_transcript_count(session_device.id):
                continue
            run = negotiation_coding.queue_coding_run(session.id, session_device.id)
            queued.append((session_device.id, run.id))
        except Exception:
            logging.exception('session %s: could not queue negotiation coding for pod %s',
                              session.id, session_device.id)
    logging.info('session %s ended: queued negotiation coding for (pod, run) %s', session.id, queued)
    return queued


def byod_join_session(name, passcode, collaborators):
    success, session_device, speakers = database.create_byod_session_device(passcode, name, collaborators)
    if success:
        session = database.get_sessions(id=session_device.session_id)
        RedisSessions.create_device_key(session_device.processing_key, session.id)
        socketio_helper.update_session_device(session_device)
        return True, {'session': session.json(), 'session_device': session_device.json(), 'key': session_device.processing_key, 'speakers': [speaker.json() for speaker in speakers]}
    else:
        return False, session_device

def pod_join_session(session_id, pod_id):
    success, session_device = database.create_pod_session_device(session_id, pod_id)
    if success:
        # Add pod redis key and update session.
        RedisSessions.create_device_key(session_device.processing_key, session_device.session_id)
        socketio_helper.update_session_device(session_device)

        # Send message to pod to connect.
        pod = database.get_devices(id=pod_id)
        if pod:
            try:
                sent = ConnectionManager.instance.send_command(pod.id, {'cmd': 'start', 'key': session_device.processing_key})
                if not sent:
                    logging.critical('pod_join_session: Pod {0} was not able to connect to the Audio Processing Service.  Is it in another session?'.format(pod.pod_id))
                sent = ConnectionManager.instance.send_command(pod.id, {'cmd': 'color', 'color': '0xFF0000'})
            except Exception:
                logging.critical('pod_join_session: Pod {0} was unreachable or failed to respond.'.format(pod.id), exc_info=True)

        return True, {'session_device': session_device.json()}
    else:
        return False, {'message': session_device}

def remove_session_device(session_device_id):
    session_device = database.get_session_devices(id=session_device_id)
    if session_device:
        RedisSessions.delete_device_key(session_device.processing_key)
        session_device.removed = True
        # A removed pod is no longer connected; clear the flag so it can't
        # linger if its disconnect callback never fired.
        session_device.connected = False
        database.save_changes()
        if session_device.device_id:
            try:
                ConnectionManager.instance.send_command(session_device.device_id, {'cmd': 'end'})
            except Exception:
                logging.critical('Session End: Pod ' + str(session_device.device_id) + ' was unreachable or failed to respond.')
