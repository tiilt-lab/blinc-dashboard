from flask import Blueprint, request, session, send_file
from app import base_dir
import json
import logging
import os
import database
import wrappers
import utility
from utility import json_response
from redis_helper import RedisEnrollmentToken

api_routes = Blueprint('student', __name__)

# Enrollment artifacts live with the processing services, not the web app:
# voice prints as audiobiometrics/<username>.wav (+ .check.json quality
# sidecar written by enrollment_check) and face embeddings as
# facial_embeddings/<username>.npy.
_SRC_DIR = os.path.abspath(os.path.join(base_dir, '..'))
_VOICE_DIR = os.path.join(_SRC_DIR, 'audio_processing', 'audiobiometrics')
_FACE_DIR = os.path.join(_SRC_DIR, 'video_processing', 'facial_embeddings')


def _voice_paths(username):
    # Neutralize any traversal in a stored username before it hits disk (the
    # /addstudent write path is public; this is the read-side belt-and-braces).
    safe = utility.safe_name(username)
    if safe is None:
        raise ValueError('unsafe username')
    base = os.path.join(_VOICE_DIR, safe)
    return base + '.wav', base + '.check.json'


def _enrollment_fields(username):
    safe = utility.safe_name(username)
    if safe is None:
        return {'voice_enrolled': False, 'voice_check': None, 'face_enrolled': False}
    wav, check = _voice_paths(safe)
    fields = {
        'voice_enrolled': os.path.isfile(wav),
        'voice_check': None,
        'face_enrolled': os.path.isfile(os.path.join(_FACE_DIR, safe + '.npy')),
    }
    if fields['voice_enrolled'] and os.path.isfile(check):
        try:
            with open(check) as f:
                fields['voice_check'] = json.load(f)
        except Exception as e:
            logging.debug('enrollment fields: unreadable %s: %s', check, e)
    return fields


def _student_in_scope(user, username):
    # Admins see everyone; users only students who appeared in their sessions
    # (same linkage the Students page overview uses).
    if user.get('role') in wrappers.ADMIN_ROLES:
        return True
    return len(database.get_student_activity(username, owner_id=user['id'])) > 0

# Roster + participation stats for the Students page. Admins see every
# student; regular users see only students who have appeared in their own
# sessions (and the stats count only those sessions).
@api_routes.route('/api/v1/students/overview', methods=['GET'])
@wrappers.verify_login()
def students_overview(**kwargs):
    user = kwargs['user']
    is_admin = user.get('role') in wrappers.ADMIN_ROLES
    rows = database.get_students_overview(owner_id=None if is_admin else user['id'])
    result = []
    for student, session_count, last_active in rows:
        entry = student.json()
        entry['session_count'] = int(session_count or 0)
        entry['last_active'] = (str(last_active) + ' UTC') if last_active else None
        entry.update(_enrollment_fields(student.username))
        result.append(entry)
    return json_response(result)


# Which enrolled voices sound alike. Computed live from the cached ECAPA
# embeddings the audio service writes beside each voice print, restricted to
# students in the caller's scope. Similar voices are allowed to enroll; this
# is how instructors see whose utterances could be cross-attributed.
@api_routes.route('/api/v1/students/voice_overlaps', methods=['GET'])
@wrappers.verify_login()
def students_voice_overlaps(**kwargs):
    import numpy as np
    user = kwargs['user']
    is_admin = user.get('role') in wrappers.ADMIN_ROLES
    rows = database.get_students_overview(owner_id=None if is_admin else user['id'])
    visible = {student.username for student, _c, _l in rows}
    embeddings = {}
    for username in visible:
        safe = utility.safe_name(username)
        if safe is None:
            continue
        path = os.path.join(_VOICE_DIR, safe + '.emb.npy')
        if os.path.isfile(path):
            try:
                embeddings[username] = np.load(path)
            except Exception as e:
                # An unreadable embedding silently changes the overlap result;
                # leave a trace.
                logging.warning('voice overlaps: could not load %s: %s', path, e)
    from analytics import pairwise_voice_overlaps
    THRESHOLD = 0.50
    pairs = pairwise_voice_overlaps(embeddings, threshold=THRESHOLD)
    return json_response({'threshold': THRESHOLD, 'pairs': pairs})


