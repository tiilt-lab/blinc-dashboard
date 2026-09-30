from flask import Blueprint, Response, request, abort, make_response, send_file
import glob
import subprocess
import threading
from utility import sanitize, string_to_bool, json_response, safe_name
from tables.session_device import SessionDevice
from redis_helper import RedisSessions
from tables.session import Session
from utility import batch_video_metrics,batch_transcript_metrics,batch_transcript_video_metrics,synthesized_transcript_video_metrics_by_window
from metrics_windowing import fmt_start_time
from app import socketio
import logging
import database
import json
import watchers
from datetime import datetime, timezone
from handlers import session_handler
import wrappers
import folder_access
import authz
import socketio_helper
import posthoc_state
import posthoc_queue
import csv
import io
import os
import base64
import queue
import time

api_routes = Blueprint('session', __name__)
image_queue_dict = {}
# Serializes synthesized-report writes: concurrent anonymous GETs both saw
# "no report yet" and each inserted a row.
_synthesis_write_lock = threading.Lock()


# A pod named in a URL must belong to the session the caller was authorized
# for — the session guards prove access to session_id only, and device ids are
# sequential/global, so an unchecked device_id is a cross-tenant read/write by
# id. Delegates to the central resource-authz layer so the membership rule
# lives in one place (authz.device_in_session).
_device_in_session = authz.device_in_session

@api_routes.route('/api/v1/sessions', methods=['GET'])
@wrappers.verify_login(public=True)
def get_sessions(user, **kwargs):
    # Admins and supers see every account's sessions (the helpers below treat
    # session_ids=None as unfiltered); everyone else sees their own plus those
    # shared with them through folders. Each row carries 'owner', 'owned' and
    # 'can_modify' so the list can label whose session it is and hide actions
    # the caller may look at but not perform.
    sees_all = user.get('role') in wrappers.ADMIN_ROLES
    folder_levels = folder_access.levels_for_all(
        database.get_folders(), user, database.get_folder_member_levels)
    if sees_all:
        sessions = database.get_sessions()
        scope = None
    else:
        sessions = database.get_sessions(owner_id=user['id'], owner_or_folder_ids=list(folder_levels))
        scope = [s.id for s in sessions]
    video_ids = database.get_session_ids_with_video(session_ids=scope)
    posthoc_ids = database.get_session_ids_with_posthoc(session_ids=scope)
    pod_counts = database.get_session_device_counts(session_ids=scope)
    participant_counts = database.get_session_participant_counts(session_ids=scope)
    running_session_ids = database.get_session_ids_for_devices(posthoc_state.running_device_ids())
    owner_emails = database.get_user_emails() if any(s.owner_id != user['id'] for s in sessions) else {}
    result = []
    for session in sessions:
        data = session.json()
        data['has_video'] = session.id in video_ids
        data['has_posthoc'] = session.id in posthoc_ids
        data['pod_count'] = pod_counts.get(session.id, 0)
        data['participant_count'] = participant_counts.get(session.id, 0)
        data['analysis_running'] = session.id in running_session_ids
        data['owned'] = session.owner_id == user['id']
        # Whether the caller may rename / move / delete it: owner, super, or an
        # editor of its folder. Admins read everything but write only their own.
        data['can_modify'] = (data['owned'] or user.get('role') == 'super'
                              or folder_access.at_least(folder_levels.get(session.folder), folder_access.EDITOR))
        if not data['owned']:
            data['owner'] = owner_emails.get(session.owner_id)
        result.append(data)
    return json_response(result)

