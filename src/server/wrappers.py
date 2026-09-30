from flask import request, session
from functools import wraps
import database
import folder_access
import utility
from utility import json_response

# Roles that can see across accounts. Routes import this instead of spelling
# out the pair, so a future role rename touches one line.
ADMIN_ROLES = ('admin', 'super')

_UNAUTHORIZED = 'You are not authorized to use this endpoint.'
# Denied and nonexistent look identical on purpose: distinguishing them lets a
# caller probe sequential ids for other people's resources.
_NOT_FOUND = 'Does not exist.'


def verify_login(roles=None, allow_key=False, public=False):
    def verify_login_decorator(f):
        @wraps(f)
        def verify_function(*args, **kwargs):
            # Allow access for sessions.
            if session.get('user', None):
                # The cookie holds a snapshot taken at login, so anything an
                # admin changes afterwards used to take effect only once the
                # person signed in again: a promotion granted nothing, a
                # demotion took nothing away, locking left the account working,
                # and a deleted user kept their access. Re-read the row instead
                # and treat the cookie as nothing more than the user's id.
                stale_user = session.get('user', None)
                system_user = database.get_users(id=stale_user.get('id'))
                if not system_user or system_user.locked:
                    session.clear()
                    return json_response({'message': _UNAUTHORIZED}, 401)
                user = system_user.json()
                session['user'] = user
                kwargs['user'] = user
                if (not roles) or (roles and user.get('role', '') in roles):
                    return f(*args, **kwargs)
                else:
                    return json_response({'message': _UNAUTHORIZED}, 401)

            # Allow devices communicating with processing keys.
            if allow_key and request.headers.get('X-Processing-Key', None):
                processing_key = request.headers.get('X-Processing-Key', None)
                session_device = database.get_session_devices(processing_key=processing_key)
                if session_device:
                    return f(*args, **kwargs)

            # Allow public API access with client keys.
            if public and request.headers.get('X-Client-Id', None):
                client_id = request.headers.get('X-Client-Id', None)
                client_token = request.headers.get('X-Client-Token', None)
                if client_id and client_token:
                    api_client = database.get_api_clients(client_id=client_id)
                    if api_client and api_client.verify_token(client_token):
                        user = database.get_users(id=api_client.user_id)
                        kwargs['user'] = user.json()
                        # user is a model here, not the dict the session branch
                        # deals with — .get() would raise rather than authorise.
                        if (not roles) or (roles and user.role in roles):
                            return f(*args, **kwargs)
                        else:
                            return json_response({'message': _UNAUTHORIZED}, 401)
            return json_response({'message': _UNAUTHORIZED}, 401)

        return verify_function
    return verify_login_decorator

