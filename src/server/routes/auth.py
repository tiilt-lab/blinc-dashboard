from flask import Blueprint, request, session
from utility import json_response
from datetime import datetime, timezone
from app import limiter
import logging
import database
import wrappers
from tables.user import User
import utility
from redis_helper import RedisLogin
from datetime import timedelta
from tables.account_token import AccountToken
import emails

api_routes = Blueprint('auth', __name__)

def get_request_username():
    content = request.json
    username = content.get('email', None)
    return username

def get_request_client_id():
    return request.headers.get('X-Client-Id', None)

@api_routes.route('/api/v1/login', methods=['POST'])
@limiter.limit("5 per 5 second", key_func=get_request_username)
def login():
    content = request.json
    ip = utility.get_client_ip(request)
    email = content.get('email', None)
    password = content.get('password', None)
    message = ''
    user = None
    if not email or not password:
        message = 'Please provide both an email and password.'
    elif not RedisLogin.can_login(email, ip):
        message = 'Too many failed login attempts.  Please try again later.'
    else:
        system_user = database.get_users(email=email)
        if system_user:
            if not system_user.verify_password(password):
                message = 'Incorrect username or password.'
                RedisLogin.failed_login_attempt(email, ip)
            else:
                if system_user.locked:
                    message = 'Your account has been locked. \nPlease contact your IT department to unlock your account.'
                else:
                    system_user.last_login = datetime.now(timezone.utc).replace(tzinfo=None)
                    database.save_changes()
                    user = system_user.json()
                    session['user'] = user
                    session.permanent = True
                    message = 'Login successful.'
                    RedisLogin.successful_login_attempt(email, ip)
                    logging.info('Login attempt for {0} from {1} succeeded.'.format(email, ip))
                    return json_response(user)
        else:
            message = 'Incorrect username or password.'
            RedisLogin.failed_login_attempt(email, ip)
    logging.info('Login attempt for {0} from {1} failed'.format(email, ip))
    return json_response({'message': message}, 400)

# Self-service sign-up for instructors. Creates a plain 'user': it can run its
# own sessions and see nothing but its own data, and a super promotes it to
# admin afterwards from the Admin panel. Deliberately distinct from
# /student/addstudent, which enrols a study participant and creates no login.
#
# Rate-limited by source address rather than by the submitted email, since the
# email is attacker-chosen here and would make the limit trivial to sidestep.
@api_routes.route('/api/v1/register', methods=['POST'])
# Per source address. Generous enough that someone fighting the password rules
# does not lock themselves out — every rejected attempt counts against it.
@limiter.limit("15 per minute")
def register():
    content = request.json or {}
    # Not passed through sanitize(): that HTML-escapes, and /login compares the
    # raw address, so an email containing & or ' would be stored in a form it
    # could never log in with. verify_fields() below is the character check.
    email = (content.get('email', None) or '').strip()
    password = content.get('password', None) or ''
    confirm = content.get('confirm', None) or ''
    ip = utility.get_client_ip(request)

    if not email or not password:
        return json_response({'message': 'Please provide both an email and password.'}, 400)
    valid, message = User.verify_fields(email=email)
    if not valid:
        return json_response({'message': message}, 400)
    if password != confirm:
        return json_response({'message': 'Confirmation password and password do not match.'}, 400)
    # Checked before the account exists, so a weak password never leaves a
    # half-created row behind.
    valid, message = User.validate_password(password)
    if not valid:
        return json_response({'message': message}, 400)

    success, user = database.add_user(email, role='user', password=password.strip())
    if not success:
        logging.info('Registration attempt for existing account {0} from {1}.'.format(email, ip))
        return json_response({'message': 'An account with that email already exists.'}, 400)

    user.last_login = datetime.now(timezone.utc).replace(tzinfo=None)
    database.save_changes()
    # Signed in immediately, exactly as a successful login would.
    session['user'] = user.json()
    session.permanent = True
    logging.info('Registered new user {0} from {1}.'.format(email, ip))
    return json_response(user.json())

# -------------------------
# Emailed links: forgot password, and finishing an invited account
# -------------------------

RESET_TTL = timedelta(hours=1)
# One reset email per account per this many seconds, however often it is asked.
RESET_COOLDOWN_SECONDS = 120

@api_routes.route('/api/v1/password/forgot', methods=['POST'])
@limiter.limit("10 per hour")
@limiter.limit("5 per hour", key_func=get_request_username)
def forgot_password():
    # Always the same answer, whether or not the address has an account, so
    # the form cannot be used to find out who is registered.
    email = ((request.get_json(silent=True) or {}).get('email') or '').strip()
    answer = json_response({'message': 'If that email has an account, a reset link is on its way.'})
    user = database.get_users(email=email) if email else None
    if user is None or user.locked:
        return answer
    age = database.latest_account_token_age(user.id, AccountToken.RESET)
    if age is not None and age < RESET_COOLDOWN_SECONDS:
        return answer
    token = database.create_account_token(user.id, AccountToken.RESET, RESET_TTL)
    emails.password_reset(user.email, token)
    logging.info('Password reset requested for {0} from {1}.'.format(email, utility.get_client_ip(request)))
    return answer

