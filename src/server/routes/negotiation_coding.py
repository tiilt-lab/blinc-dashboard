"""Negotiation coding per pod (negotiation_coding.py): start a run, read the
latest one with its codes and summary, re-assign teams, export CSV, and the
per-session overview. Gated like the other per-pod routes: session write
access to start or re-assign, read access to look; the pod must belong to
the session in the URL (ids are sequential).
"""
import csv
import importlib
import io

from flask import Blueprint, request, make_response, session as flask_session
from app import limiter
from utility import json_response
import authz
import database
import wrappers

# The engine, src/server/negotiation_coding.py. Loaded by name because a plain
# `import negotiation_coding` in a file of the same name reads as a self-import
# to the sibling-import sweep (tests/test_sibling_imports.py), although Python
# resolves it to the top-level module.
negotiation_coding = importlib.import_module('negotiation_coding')

api_routes = Blueprint('negotiation_coding', __name__)

POD = '/api/v1/sessions/<int:session_id>/devices/<int:session_device_id>/negotiation_coding'
CSV_COLUMNS = ('transcript_id', 'start_time', 'length', 'speaker_tag', 'team', 'text',
               'emotion', 'rip', 'frame', 'listening')


def _caller_key():
    # Per signed-in account (as the ticket route does), not per IP.
    return str((flask_session.get('user') or {}).get('id', 'anon'))


def _pod_or_404(session_device_id, session_id):
    if authz.device_in_session(session_device_id, session_id) is None:
        return json_response({'message': 'Does not exist.'}, 404)
    return None


def _teams_from_body():
    body = request.get_json(silent=True) or {}
    teams, error = negotiation_coding.validate_teams(body.get('teams'))
    if error:
        return None, json_response({'message': error}, 400)
    return teams, None


@api_routes.route(POD, methods=['POST'])
@wrappers.verify_login(public=True)
@limiter.limit("30 per hour", key_func=_caller_key)
@wrappers.verify_session_access
def start_negotiation_coding(session_id, session_device_id, **kwargs):
    missing = _pod_or_404(session_device_id, session_id)
    if missing:
        return missing
    teams, bad = _teams_from_body()
    if bad:
        return bad
    run = negotiation_coding.queue_coding_run(session_id, session_device_id, teams)
    return json_response({'run_id': run.id, 'status': run.status}, 202)


@api_routes.route(POD, methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def get_negotiation_coding(session_id, session_device_id, **kwargs):
    missing = _pod_or_404(session_device_id, session_id)
    if missing:
        return missing
    run = database.get_latest_negotiation_run(session_device_id)
    if run is None:
        return json_response({'message': 'No negotiation coding run for this pod.'}, 404)
    return json_response(negotiation_coding.run_payload(run))


@api_routes.route(POD + '/teams', methods=['PUT'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_access
def set_negotiation_teams(session_id, session_device_id, **kwargs):
    missing = _pod_or_404(session_device_id, session_id)
    if missing:
        return missing
    run = database.get_latest_negotiation_run(session_device_id)
    if run is None:
        return json_response({'message': 'No negotiation coding run for this pod.'}, 404)
    teams, bad = _teams_from_body()
    if bad:
        return bad
    # Stored codes re-rolled under the new teams; no LLM call.
    run = negotiation_coding.recompute_summary(run, teams)
    return json_response(negotiation_coding.run_payload(run))


@api_routes.route(POD + '/export.csv', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def export_negotiation_coding(session_id, session_device_id, **kwargs):
    missing = _pod_or_404(session_device_id, session_id)
    if missing:
        return missing
    run = database.get_latest_negotiation_run(session_device_id)
    if run is None:
        return json_response({'message': 'No negotiation coding run for this pod.'}, 404)
    payload = negotiation_coding.run_payload(run)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for c in payload['codes']:
        writer.writerow([c['transcript_id'], c['start_time'], c['length'], c['speaker_tag'],
                         negotiation_coding.team_of(c['speaker_tag'], payload['teams']), c['text'],
                         c['emotion'], c['rip'], c['frame'], ';'.join(c['listening'])])
    output = make_response(buf.getvalue().encode('utf-8'))
    output.headers['Content-Type'] = 'text/csv; charset=utf-8'
    output.headers['Content-Disposition'] = 'attachment; filename=negotiation_coding_session%d_pod%d.csv' % (
        session_id, session_device_id)
    return output


@api_routes.route('/api/v1/sessions/<int:session_id>/negotiation_coding', methods=['GET'])
@wrappers.verify_login(public=True)
@wrappers.verify_session_read_access
def list_negotiation_coding(session_id, **kwargs):
    # Every pod of the session with its latest run (no codes), run = null
    # where nothing was ever queued.
    latest = {}
    for run in database.get_negotiation_runs(session_id):   # newest first
        latest.setdefault(run.session_device_id, run)
    result = []
    for device in database.get_session_devices(session_id=session_id):
        run = latest.get(device.id)
        entry = {'device_id': device.id, 'device_name': device.name, 'run': None, 'teams': {}, 'summary': None}
        if run is not None:
            entry.update(negotiation_coding.run_payload(run, with_codes=False))
        result.append(entry)
    return json_response(result)