def verify_local(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        ip = utility.get_client_ip(request)
        if ip in ['localhost', '127.0.0.1']:
            return f(*args, **kwargs)
        else:
            return json_response({'message': 'Endpoint authorized for local use only.'}, 403)

    return verify_function

# Cross-account session reach:
#
#   user   own sessions, read and write, plus sessions filed in folders shared
#          with them — read as a viewer, write as an editor or manager
#   admin  as a user, plus every session read-only
#   super  every session, read and write
#
# _session_for() resolves the session a request names and returns None when the
# caller may not touch it at that level, so both guards below (and the listing
# in session.get_sessions) share one definition of "can reach". The rules
# themselves live in folder_access.py.
def folder_level(folder_id, user):
    """The caller's sharing level in a folder (see folder_access), or None."""
    return folder_access.effective_level(
        folder_id, user,
        get_folder=lambda fid: database.get_folders(id=fid, first=True),
        member_levels=database.get_folder_member_levels)

def can_file_session_into(folder_id, user):
    """Whether the caller may create a session in, or move one into, a folder.
    Needs editor there; admins may also file into any existing folder (their
    folder picker lists every account's). None / -1 is the caller's top level."""
    if folder_id is None or folder_id == -1:
        return True
    if user.get('role') in ADMIN_ROLES:
        return database.get_folders(id=folder_id, first=True) is not None
    return folder_access.at_least(folder_level(folder_id, user), folder_access.EDITOR)

def _session_for(session_id, user, write):
    session_model = database.get_sessions(id=session_id)
    if folder_access.session_allowed(session_model, user, write,
                                     lambda fid: folder_level(fid, user)):
        return session_model
    return None

def _verify_session_guard(write):
    def decorator(f):
        @wraps(f)
        def verify_function(*args, **kwargs):
            session_model = _session_for(kwargs['session_id'], kwargs['user'], write=write)
            if session_model:
                kwargs['session'] = session_model
                return f(*args, **kwargs)
            return json_response({'message': _NOT_FOUND}, 404)
        return verify_function
    return decorator

# Predicate form for routes that resolve the session id themselves (e.g. via a
# transcript row) rather than from the URL. Same write rules as the guards.
def session_write_allowed(session_id, user):
    return bool(_session_for(session_id, user, write=True))

# Read guard: attach the session for endpoints that only report on it.
verify_session_read_access = _verify_session_guard(write=False)

# Write guard: mutating a session (rename, delete, stop, re-run analysis)
# needs its owner, a super, or an editor of the folder it is filed in.
verify_session_access = _verify_session_guard(write=True)

# Read access to one pod's data (transcripts, metrics) for endpoints that name
# a session device rather than a session. Two callers are legitimate:
#
#   - a logged-in user with read access to the owning session, and
#   - the BYOD client, which holds that device's processing key from its join
#     response and sends it as X-Processing-Key.
#
# The key is checked against the device named in the URL, so one pod's key
# cannot read another pod's transcripts. Without either, the device id alone
# proves nothing: ids are sequential, so an unguarded endpoint hands any
# passer-by every group's speech by counting upwards.
def device_access_allowed(session_device, user=None):
    # The key may arrive as a header (API calls) or as ?key= — media loaded
    # through <video>/<img> tags cannot set headers, and the key is a secret
    # capability either way.
    processing_key = (request.headers.get('X-Processing-Key', None)
                      or request.args.get('key', None))
    if processing_key and processing_key == session_device.processing_key:
        return True
    if user is None:
        user = session.get('user', None)
    return bool(user and _session_for(session_device.session_id, user, write=False))


def verify_device_read_access(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        device_id = kwargs.get('device_id', kwargs.get('session_device_id'))
        session_device = database.get_session_devices(id=device_id)
        if not session_device:
            return json_response({'message': _NOT_FOUND}, 404)
        if device_access_allowed(session_device, user=kwargs.get('user')):
            kwargs['session_device'] = session_device
            return f(*args, **kwargs)
        return json_response({'message': _NOT_FOUND}, 404)

    return verify_function

def verify_session_access_json(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        session_id = request.json.get('sessionId', None)
        user = kwargs['user']
        if not session_id or not user:
            return json_response({'message': _UNAUTHORIZED}, 403)
        session_model = _session_for(session_id, user, write=True)
        if session_model:
            kwargs['session'] = session_model
            return f(*args, **kwargs)
        else:
            return json_response({'message': _NOT_FOUND}, 404)

    return verify_function

def verify_folder_level(required):
    """Guard for /folders/<folder_id> routes: attach the folder and the
    caller's level in it when that level is at least ``required``."""
    def decorator(f):
        @wraps(f)
        def verify_function(*args, **kwargs):
            folder_id = kwargs['folder_id']
            level = folder_level(folder_id, kwargs['user'])
            if folder_access.at_least(level, required):
                kwargs['folder'] = database.get_folders(id=folder_id, first=True)
                kwargs['folder_level'] = level
                return f(*args, **kwargs)
            if level is not None:
                # They can already see the folder, so saying why is no leak.
                return json_response({'message': 'You need {0} access to this folder.'.format(required)}, 403)
            return json_response({'message': _NOT_FOUND}, 404)
        return verify_function
    return decorator

# Renaming, moving and deleting a folder need its manager: the owner, anyone
# granted manager on it or above it, or a super.
verify_folder_access = verify_folder_level(folder_access.MANAGER)


def verify_keyword_list_access(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        keyword_list_id = kwargs['keyword_list_id']
        user = kwargs['user']
        keyword_list_model = database.get_keyword_lists(id=keyword_list_id, owner_id=user['id'])
        if keyword_list_model:
            kwargs['keyword_list'] = keyword_list_model
            return f(*args, **kwargs)
        else:
            return json_response({'message': 'Keyword list not found.'}, 403)

    return verify_function

def verify_topic_model_access(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        topic_model_id= kwargs['topic_model_id']
        user = kwargs['user']
        # Scoped to the requesting owner — without this any account can
        # operate on (e.g. delete) any user's topic model.
        topic_model_model = database.get_topic_models(id=topic_model_id, owner_id=user['id'])
        if topic_model_model:
            kwargs['topic_model'] = topic_model_model
            return f(*args, **kwargs)
        else:
            return json_response({'message': 'Topic model not found.'}, 403)

    return verify_function

# Expects 'user_id' to exist in keyword args.
def verify_user_authority(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        ranks = ['user', 'admin', 'super']
        user = kwargs['user']
        # The recipient is the target of the operation, not the caller. Looking
        # up the caller here made the rank comparison compare a role to itself,
        # so the check always passed and an admin could reset, delete, or demote
        # a super account.
        recipient_user = database.get_users(id=kwargs['user_id'])
        if recipient_user:
            recipient_role = recipient_user.role
            acting_role = user['role']
            if ranks.index(recipient_role) <= ranks.index(acting_role):
                return f(*args, **kwargs)
        # A caller who may not act on this user is told the same thing as one
        # naming an id that does not exist. Distinguishing the two let an admin
        # probe ids and learn which ones hold super accounts, since the listing
        # in admin.get_users hides those rows from them.
        return json_response({'message': 'User does not exist.'}, 404)

    return verify_function

# Expects 'user_id' and 'user' to exist in keyword args.
def avoid_self(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        user_id = kwargs['user_id']
        user = kwargs['user']
        if user_id != user['id']:
            return f(*args, **kwargs)
        else:
            return json_response({'message': 'You can not perform this operation on your account.'}, 403)

    return verify_function

# Expects 'user_id' and 'user' to exist in keyword args.
def verify_valid_role(f):
    @wraps(f)
    def verify_function(*args, **kwargs):
        ranks = ['user', 'admin']
        user_role = kwargs['user']['role']
        if user_role == 'super':
            ranks.append('super')
        role = request.json.get('role', None)
        if role in ranks:
            return f(*args, **kwargs)
        else:
            return json_response({'message': 'Invalid role.'}, 400)

    return verify_function

