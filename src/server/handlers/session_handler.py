import logging
import database
import socketio_helper
from datetime import datetime, timezone
from app import socketio
import json
from device_commands import ConnectionManager
from redis_helper import RedisSessions

def create_session(user_id, name, devices, keyword_list_id, topic_model_id, byod, features, doa, folder, asr=None):
    session, keywords = database.create_session(user_id, keyword_list_id, topic_model_id, name, folder)
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

    # Ping pod devices to stop session
    devices_to_ping = database.get_devices(ids=[session_device.device_id for session_device in session_devices if session_device.device_id is not None])
    for device in devices_to_ping:
        try:
            ConnectionManager.instance.send_command(device.id, {'cmd': 'end'})
        except Exception:
            logging.critical('Session End: Pod ' + str(device.id) + ' was unreachable or failed to respond.')
    return True, session

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