# Stream a student's voice-fingerprint recording so its quality can be
# checked by ear. Scoped like everything else on the Students page.
@api_routes.route('/api/v1/students/<int:student_id>/fingerprint_audio', methods=['GET'])
@wrappers.verify_login()
def student_fingerprint_audio(student_id, **kwargs):
    user = kwargs['user']
    student = database.get_students(id=student_id)
    if not student:
        return json_response({'message': 'Student not found.'}, 404)
    if not _student_in_scope(user, student.username):
        return json_response({'message': 'Not authorized for this student.'}, 403)
    wav, _ = _voice_paths(student.username)
    if not os.path.isfile(wav):
        return json_response({'message': 'No voice enrollment on file.'}, 404)
    return send_file(wav, mimetype='audio/wav', conditional=True)


# Drill-down for one student: the sessions/groups they appeared in (scoped
# like the overview) plus their LLM feedback count. `owned` marks sessions
# the caller owns — only those can be opened in the dashboard.
@api_routes.route('/api/v1/students/<int:student_id>/activity', methods=['GET'])
@wrappers.verify_login()
def student_activity(student_id, **kwargs):
    user = kwargs['user']
    student = database.get_students(id=student_id)
    if not student:
        return json_response({'message': 'Student not found.'}, 404)
    is_admin = user.get('role') in wrappers.ADMIN_ROLES
    owner_id = None if is_admin else user['id']
    rows = database.get_student_activity(student.username, owner_id=owner_id)
    sessions = []
    for session_model, session_device in rows:
        sessions.append({
            'session_id': session_model.id,
            'session_name': session_model.name,
            'creation_date': str(session_model.creation_date) + ' UTC',
            'ended': session_model.end_date is not None,
            'session_device_id': session_device.id,
            'group_name': session_device.name,
            'owned': session_model.owner_id == user['id'],
        })
    # The student-profile page is a one-call view: fold the enrollment state
    # (voice/face + quality sidecar) into the student object like /overview.
    student_entry = student.json()
    student_entry.update(_enrollment_fields(student.username))
    return json_response({
        'student': student_entry,
        'sessions': sessions,
        'llm_reports': database.get_student_llm_report_count(student.username, owner_id=owner_id),
    })


@api_routes.route('/api/v1/student/getstudentbyid/<string:user_name>', methods=['GET'])
def get_students(user_name, **kwargs):
    student = database.get_students(username=user_name)
    if student:
        return json_response(student.json())
    else:
        return json_response({'message': 'Student  not found.'}, 404)
      
@api_routes.route('/api/v1/student/addstudent', methods=['POST'])
def add_students(**kwargs):
    content = request.json
    lastname = content.get('lastname', None)
    firstname = content.get('firstname', None)
    username = content.get('username', None)
    if not lastname:
        return json_response({'message': 'Must provide Last name.'}, 400)
    if not firstname:
        return json_response({'message': 'Must provide First name.'}, 400)
    if not username:
        return json_response({'message': 'Must provide User name.'}, 400)
    # The username becomes a filesystem path for the voice/face enrollment
    # artifacts; reject anything that isn't a safe leaf name (this route is
    # public, so it's the primary place to stop a poisoned username).
    if utility.safe_name(username) != username:
        return json_response({'message': 'Invalid characters in username.'}, 400)
    success, student = database.add_student(lastname, firstname,username)
    if success:
        database.save_changes()
        body = student.json()
        # The enrollment sockets accept media for this alias only with this
        # token (common/enrollment_token); the sign-up page sends it on both.
        body['enrollment_token'] = RedisEnrollmentToken.mint(student.username)
        return json_response(body)
    else:
        # Existing username: allow self-service re-enrollment when the entered
        # name matches the record (students have no passwords; the name check
        # keeps a classmate from casually overwriting someone else's biometrics
        # by typing their username). The client proceeds to the recording page
        # when reenroll_allowed is set; a fresh recording replaces the stored
        # voice print / face embedding under the same alias.
        name_matches = (
            (student.firstname or '').strip().lower() == firstname.strip().lower()
            and (student.lastname or '').strip().lower() == lastname.strip().lower()
        )
        body = {'message': "Username already exists.",
                "data": student.json(),
                "reenroll_allowed": name_matches}
        if name_matches:
            # Same token as the create path: only a name-matched re-enrolment
            # may replace the stored voice/face print.
            body['enrollment_token'] = RedisEnrollmentToken.mint(student.username)
        return json_response(body, 400)
       
        
