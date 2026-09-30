
from flask import Blueprint, request
from tables.user import User
from app import base_dir
import logging
import database
import wrappers
from device_websockets import ConnectionManager
from io import BytesIO
import base64
import os
import config as cf
from redis_helper import RedisLogin
import utility
from utility import json_response, sanitize, sanitize_int_value
from datetime import timedelta
from tables.account_token import AccountToken
import emails

api_routes = Blueprint('admin', __name__)

@api_routes.route('/api/v1/admin/users', methods=['GET'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
def get_users(user, **kwargs):
    roles = ['user', 'admin']
    if user['role'] == 'super':
        roles.append('super')
    return json_response([user.json() for user in database.get_users(roles=roles)])

@api_routes.route('/api/v1/admin/students', methods=['GET'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
def get_students(**kwargs):
    students = database.get_students()
    if students:
        return json_response([student.json() for student in students])
    else:
        return json_response({'message': 'No Records found.'}, 400)

@api_routes.route('/api/v1/admin/raters', methods=['GET'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
def get_raters(**kwargs):
    raters = database.get_raters()
    if raters:
        return json_response([rater.json() for rater in raters])
    else:
        return json_response({'message': 'No Records found.'}, 400)        

@api_routes.route('/api/v1/admin/users', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
@wrappers.verify_valid_role
def add_user(**kwargs):
    content = request.json
    email = content.get('email', None)
    role = sanitize(content.get('role', None))
    valid, message = User.verify_fields(email=email, role=role)
    if not email:
        return json_response({'message': 'Must provide email.'}, 400)
    if not role:
        return json_response({'message': 'Must provide role.'}, 400)
    if not valid:
        return json_response({'message': message}, 400)
    success, user = database.add_user(email, role)
    if success:
        new_password = user.reset_password(16)
        database.save_changes()
        # The temporary password is still returned for the admin to pass on
        # by hand; the invite lets the person choose their own instead.
        token = database.create_account_token(user.id, AccountToken.INVITE, timedelta(days=7))
        invited = emails.invite(user.email, kwargs['user']['email'], token)
        return json_response({'user': user.json(), 'password': new_password, 'invite_emailed': invited})
    else:
        return json_response({'message': user}, 400)
    
@api_routes.route('/api/v1/admin/raters', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
def add_rater(**kwargs):
    content = request.json
    sessionid = sanitize_int_value(content.get('sessionid', None))
    sessiondeviceid = sanitize(content.get('sessiondeviceid', None))
    speakerid = sanitize(content.get('speakerid', None))
    speakertag = sanitize(content.get('speakertag', None))
    raterid = sanitize(content.get('raterid', None))
    type = sanitize(content.get('type', None))
    evaluationcategory = sanitize(content.get('evaluationcategory',None))
    if not sessionid:
        return json_response({'message': 'Must provide session ID as an integer.'}, 400)
    if not sessiondeviceid:
        return json_response({'message': 'Must provide session device ID as an integer.'}, 400)
    if not speakerid:
        return json_response({'message': 'Must provide speaker ID as an integer.'}, 400)
    if not speakertag:
        return json_response({'message': 'Must provide speaker tag as a string.'}, 400)
    if not raterid:
        return json_response({'message': 'Must provide rater ID as a string.'}, 400)
    if not type:
        return json_response({'message': 'Must provide type as a string.'}, 400)
    if not evaluationcategory:
        return json_response({'message': 'Must provide evaluation category as a string.'}, 400)
    success, rater = database.add_rater(sessionid,sessiondeviceid,speakerid,speakertag,raterid,type,evaluationcategory)
    if success:
        return json_response({'rater': rater.json()})
    else:
        return json_response({'message': rater}, 400)    

@api_routes.route('/api/v1/admin/users/<int:user_id>', methods=['DELETE'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
@wrappers.avoid_self
@wrappers.verify_user_authority
def delete_user(user_id, **kwargs):
    success = database.delete_user(user_id)
    if success:
        return json_response()
    else:
        return json_response({'message': 'User not found.'}, 404)
    
# Merge duplicate student profiles (students self-register during joining,
# so the same person often ends up with several usernames). Moves all
# username-keyed data to the target and deletes the duplicate; the
# duplicate's biometric recording is renamed to the target when the target
# has none, so their saved fingerprint keeps working.
@api_routes.route('/api/v1/admin/students/<int:student_id>/merge', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
def merge_student(student_id, **kwargs):
    target_id = request.json.get('targetId', None)
    if not target_id:
        return json_response({'message': 'Must supply targetId.'}, 400)
    if int(target_id) == student_id:
        return json_response({'message': 'Cannot merge a student into itself.'}, 400)
    target = database.get_students(id=int(target_id))
    moved = database.merge_students(student_id, int(target_id))
    if moved is None:
        return json_response({'message': 'Student not found.'}, 404)
    # Usernames build a path for os.remove/os.rename — basename them so a
    # traversed name can't reach outside the biometrics dir.
    dup_safe = utility.safe_name(moved['duplicate_username'])
    target_safe = utility.safe_name(target.username)
    # ALL enrollment artifacts follow the username, not just the raw .webm —
    # the voice print (.wav/.emb.npy/.check.json) and face embeddings
    # (.npy/.arc.npy) are what diarization and the Students page actually
    # read; leaving them behind made the surviving profile "not enrolled"
    # and lost the voice print.
    src_dir = os.path.join(cf.root_dir(), "chemistry-dashboard")
    artifact_files = [
        (os.path.join(src_dir, "audio_processing", "audiovideobiometrics"), (".webm",)),
        (os.path.join(src_dir, "audio_processing", "audiobiometrics"),
         (".wav", ".emb.npy", ".check.json")),
        (os.path.join(src_dir, "video_processing", "facial_embeddings"),
         (".npy", ".arc.npy")),
    ]
    try:
        if dup_safe and target_safe:
            for folder, exts in artifact_files:
                for ext in exts:
                    dup_file = os.path.join(folder, dup_safe + ext)
                    target_file = os.path.join(folder, target_safe + ext)
                    if os.path.isfile(dup_file):
                        if os.path.isfile(target_file):
                            os.remove(dup_file)
                        else:
                            os.rename(dup_file, target_file)
    except Exception as e:
        logging.warning('Unable to move biometric files during merge: {0}'.format(e))
    return json_response({'moved': moved, 'target': target.json()})


@api_routes.route('/api/v1/admin/students/<int:student_id>', methods=['DELETE'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
def delete_student(student_id, **kwargs):
    student = database.delete_student(student_id)
    if student is not None:
        safe_username = utility.safe_name(student.username)
        try:
            if safe_username:
                # Remove every enrollment artifact, not just the raw .webm
                # (same set the merge endpoint moves).
                src_dir = os.path.join(cf.root_dir(), "chemistry-dashboard")
                for folder, exts in [
                    (os.path.join(src_dir, "audio_processing", "audiovideobiometrics"), (".webm",)),
                    (os.path.join(src_dir, "audio_processing", "audiobiometrics"),
                     (".wav", ".emb.npy", ".check.json")),
                    (os.path.join(src_dir, "video_processing", "facial_embeddings"),
                     (".npy", ".arc.npy")),
                ]:
                    for ext in exts:
                        p = os.path.join(folder, safe_username + ext)
                        if os.path.isfile(p):
                            os.remove(p)
        except Exception as e:
            logging.info('Unable to delete biometric file: {0}'.format(e))
        return json_response()
    else:
        return json_response({'message': 'User not found.'}, 404)
    
@api_routes.route('/api/v1/admin/raters/<int:id>', methods=['DELETE'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
def delete_rater(id, **kwargs):
    rater = database.delete_rater(id)
    if rater is not None:
        return json_response()
    else:
        return json_response({'message': 'Rater not found.'}, 404)

@api_routes.route('/api/v1/admin/users/<int:user_id>/lock', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
@wrappers.avoid_self
@wrappers.verify_user_authority
def lock_user(user_id, **kwargs):
    user = database.update_user(user_id, {'locked': True})
    if user:
        return json_response(user.json())
    else:
        return json_response({'message': 'User not found.'}, 404)

@api_routes.route('/api/v1/admin/users/<int:user_id>/unlock', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
@wrappers.avoid_self
@wrappers.verify_user_authority
def unlock_user(user_id, **kwargs):
    user = database.update_user(user_id, {'locked': False})
    if user:
        RedisLogin.unlock_login(user.email)
        return json_response(user.json())
    else:
        return json_response({'message': 'User not found.'}, 404)

@api_routes.route('/api/v1/admin/users/<int:user_id>/role', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
@wrappers.avoid_self
@wrappers.verify_user_authority
@wrappers.verify_valid_role
def change_user_role(user_id, **kwargs):
    role = sanitize(request.get_json().get('role', None))
    valid, message = User.verify_fields(role=role)
    if not valid:
        return json_response({'message': message}, 400)
    user = database.update_user(user_id, {'role': role})
    if user:
        return json_response(user.json())
    else:
        return json_response({'message': 'User not found.'}, 404)

@api_routes.route('/api/v1/admin/users/<int:user_id>/reset', methods=['POST'])
@wrappers.verify_login(roles=wrappers.ADMIN_ROLES)
@wrappers.avoid_self
@wrappers.verify_user_authority
def reset_password(user_id, **kwargs):
    user = database.get_users(id=user_id)
    if user:
        new_password = user.reset_password(16)
        database.save_changes()
        return json_response({'password': new_password})
    else:
        return json_response({'message': 'User not found'}, 404)

@api_routes.route('/api/v1/admin/users/<int:user_id>/api', methods=['POST'])
@wrappers.verify_login(roles=['super'])
@wrappers.avoid_self
@wrappers.verify_user_authority
def allow_api_access(user_id, **kwargs):
    user = database.get_users(id=user_id)
    if user:
        api_client, client_secret = database.create_api_client(user_id)
        return json_response({'api_client': api_client.json(), 'client_secret': client_secret})
    else:
        return json_response({'message': 'User not found'}, 404)

@api_routes.route('/api/v1/admin/users/<int:user_id>/api', methods=['DELETE'])
@wrappers.verify_login(roles=['super'], public=True)
@wrappers.avoid_self
@wrappers.verify_user_authority
def revoke_api_access(user_id, **kwargs):
    user = database.get_users(id=user_id)
    if user:
        database.delete_api_client(user_id=user_id)
        return json_response({'success': True})
    else:
        return json_response({'message': 'User not found'}, 404)

@api_routes.route('/api/v1/admin/server/logs', methods=['GET'])
@wrappers.verify_login(roles=['super'])
def get_server_logs(**kwargs):
    log_type = sanitize(request.args.get('log_type', 'dcs'))
    if log_type in ['dcs', 'aps']:
        log_path = os.path.join(base_dir, 'discussion_capture_server.log')
        if log_type == 'aps':
            log_path = os.path.join(base_dir, '../audio_processing/audio_processing_service.log')
        # Tail, don't slurp: the log grows without bound between rotations and
        # the base64 JSON response holds ~2.7x the read size in memory at once.
        tail_bytes = 5 * 1024 * 1024
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            log_data = f.read()
        if size > tail_bytes:
            # Drop the partial first line and say what was omitted.
            log_data = log_data.split(b'\n', 1)[-1]
            log_data = '[truncated: showing last {0} of {1} bytes]\n'.format(
                len(log_data), size).encode('utf-8') + log_data
        return json_response({
            'type': log_type,
            'data': 'data:application/log;base64,' + base64.b64encode(log_data).decode("utf-8")
        })
    return json_response({'message': 'Log type does not exist.'}, 400)

@api_routes.route('/api/v1/admin/devices/<int:device_id>/logs', methods=['GET'])
@wrappers.verify_login(roles=['super'])
def get_device_logs(device_id, **kwargs):
    device = database.get_devices(id=device_id, connected=True, is_pod=True)
    if device:
        success, data = ConnectionManager.instance.send_command_and_wait(device_id, {'cmd': 'logs'})
        if success:
            mem = BytesIO()
            mem.write(data['logs'].encode('utf-8'))
            mem.seek(0)
            return json_response({
                'data': 'data:application/log;base64,' + base64.b64encode(mem.getvalue()).decode("utf-8")
            })
    return json_response({'message': 'Device does not exist or did not respond.'}, 400)

@api_routes.route('/api/v1/admin/server/logs', methods=['DELETE'])
@wrappers.verify_login(roles=['super'])
def delete_server_logs(**kwargs):
    log_type = sanitize(request.args.get('log_type', 'dcs'))
    if log_type in ['dcs', 'aps']:
        log_path = os.path.join(base_dir, 'discussion_capture_server.log')
        if log_type == 'aps':
            log_path = os.path.join(base_dir, '../audio_processing/audio_processing_service.log')
        open(log_path, 'w').close()
        return json_response({})
    return json_response({'message': 'Log type does not exist.'}, 400)

@api_routes.route('/api/v1/admin/devices/<int:device_id>/logs', methods=['DELETE'])
@wrappers.verify_login(roles=['super'])
def delete_device_logs(device_id, **kwargs):
    device = database.get_devices(id=device_id, connected=True, is_pod=True)
    if device:
        success, data = ConnectionManager.instance.send_command_and_wait(device_id, {'cmd': 'clear_logs'})
        if success:
            return json_response()
    return json_response({'message': 'Device does not exist or did not respond.'}, 400)