@api_routes.route('/api/v1/sessions/<int:session_id>', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def get_session(session, **kwargs):
    return json_response(session.json())

@api_routes.route('/api/v1/sessions/student/passcode/<string:passcode>', methods=['GET'])
def get_session_by_passcode(passcode, **kwargs):
    sessions = database.get_sessions(passcode=passcode, active=True)
    if sessions:
        return json_response([session.json() for session in sessions])
    else:
        return json_response({'message': 'Session  not found.'}, 404)

@api_routes.route('/api/v1/sessions/student/sessionid/<int:session_id>', methods=['GET'])
def get_session_by_id(session_id, **kwargs):
    session = database.get_sessions(id=session_id)
    if session:
        return json_response(session.json())
    else:
        return json_response({'message': 'Session  not found.'}, 404)

@api_routes.route('/api/v1/sessions/student/alias/<string:alias>', methods=['GET'])
def get_sessions_by_alias(alias, **kwargs):
    sessions = database.get_Session_by_alias(alias=alias)
    if sessions:
         return json_response([session.json() for session in sessions])
    else:
        return json_response({'message': 'Session  not found.'}, 404)
    
@api_routes.route('/api/v1/sessions/sessionid/<int:session_id>/student/alias/<string:alias>', methods=['GET'])
def get_session_device_by_alias(session_id,alias, **kwargs):
    sessionsDevices = database.get_Session_device_by_alias(session_id=session_id,alias=alias)
    if sessionsDevices:
         return json_response([device.json() for device in sessionsDevices])
    else:
        return json_response({'message': 'Session  not found.'}, 404)

@api_routes.route('/api/v1/sessions/<int:session_id>', methods=['PUT'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def update_session(session_id, user, **kwargs):
    name = request.json.get('name', None)
    folder= request.json.get('folder', None)
    if name:
        valid, message = Session.verify_fields(name=name)
        if not valid:
            return json_response({'message': message}, 400)
    # None means "not changing the folder" — without the None guard a plain
    # rename ran get_folders(id=None), which returns the user's first folder
    # or None, 404ing every rename for users who own no folders.
    if folder is not None and not wrappers.can_file_session_into(folder, user):
        return json_response({'message': 'Either the folder does not exist or invalid access'}, 404)
    session = database.update_session(session_id, name, folder)
    # Analysis-time settings (applied by the next posthoc run).
    keyword_list_id = sanitize(request.json.get('keywordListId', None))
    topic_model_id = sanitize(request.json.get('topicModelId', None))
    if keyword_list_id is not None or topic_model_id is not None:
        session = database.set_session_analysis_config(
            session_id, user['id'], keyword_list_id, topic_model_id)
    return json_response(session.json())

@api_routes.route('/api/v1/sessions/<int:session_id>', methods=['DELETE'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def delete_session(session_id, **kwargs):
    success =False
    try:
        success = database.delete_session(session_id)
    except Exception:
        # exception-level with stack: info collapsed FK errors and deadlocks
        # into a generic 400 with no trace of what actually failed.
        logging.exception("error occurred while deleting session %s", session_id)
    if success:
        return json_response()
    else:
        return json_response({'message': 'Failed to delete session.'}, 400)

@api_routes.route('/api/v1/sessions', methods=['POST'])
@wrappers.verify_login(public=True)
def create_session(user, **kwargs):
    name = request.json.get('name', 'Session')
    valid, message = Session.verify_fields(name=name)
    if not valid:
        return json_response({'message': message}, 400)
    devices = request.json.get('devices', [])
    keyword_list_id = sanitize(request.json.get('keywordListId', None))
    topic_model_id = sanitize(request.json.get('topicModelId', None))
    byod = request.json.get('byod', False)
    features = request.json.get('features', True)
    doa = request.json.get('doa', False)
    # Live transcription engine, chosen at creation and locked for the
    # session's lifetime (it lives in the session's immutable Redis config).
    asr = request.json.get('asr', None)
    if asr is not None and asr not in ('google-cloud-speech', 'whisper', 'crisperwhisper'):
        return json_response({'message': 'asr must be google-cloud-speech, whisper or crisperwhisper.'}, 400)
    folder = request.json.get('folder', None)
    if folder == -1:
        folder = None
    # The folder picker lists every folder the caller can see, so creation
    # must accept the same set wherever they may file (editor, or admin).
    if folder and not wrappers.can_file_session_into(folder, user):
        return json_response({'message': 'Either the folder does not exist or invalid access'}, 404)
    new_session = session_handler.create_session(user['id'], name, devices, keyword_list_id, topic_model_id, byod, features, doa, folder, asr=asr)
    return json_response(new_session.json())

@api_routes.route('/api/v1/sessions/byod', methods=['POST'])
def byod_join_session(**kwargs):
    name = request.json.get('name', None)
    passcode = sanitize(request.json.get('passcode', None))
    collaborators = request.json.get('collaborators', None)
    if not passcode:
        return json_response({'message': 'Must supply a passcode.'}, 400)
    # A blank name is allowed: the session assigns the next free letter
    # ("Group A", "Group B", ...) in create_byod_session_device.
    if name:
        valid, message = SessionDevice.verify_fields(name=name)
        if not valid:
            return json_response({'message': message}, 400)
    success, data = session_handler.byod_join_session(name, passcode, collaborators)
    if success:
        return json_response(data)
    else:
        return json_response({'message': data}, 400)

# Same as /api/sessions/byod but bypasses the passcode.
@api_routes.route('/api/v1/sessions/<int:session_id>/devices', methods=['POST'])
@wrappers.verify_login(public=True)
def device_join_session(session_id, **kwargs):
    name = request.json.get('name', None)
    valid, message = SessionDevice.verify_fields(name=name)
    if not valid:
        return json_response({'message': message}, 400)
    success, data = database.create_session_device(session_id, name)
    if success:
        RedisSessions.create_device_key(data.processing_key, session_id)
        return json_response({'session_device': data.json(), 'key': data.processing_key})
    else:
        return json_response({'message': data}, 400)

# Lazy remux cache for pod recordings. MediaRecorder .webm files carry no
# duration or seek cues (players show 24:00:00 and cannot scrub), so on first
# request the original is stream-copied through ffmpeg — which writes both —
# into a cache directory, evicted LRU above the size cap.
VIDEO_CACHE_CAP_BYTES = 12 * 1024 ** 3


def _video_dirs():
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        '..', 'video_processing')
    return (os.path.join(base, 'videorecordings'),
            os.path.join(base, 'videorecordings_fixed'))


def _recording_stamp(path):
    # "(...)" timestamp in the recording filename; falls back to mtime order.
    try:
        stamp = path.split('(')[1].split(')')[0]
        return datetime.strptime(stamp, '%a %b %d %H:%M:%S %Y')
    except Exception:
        return datetime.fromtimestamp(os.path.getmtime(path))


def _probe_duration(path):
    # Real duration in seconds, or None. Raw live-recording webm carries a
    # placeholder 24h header, so this is meant for the REMUXED copies (which
    # rewrite the true duration); a >12h result is treated as that placeholder.
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, timeout=60)
        d = float(probe.stdout.strip())
        return d if 0 < d < 12 * 3600 else None
    except Exception:
        return None


def _find_original_videos(session_device_id):
    # All recording segments for a pod in start-time order. A disconnect +
    # reconnect mid-session starts a new file, so one pod can have several.
    recordings_dir, _ = _video_dirs()
    matches = (
        glob.glob(os.path.join(recordings_dir, '{0}-*_orig.webm'.format(session_device_id)))
        or glob.glob(os.path.join(recordings_dir, '{0}-*.webm'.format(session_device_id)))
        or glob.glob(os.path.join(recordings_dir, '{0}-*.mp4'.format(session_device_id)))
    )
    return sorted((os.path.realpath(m) for m in matches), key=_recording_stamp)


def _find_original_video(session_device_id):
    matches = _find_original_videos(session_device_id)
    return matches[0] if matches else None


def _evict_video_cache(cache_dir, incoming_bytes):
    try:
        entries = [(os.path.join(cache_dir, f), os.stat(os.path.join(cache_dir, f)))
                   for f in os.listdir(cache_dir)]
        entries.sort(key=lambda e: e[1].st_atime)  # oldest access first
        total = sum(st.st_size for _, st in entries) + incoming_bytes
        for path, st in entries:
            if total <= VIDEO_CACHE_CAP_BYTES:
                break
            os.remove(path)
            total -= st.st_size
    except Exception as e:
        logging.warning('video cache eviction failed: %s', e)


_remux_locks = {}
_remux_locks_guard = threading.Lock()


def _is_full_range_vp9(path):
    # Some iPhone recorders emit VP9 flagged full range ("pc"). Chrome's
    # hardware VP9 decoder on macOS rejects that flag outright
    # (PIPELINE_ERROR_DECODE right after metadata), so the pod page showed
    # "No recording" for an otherwise valid file. Every other recorder we
    # have seen emits limited range, which plays everywhere.
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name,color_range',
             '-of', 'csv=p=0', path],
            capture_output=True, timeout=60, text=True)
        fields = probe.stdout.strip().split(',')
        return len(fields) >= 2 and fields[0] == 'vp9' and fields[1] == 'pc'
    except Exception:
        return False


def _transcode_to_h264(src, dst):
    # Full-range VP9 -> limited-range H.264 MP4 (plays in every browser,
    # including macOS Chrome's hardware path and Safari). NVENC first
    # (~4x realtime at 1080p on the Quadro), libx264 if the GPU encoder is
    # unavailable. Long recordings are the reason for the generous timeout.
    common_in = ['ffmpeg', '-y', '-v', 'error', '-i', src,
                 '-vf', 'scale=in_range=pc:out_range=tv', '-pix_fmt', 'yuv420p']
    common_out = ['-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', dst]
    encoders = [
        ['-c:v', 'h264_nvenc', '-preset', 'p4', '-rc', 'vbr', '-cq', '28',
         '-b:v', '0', '-maxrate', '5M', '-bufsize', '8M'],
        ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23'],
    ]
    result = None
    for enc in encoders:
        result = subprocess.run(common_in + enc + common_out,
                                capture_output=True, timeout=3600)
        if result.returncode == 0:
            return result
        logging.warning('transcode with %s failed for %s: %s',
                        enc[1], src, result.stderr[-300:])
    return result


def _fixed_video_path(session_device_id):
    # Returns the remuxed (duration+cues fixed) copy, creating it on demand.
    # The remux writes to a temp name and renames atomically, and concurrent
    # requests for the same device serialize on a lock — so a request can never
    # be served a partially written file.
    originals = _find_original_videos(session_device_id)
    if not originals:
        return None
    _, cache_dir = _video_dirs()
    os.makedirs(cache_dir, exist_ok=True)
    src_ext = '.mp4' if originals[0].lower().endswith('.mp4') else '.webm'
    # Full-range VP9 is transcoded rather than remuxed (see _is_full_range_vp9);
    # the .mp4 cache name keeps it distinct from a plain remux of the same file.
    transcode = src_ext == '.webm' and _is_full_range_vp9(originals[0])
    ext = '.mp4' if transcode else src_ext
    # Segment count + total size in the cache name: a reconnect adds a new
    # segment mid-session, which must invalidate the previously cached remux.
    total_bytes = sum(os.path.getsize(o) for o in originals)
    fixed = os.path.join(cache_dir, '{0}-{1}-{2}{3}'.format(
        session_device_id, len(originals), total_bytes, ext))
    # Real per-segment durations, written beside the remux. The concatenated
    # video drops the disconnect gaps between segments, so mapping transcript
    # time to video time needs each segment's true length (see _video_segments).
    seg_json = fixed + '.segments.json'
    # A single segment maps fine without the sidecar (one open-ended segment),
    # but a multi-segment video needs the real per-segment durations to skip the
    # disconnect gaps — so if its remux was cached before the sidecar existed,
    # fall through and regenerate both.
    if os.path.exists(fixed) and (len(originals) == 1 or os.path.exists(seg_json)):
        return fixed
    with _remux_locks_guard:
        lock = _remux_locks.setdefault(session_device_id, threading.Lock())
    with lock:
        # a concurrent request already produced it (same condition as above)
        if os.path.exists(fixed) and (len(originals) == 1 or os.path.exists(seg_json)):
            return fixed
        _evict_video_cache(cache_dir, total_bytes)
        tmp = '{0}.tmp{1}'.format(fixed, ext)
        seg_files = []
        durations = []
        try:
            if len(originals) == 1:
                if transcode:
                    result = _transcode_to_h264(originals[0], tmp)
                else:
                    result = subprocess.run(
                        ['ffmpeg', '-y', '-v', 'error', '-i', originals[0], '-c', 'copy', tmp],
                        capture_output=True, timeout=300)
                if result.returncode != 0:
                    logging.warning('ffmpeg remux failed for %s: %s', originals, result.stderr[-500:])
                    return originals[0]  # fall back to the raw first segment
                durations = [_probe_duration(tmp)]
            else:
                # Stitch all segments (same device, same codecs). The raw
                # files carry a placeholder 24h duration, which the concat
                # demuxer would use as the splice offset — so each segment
                # is first remuxed alone (rewriting its real duration), then
                # concatenated. The gap between segments is not represented;
                # playback runs continuously.
                for i, original in enumerate(originals):
                    seg = '{0}.seg{1}{2}'.format(fixed, i, src_ext)
                    result = subprocess.run(
                        ['ffmpeg', '-y', '-v', 'error', '-i', original, '-c', 'copy', seg],
                        capture_output=True, timeout=300)
                    if result.returncode != 0:
                        logging.warning('segment remux failed for %s: %s', original, result.stderr[-500:])
                        return originals[0]
                    seg_files.append(seg)
                    durations.append(_probe_duration(seg))
                listfile = '{0}.list'.format(fixed)
                seg_files.append(listfile)
                with open(listfile, 'w') as f:
                    for seg in seg_files[:-1]:
                        f.write("file '{0}'\n".format(seg.replace("'", r"'\''")))
                # With a transcode pending, concatenate into an intermediate
                # in the source container first; tmp is the final .mp4.
                cat_out = tmp
                if transcode:
                    cat_out = '{0}.cat{1}'.format(fixed, src_ext)
                    seg_files.append(cat_out)
                result = subprocess.run(
                    ['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
                     '-i', listfile, '-c', 'copy', cat_out],
                    capture_output=True, timeout=300)
                if result.returncode != 0:
                    logging.warning('ffmpeg concat failed for %s: %s', originals, result.stderr[-500:])
                    return originals[0]
                if transcode:
                    result = _transcode_to_h264(cat_out, tmp)
                    if result.returncode != 0:
                        return originals[0]
            os.replace(tmp, fixed)  # atomic: readers see nothing or all of it
            try:
                with open(seg_json, 'w') as f:
                    json.dump(durations, f)
            except OSError as e:
                logging.warning('could not write segment sidecar %s: %s', seg_json, e)
        except Exception as e:
            logging.warning('remux error for %s: %s', originals, e)
            return originals[0]
        finally:
            for leftover in [tmp] + seg_files:
                try:
                    os.remove(leftover)
                except OSError:
                    pass
    return fixed


def _video_start_offset(session_id, session_device_id):
    # Seconds between session start and recording start, parsed from the
    # recording filename's "(...)" timestamp — needed to align video time with
    # transcript start_time (both are session-relative).
    original = _find_original_video(session_device_id)
    session_model = database.get_sessions(id=session_id, first=True)
    if original is None or session_model is None:
        return 0.0
    try:
        stamp = original.split('(')[1].split(')')[0]
        recording_start = datetime.strptime(stamp, '%a %b %d %H:%M:%S %Y')
        return max((recording_start - session_model.creation_date).total_seconds(), 0.0)
    except Exception:
        return 0.0


def _video_segments(session_id, session_device_id):
    # The recording as the player sees it: one entry per segment, in play order,
    #   {'start': session-relative time of the segment's FIRST frame,
    #    'video_start': where it begins in the concatenated video (gaps removed),
    #    'duration': real length in seconds}.
    # A pod that dropped and rejoined has several segments; the concatenated
    # video runs them back-to-back, so 'start' (which includes the disconnect
    # gap) and 'video_start' (which does not) diverge. The player maps between
    # transcript time and video time across these.
    originals = _find_original_videos(session_device_id)
    session_model = database.get_sessions(id=session_id, first=True)
    if not originals or session_model is None:
        return []
    # Ensure the remux + duration sidecar exist, then read the durations.
    fixed = _fixed_video_path(session_device_id)
    durations = []
    if fixed:
        try:
            with open(fixed + '.segments.json') as f:
                durations = json.load(f)
        except (OSError, ValueError):
            durations = []

    segments = []
    video_cursor = 0.0
    for i, original in enumerate(originals):
        start = max((_recording_stamp(original)
                     - session_model.creation_date).total_seconds(), 0.0)
        dur = durations[i] if i < len(durations) and durations[i] else None
        if dur is None:
            # No probed length (probe failed, or sidecar missing on a raw
            # fallback). Approximate from the next segment's start; the last
            # segment is left open-ended.
            if i + 1 < len(originals):
                nxt = max((_recording_stamp(originals[i + 1])
                           - session_model.creation_date).total_seconds(), 0.0)
                dur = max(nxt - start, 0.0)
            else:
                dur = None
        segments.append({'start': start, 'video_start': video_cursor,
                         'duration': dur})
        if dur is not None:
            video_cursor += dur
    return segments


def prewarm_video_cache():
    # Remux every pod recording into the fixed cache in the background at
    # server start (newest first, within the LRU cap), so the first viewer of
    # any video never waits behind "Preparing video...".
    def _worker():
        try:
            recordings_dir, _ = _video_dirs()
            files = sorted(glob.glob(os.path.join(recordings_dir, '*-*.webm')),
                           key=os.path.getmtime, reverse=True)
            _, cache_dir = _video_dirs()
            done = 0
            for path in files:
                # Stop once the cache is ~80% full — remuxing more would only
                # churn the LRU eviction.
                try:
                    used = sum(os.path.getsize(os.path.join(cache_dir, f))
                               for f in os.listdir(cache_dir))
                except OSError:
                    used = 0
                if used > VIDEO_CACHE_CAP_BYTES * 0.8:
                    break
                name = os.path.basename(path)
                device_part = name.split('-', 1)[0]
                if not device_part.isdigit():
                    continue
                _fixed_video_path(int(device_part))
                done += 1
            logging.info('video cache pre-warm finished (%d recordings considered)', done)
        except Exception as e:
            logging.warning('video cache pre-warm failed: %s', e)
    t = threading.Thread(target=_worker, daemon=True, name='video-cache-prewarm')
    t.start()


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/video', methods=['GET'])
@wrappers.verify_device_read_access
def get_session_device_video(session_id, session_device_id, **kwargs):
    # Stream a pod's recorded video for playback beside the transcript.
    # conditional=True lets Flask honor HTTP Range requests so the player can
    # seek. Access mirrors the pod's other media (the /client metric +
    # transcript routes are not owner-gated).
    path = _fixed_video_path(session_device_id)
    if path is None:
        abort(404)
    mimetype = 'video/mp4' if path.lower().endswith('.mp4') else 'video/webm'
    return send_file(path, mimetype=mimetype, conditional=True)


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/video/info', methods=['GET'])
@wrappers.verify_device_read_access
def get_session_device_video_info(session_id, session_device_id, **kwargs):
    # Playback metadata: real duration (ffprobe of the remuxed copy) and the
    # session-relative start offset, so the player can map video time <->
    # transcript time and build subtitle cues.
    path = _fixed_video_path(session_device_id)
    if path is None:
        return json_response({'message': 'No recording.'}, 404)
    duration = None
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, timeout=60)
        duration = float(probe.stdout.strip())
    except Exception as e:
        logging.warning('ffprobe failed for %s: %s', path, e)
    return json_response({
        'duration': duration,
        # offset stays for the single-segment fast path and older clients;
        # segments is the full layout the player maps against (see below).
        'offset': _video_start_offset(session_id, session_device_id),
        'segments': _video_segments(session_id, session_device_id),
    })


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/video_attribution', methods=['GET'])
@wrappers.verify_device_read_access
def get_video_attribution(session_id, session_device_id, **kwargs):
    # Fuse the TalkNCE active-speaker artifact (written by the video posthoc's
    # optional ASD pass) with the pod's transcripts: for each utterance, which
    # on-camera face was talking, and by what margin. Computed at read time so
    # audio and video posthoc can complete in either order and no schema
    # changes are needed.
    original = _find_original_video(session_device_id)
    if original is None:
        return json_response({'message': 'No recording.'}, 404)
    artifact = os.path.splitext(original)[0] + '_asd.json'
    if not os.path.isfile(artifact):
        return json_response({'message': 'No active-speaker analysis for this pod. '
                                         'Re-run analysis with ASD enabled.'}, 404)
    try:
        with open(artifact) as f:
            asd = json.load(f)
    except Exception as e:
        logging.warning('video_attribution: bad artifact %s: %s', artifact, e)
        return json_response({'message': 'Active-speaker artifact unreadable.'}, 500)

    fps = float(asd.get('fps') or 25)
    offset = _video_start_offset(session_id, session_device_id)
    # Collapse tracks onto identities (unidentified tracks keep a track key).
    by_identity = {}
    for tr in asd.get('tracks', []):
        key = tr.get('identity') or 'track{0}'.format(tr.get('id'))
        dest = by_identity.setdefault(key, {})
        for frame, score in zip(tr.get('frames', []), tr.get('scores', [])):
            dest[frame] = max(score, dest.get(frame, 0.0))

    rows = []
    for t in database.get_transcripts(session_device_id=session_device_id):
        v0 = t.start_time - offset
        v1 = v0 + max(1.0, float(t.length or 1.0))
        f0, f1 = int(v0 * fps), int(v1 * fps)
        if f1 <= 0:
            continue
        means = {}
        for who, frames in by_identity.items():
            vals = [frames[f] for f in range(f0, f1) if f in frames]
            if len(vals) >= max(1, (f1 - f0) * 0.3):
                means[who] = sum(vals) / len(vals)
        if not means:
            continue
        ranked = sorted(means.items(), key=lambda kv: -kv[1])
        margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
        rows.append({
            'transcript_id': t.id,
            'start_time': t.start_time,
            'video_speaker': ranked[0][0],
            'margin': round(margin, 4),
            'scores': {k: round(v, 4) for k, v in means.items()},
        })
    return json_response({'fps': fps, 'offset': offset, 'rows': rows})


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/posthoc_completed', methods=['POST'])
@wrappers.verify_login(public=True)
def mark_posthoc_completed(session_id, session_device_id, user, **kwargs):
    # Recorded when a post-hoc re-analysis finishes for a pod, so the sessions
    # list can show which discussions have been re-analyzed. Needs write access
    # (owner, super, or an editor of the session's folder).
    if not wrappers.session_write_allowed(session_id, user):
        return json_response({'message': 'Session not found.'}, 404)
    if _device_in_session(session_device_id, session_id) is None:
        return json_response({'message': 'Session device not found.'}, 404)
    # Optional per-run model provenance blob (e.g. {asr, embedder, diarizer,
    # scorer, emotion, attention, ...}); absent for older callers.
    body = request.get_json(silent=True) or {}
    models = body.get('models')
    if database.mark_session_device_posthoc(session_device_id, models=models):
        return json_response({'ok': True})
    return json_response({'message': 'Device not found.'}, 404)


