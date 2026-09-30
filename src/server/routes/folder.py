from flask import Blueprint, request
from datetime import timedelta
import database
import folder_access
from utility import json_response
import wrappers
from tables.folder import Folder
from tables.user import User
from tables.account_token import AccountToken
from app import limiter
from flask import session as flask_session
import emails

api_routes = Blueprint('folder', __name__)


def _can_file_into(parent, user):
    # Creating or moving something into a folder needs editor there; the top
    # level (-1 / None) is everyone's own.
    if parent is None or parent == -1:
        return True
    return folder_access.at_least(wrappers.folder_level(parent, user), folder_access.EDITOR)

@api_routes.route('/api/folders', methods=['GET'])
@wrappers.verify_login()
def get_folders(user, **kwargs):
    # Everything the caller can reach: their own folders, folders shared with
    # them (and everything beneath those), and for admins and supers every
    # account's folders — sessions follow folders, so a session someone may
    # read is never filed under a folder they cannot see. A shared folder
    # whose parent the caller cannot see is reported at the top level.
    folders = database.get_folders()
    levels = folder_access.levels_for_all(folders, user, database.get_folder_member_levels)
    owner_emails = database.get_user_emails() if any(
        f.owner_id != user['id'] for f in folders if f.id in levels) else {}
    result = []
    for folder in folders:
        level = levels.get(folder.id)
        if level is None:
            continue
        data = folder.json()
        if folder.parent is not None and folder.parent not in levels:
            data['parent'] = None
        data['owned'] = folder.owner_id == user['id']
        data['access'] = level
        data['can_manage_members'] = folder_access.can_manage_members(level, user)
        if not data['owned']:
            data['owner'] = owner_emails.get(folder.owner_id)
        result.append(data)
    return json_response(result)

@api_routes.route('/api/folder', methods=['POST'])
@wrappers.verify_login()
def add_folder(user, **kwargs):
    # Whoever creates a folder owns it, and so manages it — including a
    # subfolder made inside someone else's shared folder.
    owner_id = user['id']
    name = request.json.get('name', 'Folder')
    parent = request.json.get('parent', None)
    if parent == -1:
        parent = None
    valid, message = Folder.verify_fields(name=name)
    if not valid:
        return json_response({'message': message}, 400)
    if not _can_file_into(parent, user):
        return json_response({'message': 'Folder does not exist.'}, 404)
    folder = database.add_folder(owner_id=owner_id, name=name, parent=parent)
    return json_response(folder.json())

@api_routes.route('/api/folders/<int:folder_id>', methods=['POST'])
@wrappers.verify_login()
@wrappers.verify_folder_access
def update_folder(folder_id, user, **kwargs):
    parent = request.json.get('parent', None)
    name = request.json.get('name', None)
    if name:
        valid, message = Folder.verify_fields(name=name)
        if not valid:
            return json_response({'message': message}, 400)
    if parent:
        if database.is_child_folder(folder_id, parent) or folder_id == parent:
            return json_response({'message':'Invalid location.'}, 400)
    if not _can_file_into(parent, user):
        return json_response({'message': 'Folder does not exist.'}, 404)
    folder = database.update_folder(folder_id, name, parent)
    return json_response(folder.json())

@api_routes.route('/api/folders/<int:folder_id>', methods=['DELETE'])
@wrappers.verify_login()
@wrappers.verify_folder_access
def delete_folder(folder_id, **kwargs):
    success, message = database.delete_folder(folder_id)
    return json_response({'message': message}, 200 if success else 400)


# -------------------------
# Sharing
# -------------------------