@api_routes.route('/api/v1/password/token', methods=['POST'])
@limiter.limit("30 per minute")
def check_account_token():
    # What the set-password page shows before asking for a password. POST so
    # the token stays out of access logs.
    token = database.get_account_token((request.get_json(silent=True) or {}).get('token'))
    user = database.get_users(id=token.user_id) if token else None
    if user is None:
        return json_response({'message': 'This link has expired or was already used.'}, 404)
    return json_response({'email': user.email, 'purpose': token.purpose})

@api_routes.route('/api/v1/password/reset', methods=['POST'])
@limiter.limit("20 per hour")
def reset_password_with_token():
    content = request.get_json(silent=True) or {}
    password = content.get('password') or ''
    if password != (content.get('confirm') or ''):
        return json_response({'message': 'Confirmation password and password do not match.'}, 400)
    token = database.get_account_token(content.get('token'))
    user = database.get_users(id=token.user_id) if token else None
    if user is None:
        return json_response({'message': 'This link has expired or was already used.'}, 400)
    if user.locked:
        return json_response({'message': 'Your account has been locked. Please contact your IT department.'}, 400)
    success, message = user.set_password(password)
    if not success:
        return json_response({'message': message}, 400)
    database.save_changes()
    database.use_account_tokens(user.id)
    RedisLogin.unlock_login(user.email)
    # Signed straight in, as after registering.
    user.last_login = datetime.now(timezone.utc).replace(tzinfo=None)
    database.save_changes()
    session['user'] = user.json()
    session.permanent = True
    logging.info('Password set via emailed {0} link for {1}.'.format(token.purpose, user.email))
    return json_response(user.json())

@api_routes.route('/api/v1/logout', methods=['POST'])
@wrappers.verify_login()
def logout(**kwargs):
    session.clear()
    return json_response()

@api_routes.route('/api/v1/me', methods=['GET'])
@wrappers.verify_login()
def me(user, **kwargs):
    if user:
        return json_response(user)
    else:
        return json_response(status=400)

@api_routes.route('/api/v1/password', methods=['POST'])
@wrappers.verify_login()
def change_password(**kwargs):
    content = request.json
    current_password = content.get('password', None)
    new_password = content.get('new', None)
    confirm_password = content.get('confirm', None)
    success = False
    message = ''
    session_user = session.get('user', None)
    if new_password != confirm_password:
        message = 'Confirmation password and password do not match.'
    elif session_user:
        user = database.get_users(id=session_user['id'])
        if user and user.verify_password(current_password):
            success, message = user.set_password(new_password)
            if success:
                session['user'] = user.json()
                database.save_changes()
                # Any reset link still in an inbox is now stale.
                database.use_account_tokens(user.id)
                return json_response()
        else:
            message = 'Password was not correct.'
    return json_response({'message': message}, 400)

@api_routes.route('/api/v1/email', methods=['POST'])
@wrappers.verify_login()
def change_email(**kwargs):
    # Mirrors change_password: requires the current password, validates the
    # new address, and rejects duplicates.
    content = request.json
    current_password = content.get('password', None)
    new_email = (content.get('email', None) or '').strip()
    message = ''
    session_user = session.get('user', None)
    valid, message = User.verify_fields(email=new_email)
    if valid and session_user:
        user = database.get_users(id=session_user['id'])
        if user and user.verify_password(current_password):
            existing = database.get_users(email=new_email)
            if existing and existing.id != user.id:
                message = 'That email is already in use.'
            else:
                user.email = new_email
                session['user'] = user.json()
                database.save_changes()
                return json_response()
        else:
            message = 'Password was not correct.'
    return json_response({'message': message}, 400)

@api_routes.route('/api/v1/token', methods=['GET'])
@limiter.limit("5 per 5 second", key_func=get_request_client_id)
def get_access_token(**kwargs):
    client_id = request.headers.get('X-Client-Id', None)
    client_secret = request.headers.get('X-Client-Secret', None)
    if client_id and client_secret:
        api_client = database.get_api_clients(client_id=client_id)
        if api_client and api_client.verify_secret(client_secret):
            new_token = api_client.generate_token()
            database.save_changes()
            return json_response({'token': new_token})
        else:
            return json_response({'message': 'Invalid credentials.'}, 400)
    else:
        return json_response({'message': 'X-Client-Id and X-Client_Secret must be set.'}, 400)
