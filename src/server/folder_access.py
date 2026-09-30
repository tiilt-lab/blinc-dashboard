"""Folder sharing: who may do what inside a folder tree.

Every folder has an owner (its creator) and a list of members, each at one of
three levels:

    viewer   see the folder and everything in it — sessions, metrics,
             transcripts, audio and video
    editor   viewer, plus create sessions and subfolders in it, and rename,
             move, re-analyse or delete the sessions inside
    manager  editor, plus rename, move or delete the folder itself and decide
             who else is a member

A grant flows down the tree: access to a folder is access to every subfolder
beneath it, and where a person holds grants at several heights the highest
level wins. There is no way to take access away lower down — a folder that
must stay private belongs outside the shared tree.

The folder's owner is always its manager, and a super is manager everywhere.
An admin reads every folder (as they already read every session) and may
manage anyone's membership, but gains no editor rights from the role alone.

Like authz.py this module is import-light: the two lookups it needs are passed
in, so the rules are unit-testable without Flask or a database.
"""

VIEWER = 'viewer'
EDITOR = 'editor'
MANAGER = 'manager'
LEVELS = (VIEWER, EDITOR, MANAGER)
_RANK = {None: 0, VIEWER: 1, EDITOR: 2, MANAGER: 3}


def at_least(level, required):
    return _RANK.get(level, 0) >= _RANK[required]


def higher(a, b):
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def role_level(role):
    """The level an account role carries in every folder by itself: supers
    manage everywhere, admins read everywhere, ordinary users nothing."""
    if role == 'super':
        return MANAGER
    if role == 'admin':
        return VIEWER
    return None


def _role_floor(user):
    return role_level(user.get('role', 'user'))


def effective_level(folder_id, user, get_folder, member_levels):
    """The caller's level in ``folder_id``, or None for no access.

    - get_folder(id)          -> object with .owner_id and .parent, or None
    - member_levels(user_id)  -> {folder_id: level} of the user's direct grants
    """
    if not user or folder_id is None or get_folder(folder_id) is None:
        return None
    level = _role_floor(user)
    grants = member_levels(user['id'])
    seen = set()
    current = folder_id
    while current is not None and current not in seen:
        seen.add(current)
        folder = get_folder(current)
        if folder is None:
            break
        if folder.owner_id == user['id']:
            return MANAGER
        level = higher(level, grants.get(current))
        current = folder.parent
    return level


def levels_for_all(folders, user, member_levels):
    """{folder_id: level} for every folder in ``folders`` the caller can reach.

    One pass over the whole list, for the folder listing — cheaper than asking
    effective_level folder by folder, which would re-walk shared ancestors.
    """
    by_id = {f.id: f for f in folders}
    grants = member_levels(user['id'])
    floor = _role_floor(user)
    memo = {}

    def resolve(folder_id, trail):
        if folder_id in memo:
            return memo[folder_id]
        folder = by_id.get(folder_id)
        if folder is None or folder_id in trail:
            return None
        if folder.owner_id == user['id']:
            level = MANAGER
        else:
            inherited = resolve(folder.parent, trail | {folder_id}) if folder.parent is not None else None
            level = higher(higher(floor, grants.get(folder_id)), inherited)
        memo[folder_id] = level
        return level

    result = {}
    for folder_id in by_id:
        level = resolve(folder_id, frozenset())
        if level is not None:
            result[folder_id] = level
    return result


def can_manage_members(level, user):
    """Admins organise membership on any folder; otherwise it takes a manager."""
    return at_least(level, MANAGER) or (user or {}).get('role') in ('admin', 'super')


def session_allowed(session, user, write, folder_level):
    """Whether ``user`` may read (or, with write, change) ``session``.

    folder_level(folder_id) -> the caller's level in that folder. Owner and
    super may always write; an admin may always read. Beyond that the session's
    folder decides: viewer to read, editor to write.
    """
    if session is None or not user:
        return False
    role = user.get('role', 'user')
    if role == 'super' or session.owner_id == user['id']:
        return True
    if role == 'admin' and not write:
        return True
    if session.folder is None:
        return False
    return at_least(folder_level(session.folder), EDITOR if write else VIEWER)