def _members_json(folder, user, level):
    # Direct grants on this folder, plus what flows down from above it: owners
    # of ancestor folders (managers of everything beneath) and their grants.
    # One entry per person, at their highest level.
    chain = database.get_folder_ancestors(folder.id)
    emails = database.get_user_emails()
    by_folder = {}
    for member, email in database.get_folder_members([f.id for f in chain]):
        by_folder.setdefault(member.folder_id, []).append((member, email))
    people = {}
    never_signed_in = database.get_user_ids_never_signed_in()

    def offer(user_id, email, member_level, source):
        current = people.get(user_id)
        if current is None or not folder_access.at_least(current['level'], member_level):
            people[user_id] = dict(user_id=user_id, email=email, level=member_level,
                                   inherited_from=source, invited=user_id in never_signed_in)

    # Walk from the top of the tree down so a direct grant on this folder wins
    # a tie with an inherited one.
    for f in reversed(chain):
        source = None if f.id == folder.id else dict(id=f.id, name=f.name)
        if f.id != folder.id:
            offer(f.owner_id, emails.get(f.owner_id), folder_access.MANAGER, source)
        for member, email in by_folder.get(f.id, []):
            offer(member.user_id, email, member.level, source)
    people.pop(folder.owner_id, None)
    return dict(
        owner=dict(user_id=folder.owner_id, email=emails.get(folder.owner_id)),
        members=sorted(people.values(), key=lambda p: (p['inherited_from'] is None, p['email'] or '')),
        access=level,
        can_manage_members=folder_access.can_manage_members(level, user),
    )

@api_routes.route('/api/folders/<int:folder_id>/members', methods=['GET'])
@wrappers.verify_login()
@wrappers.verify_folder_level(folder_access.VIEWER)
def get_folder_members(folder, folder_level, user, **kwargs):
    return json_response(_members_json(folder, user, folder_level))

def _caller_key():
    # Per signed-in account, so one person cannot mass-invite from many IPs.
    return str((flask_session.get('user') or {}).get('id', 'anon'))

@api_routes.route('/api/folders/<int:folder_id>/members', methods=['PUT'])
@wrappers.verify_login()
@limiter.limit("60 per hour", key_func=_caller_key)
@wrappers.verify_folder_level(folder_access.VIEWER)
def set_folder_member(folder, folder_level, user, **kwargs):
    # Add someone, or change their level. Admins and the folder's managers
    # only. Existing accounts get a notification email; an address with no
    # account gets one created and an invite to set its password.
    if not folder_access.can_manage_members(folder_level, user):
        return json_response({'message': 'Only a manager of this folder or an admin can share it.'}, 403)
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip()
    level = body.get('level')
    if level not in folder_access.LEVELS:
        return json_response({'message': 'Level must be one of: {0}.'.format(', '.join(folder_access.LEVELS))}, 400)
    if not email or '@' not in email:
        return json_response({'message': 'Enter an email address.'}, 400)
    valid, message = User.verify_fields(email=email)
    if not valid:
        return json_response({'message': message}, 400)
    member = database.get_users(email=email)
    invite_token = None
    if member is None:
        member, invite_token = database.invite_user(email)
        if member is None:
            return json_response({'message': invite_token}, 400)
    if member.id == folder.owner_id:
        return json_response({'message': 'The owner already manages this folder.'}, 400)
    previous = database.get_folder_member_levels(member.id).get(folder.id)
    database.set_folder_member(folder.id, member.id, level, granted_by=user['id'])
    if invite_token is None and member.last_login is None:
        # Invited earlier but never signed in: a fresh invite beats a share
        # notice they cannot act on yet.
        invite_token = database.create_account_token(member.id, AccountToken.INVITE, timedelta(days=7))
    if invite_token is not None:
        emails.invite(member.email, user['email'], invite_token, folder_name=folder.name, level=level)
    elif previous != level:
        emails.folder_shared(member.email, user['email'], folder.name, folder.id, level,
                             changed=previous is not None)
    return json_response(_members_json(folder, user, folder_level))

@api_routes.route('/api/folders/<int:folder_id>/members/<int:member_id>', methods=['DELETE'])
@wrappers.verify_login()
@wrappers.verify_folder_level(folder_access.VIEWER)
def remove_folder_member(folder, folder_level, user, member_id, **kwargs):
    # Managers and admins remove anyone; anyone may remove themselves (leave).
    if member_id != user['id'] and not folder_access.can_manage_members(folder_level, user):
        return json_response({'message': 'Only a manager of this folder or an admin can change who has access.'}, 403)
    if not database.remove_folder_member(folder.id, member_id):
        return json_response({'message': 'That person has no direct access to this folder.'}, 404)
    level = wrappers.folder_level(folder.id, user)
    if level is None:
        return json_response({'members': [], 'access': None, 'can_manage_members': False})
    return json_response(_members_json(folder, user, level))