@api_routes.route('/api/v1/sessions/pod', methods=['POST'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access_json
def pod_join_session(**kwargs):
    session_id = request.json.get('sessionId', None)
    pod_id = request.json.get('podId', None)
    if not session_id or not pod_id:
        return json_response({'message': 'Must supply sessionId and PodId.'}, 400)
    success, response = session_handler.pod_join_session(session_id, pod_id)
    if success:
        return json_response(response)
    else:
        return json_response(response, 400)

@api_routes.route('/api/v1/sessions/<int:session_id>/stop', methods=['POST'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def end_session(session_id, **kwargs):
    success, data = session_handler.end_session(session_id)
    if success:
        return json_response(data.json())
    else:
        return json_response({'message': data}, 400)

@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:device_id>/transcripts', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def session_device_transcripts(session_id, device_id, **kwargs):
    transcripts = database.get_transcripts(session_device_id=device_id)
    return json_response([transcript.json() for transcript in transcripts])

# Guarded by device rather than session: the URL names no session_id, so the
# session guard raised KeyError on every call and this returned 500.
# The /client variant is the same endpoint for the BYOD client, which calls it
# with its pod key rather than a login.
@api_routes.route('/api/v1/sessions/devices/<int:device_id>/speakers/<int:speaker_id>/transcripts', methods=['GET'])
@api_routes.route('/api/v1/sessions/devices/<int:device_id>/speakers/<int:speaker_id>/transcripts/client', methods=['GET'])
@wrappers.verify_device_read_access
def speaker_id_transcripts_for(device_id, speaker_id, **kwargs):
    # The guard authorises the pod, so the speaker must belong to it — otherwise
    # one pod's key or session would read any speaker's transcripts by id.
    if not database.get_speakers(id=speaker_id, session_device_id=device_id):
        return json_response({'message': 'Does not exist.'}, 404)
    transcripts = database.get_transcripts(speaker_id=speaker_id)
    return json_response([transcript.json() for transcript in transcripts])

# Open by design: the anonymous student-dashboard and expert-rating flows
# (no accounts, no processing key) read transcripts through this route. Any
# gate here must come with a credential those flows can hold — a product
# decision, not a missing decorator.
@api_routes.route('/api/v1/devices/<int:device_id>/transcripts/client', methods=['GET'])
def session_device_transcripts_for_client(device_id, **kwargs):
    transcripts = database.get_transcripts(session_device_id=device_id)
    return json_response([transcript.json() for transcript in transcripts])


# Shared assembly for the transcript+metrics endpoints below. One batched
# query instead of one per transcript.
def _with_speaker_metrics(transcripts):
    metrics_by_transcript = {}
    if transcripts:
        for m in database.get_speaker_transcript_metrics(transcript_ids=[t.id for t in transcripts]):
            metrics_by_transcript.setdefault(m.transcript_id, []).append(m.json())
    return [{'transcript': transcript.json(),
             'speaker_metrics': metrics_by_transcript.get(transcript.id, [])}
            for transcript in transcripts]


@api_routes.route('/api/v1/devices/<int:device_id>/transcriptspeakermetrics/client', methods=['GET'])
@wrappers.verify_device_read_access
def session_device_transcript_speaker_metrics_for_client(device_id, **kwargs):
    transcripts = database.get_transcripts(session_device_id=device_id)
    return json_response(_with_speaker_metrics(transcripts))

@api_routes.route('/api/v1/devices/<int:device_id>/videometrics/client', methods=['GET'])
@wrappers.verify_device_read_access
def session_device_videometrics_for_client(device_id, **kwargs):
    videometrics = database.get_speaker_video_metrics(session_device_id=device_id)
    return json_response([videometric.json() for videometric in videometrics])


@api_routes.route('/api/v1/session/<int:session_id>/transcripts/student/<string:alias>', methods=['GET'])
@api_routes.route('/api/v1/session/<int:session_id>/sessiondevice/<int:device_id>/transcripts/student/<string:alias>', methods=['GET'])
def session_transcripts_for_client(session_id, alias, device_id=None, **kwargs):
    transcripts = database.get_transcripts_by_session_alias(session_id=session_id,speaker_tag=alias,device_id=device_id)
    return json_response(_with_speaker_metrics(transcripts))


@api_routes.route('/api/v1/session/<int:session_id>/videometrics/student/<string:alias>', methods=['GET'])
@api_routes.route('/api/v1/session/<int:session_id>/sessiondevice/<int:device_id>/videometrics/student/<string:alias>', methods=['GET'])
def session_videometrics_for_client(session_id, alias, device_id=None, **kwargs):
    videoMetrics = database.get_speaker_video_metrics_by_session_alias(session_id=session_id,student_username=alias,device_id=device_id)
    return json_response([videometric.json() for videometric in videoMetrics])


@api_routes.route('/api/v1/devices/<int:device_id>/transcripts/speaker_metrics', methods=['GET'])
@wrappers.verify_device_read_access
def session_device_speaker_metrics(device_id, **kwargs):
    speaker_metrics = database.get_speaker_transcript_metrics(session_device_id=device_id)
    # logging.info(f'Received speaker metrics from database{speaker_metrics}')
    return json_response([speaker_metric.json() for speaker_metric in speaker_metrics])

@api_routes.route('/api/v1/sessions/<int:session_id>/transcripts/speaker_metrics', methods=['POST'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def session_transcript_speaker_metrics(session_id, **kwargs):
    transcripts = database.get_transcripts(session_id=session_id)
    return json_response(json.dumps(_with_speaker_metrics(transcripts)))

@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:device_id>/speakers', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def session_device_speakers(session_id, device_id, **kwargs):
    if _device_in_session(device_id, session_id) is None:
        return json_response({'message': 'Session device not found.'}, 404)
    speakers = database.get_speakers(session_device_id=device_id)
    return json_response([speaker.json() for speaker in speakers])

@api_routes.route('/api/v1/sessions/<int:session_id>/speakers', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def session_speakers(session_id, **kwargs):
    speakers = database.get_speakers(session_id=session_id)
    return json_response([speaker.json() for speaker in speakers])

@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:device_id>/keywords', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def session_device_keywords(session_id, device_id, **kwargs):
    if _device_in_session(device_id, session_id) is None:
        return json_response({'message': 'Session device not found.'}, 404)
    keywords = database.get_keyword_usages(session_device_id=device_id)
    return json_response([keyword.json() for keyword in keywords])

@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:session_device_id>', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def session_device(session_id, session_device_id, **kwargs):
        # `processing_key` was a required positional nothing supplied — every
        # call 500ed with a TypeError before reaching the body.
        session_device = _device_in_session(session_device_id, session_id)
        if session_device is None:
            return json_response({'message': 'Session device not found.'}, 404)
        return json_response(session_device.json())

def _pod_ids_with_recordings():
    # session_device_ids that have a raw recording file on disk. Short
    # recordings can finish before any transcript or video-metric row lands
    # in the DB, but the recording itself is real data (playable, and
    # post-hoc analyzable) — without this check the overview called such
    # pods "No data" and refused to open them.
    recordings_dir, _ = _video_dirs()
    ids = set()
    try:
        for fn in os.listdir(recordings_dir):
            if fn.endswith('.webm') or fn.endswith('.mp4'):
                prefix = fn.split('-', 1)[0]
                if prefix.isdigit():
                    ids.add(int(prefix))
    except OSError:
        pass
    return ids


@api_routes.route('/api/v1/sessions/<int:session_id>/devices', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def session_devices(session_id, **kwargs):
        # The overview polls this every 2s while open; the auto-close task
        # treats a recently-watched session as active.
        watchers.mark_watched(session_id)
        devices = database.get_session_devices(session_id=session_id)
        # Per-pod duration (seconds, derived from transcript timestamps since
        # session_device stores no start/end) + participant count.
        durations = database.get_pod_durations(session_id)
        speaker_counts = database.get_pod_speaker_counts(session_id)
        video_pods = database.get_pod_video_presence(session_id)
        recorded = _pod_ids_with_recordings()
        result = []
        for device in devices:
            data = device.json()
            data['duration'] = durations.get(device.id)
            if data['duration'] is None:
                data['duration'] = posthoc_state.last_duration(device.id)
            data['speaker_count'] = speaker_counts.get(device.id, 0)
            # True if the pod captured any usable data (transcript, video
            # metrics, or a recording file); pods that truly recorded
            # nothing should be flagged, not analyzed.
            data['has_data'] = (durations.get(device.id) is not None) \
                or (device.id in video_pods) or (device.id in recorded)
            data['has_video'] = (device.id in video_pods) or (device.id in recorded)
            data['analysis_running'] = posthoc_state.is_running(device.id)
            result.append(data)
        return json_response(result)


@api_routes.route('/api/v1/sessions/<int:session_id>/triage', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def get_session_triage(session_id, **kwargs):
    # Live per-pod alert flags for the instructor overview during class.
    return json_response(database.get_session_triage(session_id))


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/dynamics', methods=['GET'])
@wrappers.verify_device_read_access
def get_pod_dynamics(session_id, session_device_id, **kwargs):
    # Conversation dynamics for a pod: per-speaker turns/speaking-share (equity)
    # + the who-follows-whom response network + Tier-1 talk metrics
    # (equity timeline, silences, handoffs vs interruptions, questions).
    result = database.get_conversation_dynamics(session_device_id)
    result['talk'] = database.get_talk_metrics(session_device_id)
    return json_response(result)


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/posthoc_running', methods=['GET'])
@wrappers.verify_device_read_access
def get_posthoc_running(session_id, session_device_id, **kwargs):
    # Reliable "is an analysis running for this pod" for the dashboard. The
    # trigger panel's websocket probes ask the processing services directly,
    # but a busy run starves their event loops for minutes at a time, so the
    # probes time out and the page shows nothing. This reads the capture
    # server's own callback-fed state instead.
    return json_response(posthoc_state.running_detail(session_device_id))


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/gaze_overlays', methods=['GET'])
@wrappers.verify_device_read_access
def get_gaze_overlays(session_id, session_device_id, **kwargs):
    # Gaze-model overlay geometry (head boxes / gaze points / focused-object
    # boxes, normalized coords) for the video player's overlay toggles. Only
    # exists for pods whose video analysis ran after overlay capture landed.
    recordings_dir, _ = _video_dirs()
    path = os.path.join(recordings_dir,
                        '{0}-gaze_overlays.jsonl'.format(session_device_id))
    records = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except FileNotFoundError:
        pass
    return json_response({'records': records})


@api_routes.route('/api/v1/devices/<int:device_id>/heartrate/client', methods=['POST'])
@wrappers.verify_device_read_access
def add_device_heart_rate(device_id, **kwargs):
    # Heart-rate/RR ingest from the byod join page (Polar straps over Web
    # Bluetooth). Samples carry the client's epoch-ms clock; convert to the
    # session-relative seconds every other metric table uses, correcting for
    # client/server clock skew via the batch's client_now.
    session_device = kwargs.get('session_device') or database.get_session_devices(id=device_id)
    content = request.get_json() or {}
    samples = content.get('samples', [])
    if not isinstance(samples, list) or not samples:
        return json_response({'added': 0})
    if len(samples) > 500:
        samples = samples[:500]
    parent_session = database.get_sessions(id=session_device.session_id)
    session_start_ms = parent_session.creation_date.timestamp() * 1000.0
    try:
        skew_ms = datetime.now(timezone.utc).replace(tzinfo=None).timestamp() * 1000.0 - float(content.get('client_now'))
    except (TypeError, ValueError):
        skew_ms = 0.0
    rows = []
    for s in samples:
        try:
            hr = int(s['hr'])
            t_ms = float(s['t']) + skew_ms
        except (KeyError, TypeError, ValueError):
            continue
        rr = s.get('rr') or []
        rr_text = ','.join(str(int(v)) for v in rr if isinstance(v, (int, float)))[:64]
        rows.append({
            'speaker_id': s.get('speaker_id'),
            # sanitize('') returns None — slicing it raised a TypeError that
            # aborted the whole batch (up to 500 samples) on one bad sample.
            'speaker_alias': (sanitize(str(s.get('alias', ''))) or 'Unknown')[:64],
            'sensor_name': (sanitize(str(s.get('sensor', ''))) or '')[:64],
            'time_stamp': max(0, int((t_ms - session_start_ms) / 1000.0)),
            'heart_rate': hr,
            'rr_ms': rr_text,
        })
    added = database.add_speaker_hr_metrics_batch(session_device.id, rows) if rows else []
    return json_response({'added': len(added)})


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/heartrate', methods=['GET'])
@wrappers.verify_device_read_access
def get_pod_heart_rate(session_id, session_device_id, **kwargs):
    # Same open-read trust level as the joint_attention endpoint below.
    rows = database.get_speaker_hr_metrics(session_device_id=session_device_id)
    return json_response([row.json() for row in rows])


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/joint_attention', methods=['GET'])
@wrappers.verify_device_read_access
def get_pod_joint_attention(session_id, session_device_id, **kwargs):
    # Joint visual attention: >=2 members' gaze on the same target, computed
    # from the per-person Gaze-LLE focus targets the video pipeline stores.
    return json_response(database.get_joint_attention(session_device_id))


@api_routes.route('/api/v1/sessions/upload_video', methods=['POST'])
@wrappers.verify_login(public=True)
def upload_video_session(user, **kwargs):
    # Analyze a user-provided recording: create a session + one pod, place the
    # video (and an extracted 16k mono f32 wav) exactly where the post-hoc
    # services look for pod recordings, then auto-queue the full analysis.
    # No enrolled speakers, so diarization yields generic SPEAKER_NN labels.
    f = request.files.get('video')
    if f is None:
        return json_response({'message': 'video file required'}, 400)
    name = sanitize(request.form.get('name') or 'Uploaded video')[:60]
    session_obj = session_handler.create_session(user['id'], name, [], None, None, True, True, False, None)
    ok, device = database.create_session_device(session_obj.id, 'Uploaded')
    if not ok:
        return json_response({'message': str(device)}, 400)
    key = device.processing_key
    ctime = time.ctime()
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..')
    vid_dir = os.path.join(base, 'video_processing', 'videorecordings')
    aud_dir = os.path.join(base, 'audio_processing', 'recordings')
    ext = (f.filename or '').rsplit('.', 1)[-1].lower()
    ext = ext if ext in ('webm', 'mp4') else 'webm'
    vid_path = os.path.join(vid_dir, '{0}_{1}_{2}_({3})_orig.{4}'.format(key, session_obj.id, device.id, ctime, ext))
    f.save(vid_path)
    wav_path = os.path.join(aud_dir, '{0}({1})_orig.wav'.format(key, ctime))
    try:
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', vid_path, '-vn',
                        '-ar', '16000', '-ac', '1', '-acodec', 'pcm_f32le', wav_path],
                       check=True, timeout=600)
    except Exception as e:
        return json_response({'message': 'audio extraction failed: %s' % e}, 400)
    # close the session (uploads are not live) and queue the analysis. The
    # old hasattr(database, 'update_session_status') guard referenced a
    # function that never existed, silently leaving every uploaded session
    # "live" (no end_date, redis config present).
    session_obj.end_date = datetime.now(timezone.utc).replace(tzinfo=None)
    database.save_changes()
    try:
        from redis_helper import RedisSessions as _RS
        _RS.delete_session(session_obj.id)
    except Exception as e:
        logging.warning('upload session redis cleanup failed: %s', e)
    posthoc_queue.enqueue(session_obj.id, [device.id])
    return json_response({'session_id': session_obj.id, 'device_id': device.id, 'queued': True})


# Global analysis-queue view (the queue itself is one worker across all
# sessions). Jobs are scoped to sessions the caller can read (own or shared
# through a folder) unless they're an admin; names are joined in so the UI needs no extra requests.
@api_routes.route('/api/v1/posthoc_queue', methods=['GET'])
@wrappers.verify_login()
def global_posthoc_queue(**kwargs):
    user = kwargs['user']
    is_admin = user.get('role') in wrappers.ADMIN_ROLES
    jobs = posthoc_queue.status()
    session_cache = {}
    result = []
    for job in jobs:
        sid = job['session_id']
        if sid not in session_cache:
            session_cache[sid] = database.get_sessions(id=sid)
        session = session_cache[sid]
        if not session:
            continue
        if not is_admin and not wrappers._session_for(sid, user, write=False):
            continue
        device = database.get_session_devices(id=job['device_id'])
        entry = dict(job)
        entry['session_name'] = session.name
        entry['device_name'] = (device.name if device and device.name else 'Pod {0}'.format(job['device_id']))
        result.append(entry)
    return json_response(result)


@api_routes.route('/api/v1/sessions/<int:session_id>/posthoc_queue', methods=['POST'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def enqueue_posthoc(session_id, **kwargs):
    # Queue full analyses for several pods; they run one pod at a time.
    body = request.get_json(silent=True) or {}
    device_ids = body.get('device_ids') or []
    if not device_ids:
        return json_response({'message': 'device_ids required'}, 400)
    # Only pods that actually belong to this session: the run context
    # (transcript time anchoring, keywords) comes from the session, so a
    # cross-session enqueue silently mis-processes the pod.
    valid = {d.id for d in database.get_session_devices(session_id=session_id)}
    rejected = [int(d) for d in device_ids if int(d) not in valid]
    device_ids = [int(d) for d in device_ids if int(d) in valid]
    if rejected:
        logging.warning('posthoc enqueue: rejected devices %s not in session %s', rejected, session_id)
    if not device_ids:
        return json_response({'message': 'No device_ids belong to this session.'}, 400)
    added = posthoc_queue.enqueue(session_id, device_ids, models=body.get('models'))
    return json_response({'queued': added, 'status': posthoc_queue.status(session_id)})


@api_routes.route('/api/v1/sessions/<int:session_id>/posthoc_queue/stop', methods=['POST'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def stop_posthoc_queue(session_id, **kwargs):
    # Cancel: drop queued jobs and tell the services to stop this session's
    # running pods (best-effort; services also apply on their next restart).
    cleared = posthoc_queue.clear_pending()
    cancelled = 0
    try:
        import asyncio, websockets
        async def _cancel(port, did):
            async with websockets.connect('ws://127.0.0.1:%s' % port, open_timeout=5) as ws:
                await ws.send(json.dumps({'type': 'cancel_posthoc', 'sessiondeviceid': did}))
                await asyncio.wait_for(ws.recv(), timeout=5)
        for did in list(posthoc_state.running_device_ids()):
            for port in (os.getenv('DC_AUDIO_POSTHOC_WS_PORT', '9015'), os.getenv('DC_VIDEO_POSTHOC_WS_PORT', '9014')):
                try:
                    asyncio.run(_cancel(port, did)); cancelled += 1
                except Exception:
                    pass
            posthoc_state.mark_done(did)
    except Exception as e:
        logging.warning('stop_posthoc_queue cancel pass failed: %s', e)
    return json_response({'cleared': cleared, 'cancel_signals': cancelled})


@api_routes.route('/api/v1/sessions/<int:session_id>/posthoc_queue', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def posthoc_queue_status(session_id, **kwargs):
    return json_response(posthoc_queue.status(session_id))


@api_routes.route('/api/v1/students/<username>/longitudinal', methods=['GET'])
def get_student_longitudinal(username, **kwargs):
    # A student's per-session speaking share + attention across the term.
    # Open like the dashboard's other per-student routes (transcripts/student,
    # videometrics/student): the anonymous student reflection dashboard is the
    # caller, and verify_login(public=True) still 401s without a session.
    return json_response(database.get_student_longitudinal(username))

# Open by design: the anonymous expert-rating flow resolves its assigned pod
# through this route with no login and no processing key. It returns only
# device metadata (name, session id), not recorded data.
@api_routes.route('/api/v1/devices/<int:session_device_id>/session_device', methods=['GET'])
def session_device_by_id(session_device_id, **kwargs):
    device = database.get_session_devices(id=session_device_id)
    if device is None:
        return json_response({'message': 'Does not exist.'}, 404)
    return json_response(device.json())


def _face_thumb_path(alias):
    # Representative face crop saved by the video pipeline (per student alias).
    # Use the shared safe_name guard (charset + dot-only + basename), not a
    # bare basename() — that was the weakest of the three path guards.
    safe = safe_name(alias)
    if safe is None:
        return None
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        '..', 'video_processing', 'facial_embeddings', 'thumbnails')
    return os.path.join(base, "{0}.jpg".format(safe))


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/facethumb/<alias>', methods=['GET'])
@wrappers.verify_device_read_access
def get_face_thumbnail(session_id, session_device_id, alias, **kwargs):
    # Face crop for a recognized student, scoped to a session+device path (same
    # access model as the pod video). 404 until the pod is (re)processed.
    path = _face_thumb_path(alias)
    if path is None or not os.path.exists(path):
        return ('', 404)
    resp = make_response(send_file(path, mimetype='image/jpeg'))
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp

@api_routes.route('/api/v1/help_button', methods=['POST'])
@wrappers.verify_login(allow_key=True)
def set_help_button(**kwargs):
    session_device_id = request.json.get('id', None)
    state = request.json.get('activated', False)
    if not session_device_id:
        return json_response({'message': 'Session device ID must be provided.'}, 400)

    session_device = database.get_session_devices(id=session_device_id)
    if session_device:
        if session_device.button_pressed != state:
            session_device.button_pressed = state
            database.save_changes()
            room_name = str(session_device.session_id)
            socketio.emit('device_update', json.dumps(session_device.json()), room=room_name, namespace="/session")
        return json_response()
    return json_response({'message': 'Session device not found.'}, 404)

@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:device_id>/name', methods=['PUT'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def rename_session_device(session_id, device_id, **kwargs):
    # Inline group rename from the pods overview (same UX as session rename).
    name = sanitize((request.json or {}).get('name', '') or '').strip()
    if not name:
        return json_response({'message': 'Name must not be empty.'}, 400)
    if len(name) > 64:
        return json_response({'message': 'Name must not exceed 64 characters.'}, 400)
    session_device = _device_in_session(device_id, session_id)
    if session_device is None:
        return json_response({'message': 'Pod not found in this session.'}, 404)
    session_device.name = name
    database.save_changes()
    # Live viewers of the session get the new name immediately.
    socketio.emit('device_update', json.dumps(session_device.json()),
                  room=str(session_device.session_id), namespace="/session")
    return json_response(session_device.json())


@api_routes.route('/api/v1/sessions/<int:session_id>/passcode', methods=['POST'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def set_session_passcode(session_id, **kwargs):
    state = request.json.get('state', None)
    if not state in ['lock', 'unlock', 'refresh']:
        return json_response({'message': 'Invalid passcode state.'}, 400)
    session = database.get_sessions(id=session_id)
    if session:
        if state == 'lock':
            session.passcode = None
            database.save_changes()
        elif state in ['unlock', 'refresh']:
            database.generate_session_passcode(session.id)
        socketio_helper.update_session(session)
        return json_response(session.json())
    return json_response({'message': 'Session not found.'}, 404)

@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:session_device_id>', methods=['DELETE'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def remove_device_from_session(session_id, session_device_id, **kwargs):
    # verify_session_access only proves the caller owns session_id; the pod id
    # is from the URL and must belong to THAT session, or a user who owns any
    # one session could end/delete another tenant's live pod by id (the
    # sibling rename/get routes already check this).
    target = _device_in_session(session_device_id, session_id)
    if target is None:
        return json_response({'message': 'Session device not found.'}, 404)
    delete = string_to_bool(request.args.get('delete', 'false'))
    if delete:
        # Admins/supers may delete any pod; everyone else only pods that
        # never recorded anything (mis-joins, empty test pods).
        user = kwargs.get('user') or {}
        if user.get('role') not in wrappers.ADMIN_ROLES and database.session_device_has_data(session_device_id):
            return json_response({'message': 'This pod has recorded data — only an admin can delete it.'}, 403)
    session_handler.remove_session_device(session_device_id)
    if delete:
        database.delete_session_device(session_device_id)
        socketio_helper.remove_session_device(session_id, session_device_id)
    return json_response()


# Shared tail for the three metrics-export endpoints: wrap the accumulated
# CSV buffer / JSON dict in a download response.
def _metrics_export_response(si, json_payload, format):
    if format == 'csv':
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=export.csv"
        output.headers["Content-type"] = "text/csv"
    elif format == 'json':
        json_data = json.dumps(json_payload, indent=2)
        output = make_response(json_data)
        output.headers["Content-Disposition"] = "attachment; filename=export.json"
        output.headers["Content-type"] = "application/json"
    else:
        output = json_response({'message': 'Unsupported export format.'}, 400)
    return output


@api_routes.route('/api/v1/sessions/<int:session_id>/exporttranscriptmetrics/<int:windowsize>/<string:format>',methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def export_session_transcript_metrics(session_id,windowsize, format, **kwargs):
    si = io.StringIO()
    field_names = None  
    fwrite = None 
    session_devices = database.get_session_devices(session_id=session_id)
    all_participants_metrics = {}
    if windowsize == 0:
        field_names = ['Device ID', 'Device Name', 'Start Time', 'Transcript Id','Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone', 'Direction',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness','communication_density','Word Count', 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()
        for session_device in session_devices:
            transcripts = database.get_all_transcript_metrics_by_session(session_device_id=session_device.id)
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            # Index once: the per-transcript list scan was O(transcripts x
            # keyword_usages) per export.
            keywords_by_transcript = {}
            for keyword in keywords:
                keywords_by_transcript.setdefault(keyword.transcript_id, []).append(keyword)
            for t, sm in transcripts:
                transcript_keywords = keywords_by_transcript.get(t.id, [])
                fwrite.writerow({'Device ID':session_device.id,
                    'Device Name':session_device.name,
                    'Start Time': fmt_start_time(t.start_time),
                    'Transcript Id': int(t.id),
                    'Transcript':t.transcript,
                    'Keywords': ', '.join([keyword.keyword for keyword in transcript_keywords]),
                    'Keywords Detected': ', '.join([keyword.word for keyword in transcript_keywords]),
                    'Similarity': ', '.join([str(round((1-keyword.similarity)*100,3)) for keyword in transcript_keywords]),
                    'Analytic Thinking': int(t.analytic_thinking_value),
                    'Authenticity': int(t.authenticity_value),
                    'Certainty': int(t.certainty_value),
                    'Clout': int(t.clout_value),
                    'Emotional Tone': int(t.emotional_tone_value),
                    'participation_score': int(float(sm.participation_score)*100),
                    'internal_cohesion':  int(float(sm.internal_cohesion)*100),
                    'responsivity':  int(float(sm.responsivity)*100),
                    'social_impact':  int(float(sm.social_impact)*100),
                    'newness':  int(float(sm.newness)*100), 
                    'communication_density':  int(float(sm.communication_density)*100),
                    'Word Count': int(t.word_count),
                    'Speaker Tag': t.speaker_tag,
                    'Speaker ID': int(t.speaker_id),
                    'Topic ID': int(t.topic_id)
                    })
    else:
        field_names = ['Group ID', 'Group Name', 'Time Range (s)','Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone', 'Direction',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness','Word Count', 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()     
        for session_device in session_devices:
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            speakers = database.get_speakers(session_device_id=session_device.id)
            for speaker in speakers:
                transcriptSpeakerMetric = database.get_all_transcript_metrics_by_session(session_device_id=session_device.id,speaker_id=speaker.id)
                speaker_data = batch_transcript_metrics(transcriptSpeakerMetric,windowsize,fwrite,speaker,session_device,keywords,format)
                if format == 'json':
                    all_participants_metrics[speaker.alias] = [dict(zip(field_names, row)) for row in speaker_data]
                    # all_participants_metrics.extend(speaker_data)
                        

    return _metrics_export_response(si, all_participants_metrics, format)

@api_routes.route('/api/v1/sessions/<int:session_id>/exportvideometrics/<int:windowsize>/<string:format>',methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def export_session_video_metrics(session_id, windowsize, format, **kwargs):
    field_names = None
    fwrite = None  
    si = io.StringIO()
    session_devices = database.get_session_devices(session_id=session_id)
    all_participants_metrics = {}
    if windowsize == 0:
        field_names = ['Device ID', 'Device Name', 'Start Time', 'Facial Emotion', 'Object Focus On', 'Attention Level', 'Speaker Tag']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()  
        for session_device in session_devices:
            videoMetrics = database.get_video_metrics_by_session(session_device_id=session_device.id)
            for v in videoMetrics:
                fwrite.writerow({'Device ID':session_device.id,
                    'Device Name':session_device.name,
                    'Start Time': fmt_start_time(v.time_stamp),
                    'Facial Emotion': str(v.facial_emotion),
                    'Object Focus On': str(v.object_on_focus),
                    'Attention Level': int(v.attention_level),
                    'Speaker Tag': v.student_username
                    })

    else:
        field_names = ['Group ID', 'Group Name', 'Time Range (s)', 'Facial Emotion', 'Object Focus On', 'Attention Level','Attention Rate',"Attention Class", 'Speaker Tag']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()
        for session_device in session_devices:
            speakers = database.get_speakers(session_device_id=session_device.id)
            for speaker in speakers:
                videoMetrics = database.get_speaker_video_metrics(session_device_id=session_device.id,student_username=speaker.alias)
                speaker_data = batch_video_metrics(videoMetrics,windowsize,fwrite,speaker,session_device,format)
                if format == 'json':
                    all_participants_metrics[speaker.alias] = [dict(zip(field_names, row)) for row in speaker_data]
                    # all_participants_metrics.extend(speaker_data)  
    return _metrics_export_response(si, all_participants_metrics, format)

@api_routes.route('/api/v1/sessions/<int:session_id>/exporttranscriptvideometrics/<int:windowsize>/<string:format>',methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def export_session_transcript_video_metrics(session_id,windowsize, format, **kwargs):
    si = io.StringIO()
    field_names = None
    fwrite = None
    session_devices = database.get_session_devices(session_id=session_id)
    all_participants_metrics = {}
    if windowsize == 0:
        field_names = ['Device ID', 'Device Name', 'Start Time', 'Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone', 'Direction',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness','communication_density',
                    'Word Count', 'Facial Emotion', 'Object Focus On', 'Attention Level', 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()
        for session_device in session_devices:
            all_metrics = database.get_all_metrics_by_session(session_device_id=session_device.id)
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            # Index once: the per-transcript list scan was O(transcripts x
            # keyword_usages) per export.
            keywords_by_transcript = {}
            for keyword in keywords:
                keywords_by_transcript.setdefault(keyword.transcript_id, []).append(keyword)
            for t,sm,vm in all_metrics:
                transcript_keywords = keywords_by_transcript.get(t.id, [])
                fwrite.writerow({'Device ID':session_device.id,
                    'Device Name':session_device.name,
                    'Start Time': fmt_start_time(t.start_time),
                    'Transcript':t.transcript,
                    'Keywords': ', '.join([keyword.keyword for keyword in transcript_keywords]),
                    'Keywords Detected': ', '.join([keyword.word for keyword in transcript_keywords]),
                    'Similarity': ', '.join([str(round((1-keyword.similarity)*100,3)) for keyword in transcript_keywords]),
                    'Analytic Thinking': int(t.analytic_thinking_value),
                    'Authenticity': int(t.authenticity_value),
                    'Certainty': int(t.certainty_value),
                    'Clout': int(t.clout_value),
                    'Emotional Tone': int(t.emotional_tone_value),
                    'participation_score': int(float(sm.participation_score)*100),
                    'internal_cohesion':  int(float(sm.internal_cohesion)*100),
                    'responsivity':  int(float(sm.responsivity)*100),
                    'social_impact':  int(float(sm.social_impact)*100),
                    'newness':  int(float(sm.newness)*100), 
                    'communication_density':  int(float(sm.communication_density)*100),
                    'Word Count': int(t.word_count),
                    'Facial Emotion': str(vm.facial_emotion),
                    'Object Focus On': str(vm.object_on_focus),
                    'Attention Level': int(vm.attention_level),
                    'Speaker Tag': t.speaker_tag,
                    'Speaker ID': int(t.speaker_id),
                    'Topic ID': int(t.topic_id)
                    })
    else:
        field_names = ['Group ID', 'Group Name', 'Time Range (s)', 'Transcript', 'Keywords', 'Keywords Detected', 'Similarity', 'Analytic Thinking', 'Authenticity', 'Certainty',
                    'Clout', 'Emotional Tone',  'participation_score', 'internal_cohesion', 'responsivity', 'social_impact','newness',
                    'Word Count', 'Facial Emotion', 'Object Focus On', 'Attention Level','Attention Rate',"Attention Class", 'Speaker Tag', 'Speaker ID', 'Topic ID']
        fwrite = csv.DictWriter(si, fieldnames = field_names)
        fwrite.writeheader()       
        for session_device in session_devices:
            keywords = database.get_keyword_usages(session_device_id=session_device.id)
            speakers = database.get_speakers(session_device_id=session_device.id)
            for speaker in speakers:
                videoMetrics = database.get_speaker_video_metrics(session_device_id=session_device.id,student_username=speaker.alias)
                transcriptSpeakerMetric = database.get_all_transcript_metrics_by_session(session_device_id=session_device.id,speaker_id=speaker.id)
                speaker_data = batch_transcript_video_metrics(transcriptSpeakerMetric,videoMetrics,windowsize,fwrite,speaker,session_device,keywords,format)
                if format == 'json':
                    all_participants_metrics[speaker.alias] = [dict(zip(field_names, row)) for row in speaker_data]
                    # all_participants_metrics.extend(speaker_data) 
                   

    return _metrics_export_response(si, all_participants_metrics, format)

# Open by design: the anonymous student-dashboard and expert-rating flows
# call this with no credential (students have no accounts).
@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/synthesized_feedback_metrics',methods=['GET'])
def getSynthesizedFeedbackMetrics(session_id,session_device_id, **kwargs):
    session_device = _device_in_session(session_device_id, session_id)
    if session_device is None:
        return json_response({'message': 'Session device not found.'}, 404)

    existing_synthesis = database.get_synthesized_feedback_report(sessionId=session_id, sessionDeviceId = session_device_id)

    # An anonymous endpoint must not be a recompute-and-rewrite amplifier:
    # once the session has ended the data is frozen, so serve the stored
    # report instead of rebuilding the whole window synthesis per GET.
    parent_session = database.get_sessions(id=session_id)
    if existing_synthesis and parent_session is not None and parent_session.end_date is not None:
        return json_response(json.loads(existing_synthesis[0].synthesized_feedback))

    keywords = database.get_keyword_usages(session_device_id=session_device_id)
    videoMetrics = database.get_speaker_video_metrics(session_device_id=session_device_id)
    transcriptSpeakerMetric = database.get_all_transcript_metrics_by_session_by_timeline(session_device_id=session_device.id)
    combine_metric_level = synthesized_transcript_video_metrics_by_window(transcriptSpeakerMetric,videoMetrics,session_device,keywords,windowsize=10)

    combine_metric_dump = json.dumps(combine_metric_level)
    with _synthesis_write_lock:
        # Re-read under the lock: two concurrent GETs both saw "no report"
        # and each added a row, duplicating reports forever after.
        existing_synthesis = database.get_synthesized_feedback_report(sessionId=session_id, sessionDeviceId = session_device_id)
        if existing_synthesis:
            database.update_synthesized_feedback_report(id=existing_synthesis[0].id,synthesized_feedback=combine_metric_dump)
        else:
            database.add_synthesized_feedback_report(sessionId=session_id,sessionDeviceId=session_device_id,synthesized_feedback=combine_metric_dump)

    return json_response(combine_metric_level)
    
# Only the video processing service on this host calls these two (its
# config.ini points at 127.0.0.1); the frontend never does.
@api_routes.route('/api/v1/sessions/getredissessionkey', methods=['POST'])
@wrappers.verify_local
def get_device_key(**kwargs):
    content = request.get_json()
    redis_key = RedisSessions.get_device_key(content['auth_key'])
    if redis_key:
        return json_response({'redis_key': redis_key})
    else:
        return json_response({'message': "key cannot be authenticated"}, 400)

@api_routes.route('/api/v1/sessions/getredissessionconfig', methods=['POST'])
@wrappers.verify_local
def get_session_config(**kwargs):
    content = request.get_json()
    redis_session = RedisSessions.get_session_config(content['session_key'])
    if redis_session:
        return json_response({'redis_session_key': redis_session})
    else:
        return json_response({'message': "Session key cannot be authenticated"}, 400)

@api_routes.route('/api/v1/session/addcartoonizedimage', methods=['POST'])
@wrappers.verify_local
def add_cartoonized_image(**kwargs):
    logging.info('Received cartoonized image ...')
    content = request.get_json()
    queue_key = '{0}_{1}_{2}'.format(content['source'],content['sessionid'],content['deviceid'])
    # setdefault is atomic (the old check-then-set lost a frame batch when two
    # POSTs raced), and the queue is bounded: if no browser ever opens the
    # stream, base64 frames otherwise accumulate for the process lifetime.
    # When full, drop the oldest frame — the stream is a live preview.
    image_queue = image_queue_dict.setdefault(queue_key, queue.Queue(maxsize=120))
    try:
        image_queue.put_nowait(content)
    except queue.Full:
        try:
            image_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            image_queue.put_nowait(content)
        except queue.Full:
            pass
    return json_response()

@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:device_id>/auth/<auth_id>/streamimages')
def stream_cartonized_images(session_id, device_id,auth_id, **kwargs):
    try:
        # Absolute path — this used to os.chdir() from a request thread, which
        # mutates the whole process's cwd under every other request.
        recordings = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  '..', '..', 'video_processing', 'videorecordings')
        loading_frame = read_image(os.path.join(recordings, 'loading_img', 'loading.png'))
        queue_key = '{0}_{1}_{2}'.format(auth_id,session_id,device_id)
        return Response(gen(loading_frame,queue_key),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    except Exception as e:
        logging.error('Error occurred while streaming cartoonized image: {0}'.format(e))
        return json_response({'message': 'Streaming failed.'}, 500)


def gen(loading_frame,queue_key):
    try:
        started = False
        while True:
            image_queue = image_queue_dict.get(queue_key)
            if not started:
                if image_queue is not None and image_queue.qsize() >= 1:
                    started = True
                    continue
                yield (b'--frame\r\n'
                        b'Content-Type: image/png\r\n\r\n' + loading_frame + b'\r\n')
                time.sleep(0.2)
            else:
                # Block instead of spinning; a drained queue no longer pegs a
                # core while the client stays connected.
                try:
                    data = image_queue.get(timeout=1)
                except queue.Empty:
                    continue
                yield (b'--frame\r\n'
                b'Content-Type: image/png\r\n\r\n' +  base64.b64decode(data['image']) + b'\r\n')
    finally:
        # Client gone (GeneratorExit lands here): drop the queue so
        # image_queue_dict does not grow forever across sessions.
        image_queue_dict.pop(queue_key, None)

def read_image(filepath):
    with open(filepath, 'rb') as f:
        return f.read()
