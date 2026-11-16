"""Downloads for the pods overview's Download dialog: the session's data as one
ZIP of CSV files (exports.py), and each pod's recorded video.

Both need a signed-in user (or API client) with read access to the session:
the owner, a super or admin, or a member of the session's folder. Unlike the
in-page video player (routes/session.py, get_session_device_video), a pod's
processing key does NOT open these. Every laptop that joined a pod holds that
key, and the full recording is for the session's researchers, not its
participants.

A recording is served as the remuxed copy routes/session.py builds for the
player (seekable, disconnect segments joined). Building it can take minutes,
longer than nginx waits on a request, so the dialog polls video/download/status:
the first poll starts the build in a background thread, later polls report
"preparing" until the copy is ready. If the build fails, the raw recording
files stay downloadable one by one (?part=N).
"""
import logging
import os
import re
import threading
import time

from flask import Blueprint, request, send_file

import authz
import exports
import wrappers
from utility import json_response

api_routes = Blueprint('session_export', __name__)

POD_VIDEO = '/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/video/download'

# Written beside a remux target whose build did not produce it. Ignored once
# older than this (seconds), so a build mistaken for failed (another worker
# still held the remux lock past its wait) is retried rather than stuck. Same
# span as the remux lock's TTL in routes/session.py.
FAILED_SUFFIX = '.failed'
FAILED_RETRY_S = 3900

# Pods this process is building a remux for, so repeated polls start one
# thread. Another worker may start its own; the remux lock in
# routes/session.py lets only one of them run ffmpeg and the rest wait for it.
_preparing = set()
_preparing_lock = threading.Lock()


def _media():
    # The recording helpers live with the player routes; imported on use so
    # this module loads without the whole of routes/session.py.
    from routes import session as session_routes
    return session_routes


class _Recordings:
    """The ``media`` exports.build_zip reads pod recordings through. Never
    starts a remux: segment lengths come from a ready copy or are estimated."""

    def recording_files(self, device_id):
        return _media()._find_original_videos(device_id)

    def segments(self, session_id, device_id):
        return _media()._video_segments(session_id, device_id, prepare=False)


@api_routes.route('/api/v1/sessions/<int:session_id>/export.zip', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def export_session_zip(session_id, **kwargs):
    # ?items=pods,heart_rate,... (exports.DATASETS keys); ?window=<seconds>
    # groups the metric files into windows, 0 = one row per line.
    try:
        keys = exports.parse_items(request.args.get('items'))
        window = exports.parse_window(request.args.get('window'))
    except ValueError as e:
        return json_response({'message': str(e)}, 400)
    session = kwargs['session']
    archive = exports.build_zip(session, keys, window, _Recordings())
    logging.info('session export: user %s session %s items %s window %s',
                 (kwargs.get('user') or {}).get('id'), session_id, ','.join(keys), window)
    return send_file(archive, mimetype='application/zip', as_attachment=True,
                     download_name=exports.zip_filename(session))


def _pod_slug(pod):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', pod.name or '').strip('_')[:40] or 'pod%d' % pod.id


def _failed_recently(target):
    try:
        return time.time() - os.path.getmtime(target.fixed + FAILED_SUFFIX) < FAILED_RETRY_S
    except OSError:
        return False


def _prepare(device_id, target):
    # Build the remux in the background (once per process); mark the target
    # failed if the build fell back to a raw file instead.
    with _preparing_lock:
        if device_id in _preparing:
            return
        _preparing.add(device_id)

    def work():
        media = _media()
        try:
            media._fixed_video_path(device_id)
            if not media._fixed_video_ready(target):
                logging.warning('video download: remux for pod %s did not finish', device_id)
                with open(target.fixed + FAILED_SUFFIX, 'w') as f:
                    f.write('remux did not produce %s\n' % os.path.basename(target.fixed))
        except Exception as e:
            logging.warning('video download: remux for pod %s failed: %s', device_id, e)
        finally:
            with _preparing_lock:
                _preparing.discard(device_id)

    threading.Thread(target=work, daemon=True, name='video-download-%d' % device_id).start()


def _video_state(session_id, pod):
    """(target, status dict) for a pod's download, or (None, None) when it
    has no recording. Starts the remux when it is neither ready nor failed."""
    target = _media()._fixed_video_target(pod.id)
    if target is None:
        return None, None
    status = {'segments': len(target.originals)}
    if _media()._fixed_video_ready(target):
        status.update(state='ready', bytes=os.path.getsize(target.fixed),
                      filename='session%d_%s%s' % (session_id, _pod_slug(pod), target.ext))
    elif _failed_recently(target):
        status.update(state='failed', parts=[
            {'part': i + 1, 'bytes': os.path.getsize(p)} for i, p in enumerate(target.originals)])
    else:
        _prepare(pod.id, target)
        status.update(state='preparing')
    return target, status


@api_routes.route(POD_VIDEO + '/status', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def video_download_status(session_id, session_device_id, **kwargs):
    pod = authz.device_in_session(session_device_id, session_id)
    if pod is None:
        return json_response({'message': 'Pod not found in this session.'}, 404)
    _target, status = _video_state(session_id, pod)
    if status is None:
        return json_response({'message': 'No recording.'}, 404)
    return json_response(status)


@api_routes.route(POD_VIDEO, methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def video_download(session_id, session_device_id, **kwargs):
    # The joined, seekable recording; ?part=N instead serves raw recording
    # file N (1-based), for when the joined copy could not be built.
    pod = authz.device_in_session(session_device_id, session_id)
    if pod is None:
        return json_response({'message': 'Pod not found in this session.'}, 404)
    user_id = (kwargs.get('user') or {}).get('id')
    part = request.args.get('part', type=int)
    if part is not None:
        originals = _media()._find_original_videos(pod.id)
        if not 1 <= part <= len(originals):
            return json_response({'message': 'No such recording part.'}, 404)
        path = originals[part - 1]
        name = 'session%d_%s_part%dof%d%s' % (session_id, _pod_slug(pod), part, len(originals),
                                              os.path.splitext(path)[1])
    else:
        target, status = _video_state(session_id, pod)
        if status is None:
            return json_response({'message': 'No recording.'}, 404)
        if status['state'] != 'ready':
            return json_response(status, 409)
        path, name = target.fixed, status['filename']
    logging.info('video download: user %s session %s pod %s file %s',
                 user_id, session_id, pod.id, os.path.basename(path))
    mimetype = 'video/mp4' if path.lower().endswith('.mp4') else 'video/webm'
    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=name,
                     conditional=True)
