"""Wearable sensors streamed from the byod join page over Web Bluetooth:
heart-rate straps (Polar H10), EEG headbands (Muse), the sync taps that
check their clocks against the pod's audio, and the server clock the join
page syncs to (physio_time.py has the timing rules)."""
import math
import time

import csv
import io

from flask import Blueprint, Response, request, stream_with_context
import authz
import database
import physio_time
import raw_signals
import sync_taps
import wrappers
from utility import sanitize, json_response

api_routes = Blueprint('wearables', __name__)

MAX_HR_SAMPLES = 500
# Four electrodes a second per wearer, a 5-second flush, a full table of
# wearers with headroom for a backlog after a network hiccup.
MAX_EEG_SAMPLES = 2000
MAX_SYNC_MARKERS = 50
# Raw blocks are a second each, one per stream per wearer: two a second per
# headband, the same headroom as MAX_EEG_SAMPLES.
MAX_RAW_BLOCKS = 500
# Blocks read per round trip by the export.
RAW_EXPORT_PAGE = 200
EEG_CHANNELS = ('TP9', 'AF7', 'AF8', 'TP10', 'AUX')


def _server_now_ms():
    return time.time() * 1000.0


def _text(value, default, limit):
    # sanitize('') returns None — slicing it raised a TypeError that
    # aborted a whole batch on one bad sample.
    return (sanitize(str(value if value is not None else '')) or default)[:limit]


def _batch_clock(session_device, content):
    """(session start, skew) for a batch: epoch ms of session second 0 and
    the server-minus-laptop skew a sample without a synced time falls back
    to (physio_time.py)."""
    parent_session = database.get_sessions(id=session_device.session_id)
    return (physio_time.session_start_ms(parent_session.creation_date),
            physio_time.batch_skew_ms(_server_now_ms(), content.get('client_now')))


