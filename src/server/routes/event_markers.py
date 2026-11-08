"""Event markers from the join page (tables/event_marker.py): the moment a
laptop joined, started recording, and each step of the baseline (eyes-open
and eyes-closed rest, the group game, the start of the discussion).

The page sends each marker as it happens, timed like every wearable sample
(physio_time.py): server_t when the page has synced its clock to the
server, else its own t corrected by the batch's client_now skew. Stored on
the session clock in ms, the same clock as the heart-rate and EEG streams,
so a marker lines up with them (and with an imported device file placed on
that clock) by time.
"""
import csv
import io
import time

from flask import Blueprint, request, make_response
import database
import physio_time
import wrappers
from tables.event_marker import EventMarker
from utility import sanitize, json_response

api_routes = Blueprint('event_markers', __name__)

# A page holds markers while offline and sends them together when it is
# back; a whole session protocol is ten.
MAX_MARKERS = 50


def _text(value, limit):
    # Optional free text: None when absent or empty.
    text = sanitize(str(value)) if value is not None else None
    return text[:limit] if text else None


@api_routes.route('/api/v1/devices/<int:device_id>/markers/client', methods=['POST'])
@wrappers.verify_device_read_access
def add_device_event_markers(device_id, **kwargs):
    # {markers: [{marker_id, label, t, server_t?, sensors?, detail?}],
    #  client_now}. Unknown labels and markers without an id or a time are
    # skipped; a marker_id already stored for this pod is not stored twice.
    session_device = kwargs.get('session_device') or database.get_session_devices(id=device_id)
    content = request.get_json(silent=True) or {}
    markers = content.get('markers', [])
    if not isinstance(markers, list) or not markers:
        return json_response({'added': 0})
    parent_session = database.get_sessions(id=session_device.session_id)
    start_ms = physio_time.session_start_ms(parent_session.creation_date)
    skew_ms = physio_time.batch_skew_ms(time.time() * 1000.0, content.get('client_now'))
    rows = []
    for m in markers[:MAX_MARKERS]:
        if not isinstance(m, dict) or m.get('label') not in EventMarker.LABELS or not m.get('marker_id'):
            continue
        at = physio_time.server_epoch_ms(m, 't', skew_ms)
        if at is None:
            continue
        time_stamp, time_ms = physio_time.session_times(at, start_ms)
        rows.append({
            'marker_id': str(m['marker_id'])[:40],
            'label': m['label'],
            'time_stamp': time_stamp,
            'time_ms': time_ms,
            'sensors': _text(m.get('sensors'), 255),
            'detail': _text(m.get('detail'), 255),
        })
    added = database.add_event_markers(session_device.id, rows) if rows else []
    return json_response({'added': len(added)})


@api_routes.route('/api/v1/sessions/<int:session_id>/device/<int:session_device_id>/markers', methods=['GET'])
@wrappers.verify_device_read_access
def get_pod_event_markers(session_id, session_device_id, **kwargs):
    return json_response([m.json() for m in database.get_event_markers(session_device_id=session_device_id)])


@api_routes.route('/api/v1/sessions/<int:session_id>/markers.csv', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def export_session_event_markers(session_id, **kwargs):
    # Every pod's markers in time order, one CSV for the session.
    pods = {p.id: p for p in database.get_session_devices(session_id=session_id)}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EventMarker.CSV_COLUMNS)
    for m in database.get_event_markers(session_id=session_id):
        writer.writerow(m.csv_row(pods.get(m.session_device_id)))
    output = make_response(buf.getvalue().encode('utf-8'))
    output.headers['Content-Type'] = 'text/csv; charset=utf-8'
    output.headers['Content-Disposition'] = 'attachment; filename=markers_session%d.csv' % session_id
    return output