@api_routes.route('/api/v1/student/updatestudent', methods=['POST'])
def update_students(**kwargs):
    content = request.json or {}
    id = content.get('id',None)
    if not id:
        return json_response({'message': 'Student  Id must be provided '}, 400)
    lastname = content.get('lastname', None)
    firstname = content.get('firstname', None)
    biometric_captured = content.get('biometric_captured', None)

    student = database.get_students(id=id)
    if not student:
        return json_response({'message': 'Student not found.'}, 404)

    # This route was fully unauthenticated — anyone could rewrite any
    # student's name and enrollment flag by id. Admins/supers keep full
    # edit; everyone else (the anonymous enrollment-completion path, which
    # only sets biometric_captured for the student who just enrolled) must
    # prove they know the student's name and may not rename — the same
    # protection addstudent uses for re-enrollment.
    sess_user = session.get('user')
    is_admin = False
    if sess_user:
        fresh = database.get_users(id=sess_user.get('id'))
        is_admin = bool(fresh and not fresh.locked and fresh.role in wrappers.ADMIN_ROLES)

    if not is_admin:
        name_ok = (
            (student.firstname or '').strip().lower() == (firstname or '').strip().lower()
            and (student.lastname or '').strip().lower() == (lastname or '').strip().lower()
        )
        if not name_ok:
            return json_response({'message': 'Name does not match our records.'}, 403)
        # Non-admins may only (re)confirm enrollment, never rename.
        lastname = None
        firstname = None

    success, student = database.update_student(id,lastname, firstname,biometric_captured)
    if success:
        database.save_changes()
        return json_response(student.json())
    else:
        return json_response({'message': "Update unsuccessful"}, 400)
    
@api_routes.route('/api/v1/student/raters/<string:rater_id>', methods=['GET'])
def get_rater_by_id(rater_id, **kwargs):
    raters = database.get_raters(raterid=rater_id,completed=0)
    if raters:
        return json_response([rater.json() for rater in raters])
    else:
        return json_response({'message': 'No Records found.'}, 400)

@api_routes.route('/api/v1/student/postrating', methods=['POST'])
def post_rating(**kwargs):
    content = request.json
    id = content.get('id',None)
    sessionid = content.get('sessionid',None)
    sessionDeviceId = content.get('sessionDeviceId',None)
    speakerTag = content.get('speakerTag',None)
    raterid = content.get('raterid',None)
    evaluationCategory = content.get('evaluationCategory',None)
    response = content.get('response',None)
    success = False
    # Scope the existing-rating lookup to THIS rater — with raterid omitted a
    # second rater's submission silently overwrites the first rater's row.
    rating = database.get_ratings(sessionid=sessionid,sessiondeviceid=sessionDeviceId,speakertag=speakerTag,raterid=raterid,evaluationcategory=evaluationCategory) if raterid else None
    if rating and response:
        resp = json.dumps(response)
        success, _ = database.update_rating(rating[0].id,response=resp)
    elif sessionid and sessionDeviceId and speakerTag and raterid and evaluationCategory and response:
        resp = json.dumps(response)
        success, _ = database.add_rating(sessionid,sessionDeviceId, speakerTag,raterid,evaluationCategory,resp) 
        database.update_rater(id,completed=1)

    if success:
        return json_response({'message': "success"})
    else:
        return json_response({'message': "Posting unsuccessful"}, 400)
    
@api_routes.route('/api/v1/student/postsurveyresponse', methods=['POST'])
def post_survey_response(**kwargs):
    content = request.json
    sessionid = content.get('sessionid',None)
    sessionDeviceId = content.get('sessionDeviceId',None)
    username = content.get('username',None)
    response = content.get('response',None)
    success = False
    survey = database.get_survey_response(sessionid=sessionid,sessiondeviceid=sessionDeviceId,username=username)
    if survey and response:
        resp = json.dumps(response)
        success, _ = database.update_survey_response(survey[0].id,response=resp)
    elif sessionid and sessionDeviceId and username and response:
        resp = json.dumps(response)
        success, _ = database.add_survey_response(sessionid,sessionDeviceId, username,resp) 
    if success:
        return json_response({'message': "success"})
    else:
        return json_response({'message': "Survey submission unsuccessful"}, 400)