def _finite(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@api_routes.route('/api/v1/time', methods=['GET'])
def get_server_time():
    # The join page's clock sync (server-clock.js) times a round trip to
    # here; nothing secret, nothing per user.
    return json_response({'server_ms': _server_now_ms()})


@api_routes.route('/api/v1/devices/<int:device_id>/heartrate/client', methods=['POST'])
@wrappers.verify_device_read_access
def add_device_heart_rate(device_id, **kwargs):
    # Heart-rate/RR ingest from the byod join page (Polar straps over Web
    # Bluetooth), one sample per strap notification: hr, rr (ms), the
    # laptop's t, and from a synced page server_t (the notification) and
    # server_beat (when its last beat ended, sample-clock.js).
    session_device = kwargs.get('session_device') or database.get_session_devices(id=device_id)
    content = request.get_json() or {}
    samples = content.get('samples', [])
    if not isinstance(samples, list) or not samples:
        return json_response({'added': 0})
    samples = samples[:MAX_HR_SAMPLES]
    start_ms, skew_ms = _batch_clock(session_device, content)
    rows = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        try:
            hr = int(s['hr'])
        except (KeyError, TypeError, ValueError):
            continue
        at = physio_time.server_epoch_ms(s, 't', skew_ms)
        if at is None:
            continue
        time_stamp, time_ms = physio_time.session_times(at, start_ms)
        beat = physio_time.server_epoch_ms(s, 'beat', skew_ms)
        rr = s.get('rr') or []
        rr_text = ','.join(str(int(v)) for v in rr if isinstance(v, (int, float)))[:64]
        rows.append({
            'speaker_id': s.get('speaker_id'),
            'speaker_alias': _text(s.get('alias'), 'Unknown', 64),
            'sensor_name': _text(s.get('sensor'), '', 64),
            'time_stamp': time_stamp,
            'time_ms': time_ms,
            'beat_ms': physio_time.session_times(beat, start_ms)[1] if beat is not None else None,
            'heart_rate': hr,
            'rr_ms': rr_text,
        })
    added = database.add_speaker_hr_metrics_batch(session_device.id, rows) if rows else []
    return json_response({'added': len(added)})


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/heartrate', methods=['GET'])
@wrappers.verify_device_read_access
def get_pod_heart_rate(session_id, session_device_id, **kwargs):
    # Same open-read trust level as the joint_attention endpoint.
    rows = database.get_speaker_hr_metrics(session_device_id=session_device_id)
    return json_response([row.json() for row in rows])


@api_routes.route('/api/v1/devices/<int:device_id>/eeg/client', methods=['POST'])
@wrappers.verify_device_read_access
def add_device_eeg(device_id, **kwargs):
    # EEG band power from the join page's Muse headbands (muse-eeg.js): one
    # sample per electrode per 1-second epoch, server_t its first sample.
    session_device = kwargs.get('session_device') or database.get_session_devices(id=device_id)
    content = request.get_json() or {}
    samples = content.get('samples', [])
    if not isinstance(samples, list) or not samples:
        return json_response({'added': 0})
    samples = samples[:MAX_EEG_SAMPLES]
    start_ms, skew_ms = _batch_clock(session_device, content)
    rows = []
    for s in samples:
        if not isinstance(s, dict) or s.get('channel') not in EEG_CHANNELS:
            continue
        at = physio_time.server_epoch_ms(s, 't', skew_ms)
        bands = s.get('bands') if isinstance(s.get('bands'), dict) else {}
        if at is None:
            continue
        time_stamp, time_ms = physio_time.session_times(at, start_ms)
        rows.append({
            'speaker_id': s.get('speaker_id'),
            'speaker_alias': _text(s.get('alias'), 'Unknown', 64),
            'sensor_name': _text(s.get('sensor'), '', 64),
            'time_stamp': time_stamp,
            'time_ms': time_ms,
            'channel': s['channel'],
            'bands': {name: _finite(bands.get(name)) for name in ('delta', 'theta', 'alpha', 'beta', 'gamma')},
            'std_uv': _finite(s.get('std')),
        })
    added = database.add_speaker_eeg_metrics_batch(session_device.id, rows) if rows else []
    return json_response({'added': len(added)})


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/eeg', methods=['GET'])
@wrappers.verify_device_read_access
def get_pod_eeg(session_id, session_device_id, **kwargs):
    rows = database.get_speaker_eeg_metrics(session_device_id)
    return json_response([row.json() for row in rows])


@api_routes.route('/api/v1/devices/<int:device_id>/raw/client', methods=['POST'])
@wrappers.verify_device_read_access
def add_device_raw_blocks(device_id, **kwargs):
    # Native-rate signal from the join page (muse-eeg.js RawBlockAssembler):
    # one block per stream per second, {sensor_kind, stream, index (first
    # sample since connect), t / server_t (its first sample), data (base64
    # int16 counts)}. A block whose size does not match its stream's layout
    # (raw_signals.SPECS) is dropped, not stored half-read.
    session_device = kwargs.get('session_device') or database.get_session_devices(id=device_id)
    content = request.get_json() or {}
    blocks = content.get('blocks', [])
    if not isinstance(blocks, list) or not blocks:
        return json_response({'added': 0})
    start_ms, skew_ms = _batch_clock(session_device, content)
    rows = []
    for b in blocks[:MAX_RAW_BLOCKS]:
        if not isinstance(b, dict):
            continue
        sp = raw_signals.spec(b.get('sensor_kind'), b.get('stream'))
        data = raw_signals.decode_block(sp, b.get('data')) if sp else None
        index = raw_signals.finite_int(b.get('index'))
        at = physio_time.server_epoch_ms(b, 't', skew_ms)
        if data is None or index is None or index < 0 or at is None:
            continue
        rows.append({
            'speaker_id': b.get('speaker_id'),
            'speaker_alias': _text(b.get('alias'), 'Unknown', 64),
            'sensor_kind': b['sensor_kind'],
            'sensor_name': _text(b.get('sensor'), '', 64),
            'stream': b['stream'],
            'channels': ','.join(sp['channels']),
            'rate': sp['rate'],
            'scale': sp['scale'],
            'unit': sp['unit'],
            'sample_index': index,
            'time_ms': physio_time.session_times(at, start_ms)[1],
            'n_samples': sp['rate'],
            'n_missing': raw_signals.missing_count(data),
            'data': data,
        })
    added = database.add_raw_blocks_batch(session_device.id, rows) if rows else []
    return json_response({'added': len(added)})


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/raw', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def get_pod_raw_summary(session_id, session_device_id, **kwargs):
    # Which raw streams the pod has, for the dashboard's download links.
    if authz.device_in_session(session_device_id, session_id) is None:
        return json_response({'message': 'Not found.'}, 404)
    return json_response([
        {'sensor_kind': kind, 'stream': stream, 'seconds': blocks,
         'rate': (raw_signals.spec(kind, stream) or {}).get('rate')}
        for kind, stream, blocks in database.raw_block_summary(session_device_id)])


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/raw/<sensor_kind>/<stream>.csv',
                  methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def export_pod_raw(session_id, session_device_id, sensor_kind, stream, **kwargs):
    # The pod's raw stream as CSV, one row per sample at the native rate
    # (raw_signals.csv_rows), wearer by wearer; ?speaker_id= for one wearer.
    # Streamed: an hour of one headband's EEG is close to a million rows.
    sp = raw_signals.spec(sensor_kind, stream)
    if sp is None or authz.device_in_session(session_device_id, session_id) is None:
        return json_response({'message': 'Not found.'}, 404)
    speaker_id = request.args.get('speaker_id', type=int)
    query = database.query_raw_blocks(session_device_id, sensor_kind, stream, speaker_id)

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(raw_signals.header(sp))
        for block in query.yield_per(RAW_EXPORT_PAGE):
            writer.writerows(raw_signals.csv_rows(sp, block))
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
        yield buf.getvalue()

    name = 'raw_%s_%s_session%d_pod%d.csv' % (sensor_kind, stream, session_id, session_device_id)
    return Response(stream_with_context(generate()), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=' + name})


def _sync_row(m, kind, source, time_ms):
    return {
        'speaker_id': m.get('speaker_id'),
        'sync_id': _text(m.get('sync_id'), '', 40),
        'kind': kind,
        'source': source,
        'sensor_kind': _text(m.get('sensor_kind'), '', 16),
        'sensor_name': _text(m.get('sensor'), '', 64),
        'time_ms': time_ms,
    }


@api_routes.route('/api/v1/devices/<int:device_id>/sync/client', methods=['POST'])
@wrappers.verify_device_read_access
def add_device_sync_markers(device_id, **kwargs):
    # A sync press and the taps the sensor felt (sync_taps.py), timed like
    # every other sample from the join page.
    session_device = kwargs.get('session_device') or database.get_session_devices(id=device_id)
    content = request.get_json() or {}
    markers = content.get('markers', [])
    if not isinstance(markers, list) or not markers:
        return json_response({'added': 0})
    start_ms, skew_ms = _batch_clock(session_device, content)
    rows = []
    for m in markers[:MAX_SYNC_MARKERS]:
        if not isinstance(m, dict) or m.get('kind') not in ('prompt', 'tap') or not m.get('sync_id'):
            continue
        at = physio_time.server_epoch_ms(m, 't', skew_ms)
        if at is None:
            continue
        rows.append(_sync_row(m, m['kind'], 'live', physio_time.session_times(at, start_ms)[1]))
    added = database.add_sync_markers_batch(session_device.id, rows) if rows else []
    return json_response({'added': len(added)})


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/sync/import', methods=['POST'])
@wrappers.verify_login()
def import_sync_taps(session_id, session_device_id, user=None, **kwargs):
    # Taps read from a device's own export, on that device's clock: an
    # Empatica E4's tags.csv (button presses, Unix seconds) or knocks found
    # in its ACC.csv. {sync_id, speaker_id, sensor_kind, sensor, epoch_ms: []}.
    # Not synced to anything, so the sync report searches wider for them.
    if not wrappers.session_write_allowed(session_id, user):
        return json_response({'message': 'Not found.'}, 404)
    session_device = database.get_session_devices(id=session_device_id)
    if not session_device or session_device.session_id != session_id:
        return json_response({'message': 'Not found.'}, 404)
    content = request.get_json() or {}
    epochs = [v for v in (_finite(e) for e in (content.get('epoch_ms') or [])[:MAX_SYNC_MARKERS]) if v is not None]
    if not content.get('sync_id') or len(epochs) < sync_taps.MIN_MATCHED:
        return json_response({'message': 'sync_id and at least two tap times are required.'}, 400)
    start_ms = physio_time.session_start_ms(database.get_sessions(id=session_id).creation_date)
    rows = [_sync_row(content, 'tap', 'import', physio_time.session_times(e, start_ms)[1])
            for e in epochs]
    added = database.add_sync_markers_batch(session_device.id, rows)
    return json_response({'added': len(added)})


def _audio_window_reader(session_id, session_device_id):
    """read_window for sync_taps.report over the pod's recording, mapped
    through its clock sidecar (the transcript player's mapping), or None
    when the pod has no audio."""
    from routes import session as session_routes
    path = session_routes._find_pod_audio(session_device_id)
    if not path:
        return lambda start_s, end_s: None
    session_model = database.get_sessions(id=session_id)
    info = session_routes._audio_clock(path) or session_routes._stamp_clock(path, session_model)
    segments = session_routes._audio_segments(info, session_routes._audio_duration(path))

    def read(start_s, end_s):
        import soundfile
        for seg in segments:
            dur = seg['duration'] if seg['duration'] is not None else float('inf')
            if not (seg['start'] <= end_s and start_s < seg['start'] + dur):
                continue
            lo = max(start_s, seg['start'])
            hi = min(end_s, seg['start'] + dur)
            sr = soundfile.info(path).samplerate
            first = int((seg['video_start'] + lo - seg['start']) * sr)
            data, sr = soundfile.read(path, start=first,
                                      stop=int((seg['video_start'] + hi - seg['start']) * sr),
                                      dtype='float64')
            if len(data) == 0:
                return None
            return data, sr, seg['start'] + first / float(sr) - seg['video_start']
        return None
    return read


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/sync', methods=['GET'])
@wrappers.verify_device_read_access
def get_pod_sync(session_id, session_device_id, **kwargs):
    # Each sync press with the offset its taps measured against the audio
    # (sync_taps.report). Computed on read: a few seconds of audio per press.
    markers = [m.json() for m in database.get_sync_markers(session_device_id)]
    if not markers:
        return json_response([])
    return json_response(sync_taps.report(markers, _audio_window_reader(session_id, session_device_id)))
