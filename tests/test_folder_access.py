"""Tests for folder sharing rules (src/server/folder_access.py).

Lookups are injected, so the rules run without Flask or a database (CI has
neither), in the same style as test_authz.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "server"))

import folder_access as fa  # noqa: E402


class _Folder:
    def __init__(self, id, owner_id, parent=None):
        self.id = id
        self.owner_id = owner_id
        self.parent = parent


class _Session:
    def __init__(self, owner_id, folder=None):
        self.owner_id = owner_id
        self.folder = folder


ALICE = {"id": 1, "role": "user"}
BOB = {"id": 2, "role": "user"}
CAROL = {"id": 3, "role": "user"}
ADMIN = {"id": 9, "role": "admin"}
SUPER = {"id": 8, "role": "super"}

# Alice's tree:  10 Study ─ 11 Section A ─ 12 Week 1
#                        └ 13 Section B
# Bob made 14 inside Section A.  Carol owns 20, unrelated.
FOLDERS = {
    10: _Folder(10, owner_id=1),
    11: _Folder(11, owner_id=1, parent=10),
    12: _Folder(12, owner_id=1, parent=11),
    13: _Folder(13, owner_id=1, parent=10),
    14: _Folder(14, owner_id=2, parent=11),
    20: _Folder(20, owner_id=3),
}


def _grants(table):
    return lambda user_id: table.get(user_id, {})


def level(folder_id, user, grants=None):
    return fa.effective_level(folder_id, user, FOLDERS.get, _grants(grants or {}))


def test_owner_is_manager_of_own_tree():
    assert level(10, ALICE) == fa.MANAGER
    assert level(12, ALICE) == fa.MANAGER


def test_stranger_has_no_access():
    assert level(10, BOB) is None
    assert level(20, ALICE) is None


def test_grant_inherits_down_not_up():
    g = {2: {11: fa.VIEWER}}
    assert level(11, BOB, g) == fa.VIEWER
    assert level(12, BOB, g) == fa.VIEWER
    assert level(10, BOB, g) is None
    assert level(13, BOB, g) is None


def test_highest_grant_on_the_path_wins():
    g = {2: {10: fa.EDITOR, 12: fa.VIEWER}}
    assert level(12, BOB, g) == fa.EDITOR
    g = {2: {10: fa.VIEWER, 12: fa.MANAGER}}
    assert level(12, BOB, g) == fa.MANAGER
    assert level(11, BOB, g) == fa.VIEWER


def test_creator_of_subfolder_manages_it():
    # Bob holds only editor on the parent but made folder 14, so he manages it.
    assert level(14, BOB, {2: {11: fa.EDITOR}}) == fa.MANAGER
    # And the owner of the enclosing tree still manages it too.
    assert level(14, ALICE) == fa.MANAGER


def test_roles():
    assert level(20, SUPER) == fa.MANAGER
    assert level(20, ADMIN) == fa.VIEWER
    assert level(20, ADMIN, {9: {20: fa.EDITOR}}) == fa.EDITOR


def test_role_level_is_what_a_role_alone_reaches():
    # What the share dialog lists for admins and supers on every folder.
    assert fa.role_level('super') == fa.MANAGER
    assert fa.role_level('admin') == fa.VIEWER
    assert fa.role_level('user') is None
    assert fa.role_level(None) is None


def test_missing_folder_grants_nothing():
    assert level(999, SUPER) is None
    assert level(None, ALICE) is None


def test_parent_cycle_terminates():
    loop = {1: _Folder(1, owner_id=5, parent=2), 2: _Folder(2, owner_id=5, parent=1)}
    assert fa.effective_level(1, BOB, loop.get, _grants({})) is None
    assert fa.levels_for_all(list(loop.values()), BOB, _grants({})) == {}


def test_levels_for_all_matches_effective_level():
    g = {2: {11: fa.EDITOR, 20: fa.VIEWER}}
    everyone = fa.levels_for_all(list(FOLDERS.values()), BOB, _grants(g))
    assert everyone == {11: fa.EDITOR, 12: fa.EDITOR, 14: fa.MANAGER, 20: fa.VIEWER}
    for fid in FOLDERS:
        assert everyone.get(fid) == level(fid, BOB, g)


def test_admin_listing_sees_every_folder():
    everyone = fa.levels_for_all(list(FOLDERS.values()), ADMIN, _grants({}))
    assert set(everyone) == set(FOLDERS)


def _session(session, user, write, grants=None):
    return fa.session_allowed(session, user, write, lambda fid: level(fid, user, grants))


def test_session_viewer_reads_but_cannot_write():
    s = _Session(owner_id=1, folder=12)
    g = {2: {10: fa.VIEWER}}
    assert _session(s, BOB, write=False, grants=g)
    assert not _session(s, BOB, write=True, grants=g)


def test_session_editor_writes():
    s = _Session(owner_id=1, folder=12)
    assert _session(s, BOB, write=True, grants={2: {11: fa.EDITOR}})


def test_session_outside_shared_tree_stays_private():
    assert not _session(_Session(owner_id=1, folder=13), BOB, False, {2: {11: fa.MANAGER}})
    assert not _session(_Session(owner_id=1, folder=None), BOB, False, {2: {10: fa.MANAGER}})


def test_session_roles_unchanged():
    s = _Session(owner_id=3)
    assert _session(s, CAROL, write=True)
    assert _session(s, SUPER, write=True)
    assert _session(s, ADMIN, write=False)
    assert not _session(s, ADMIN, write=True)
    assert not _session(None, SUPER, write=False)


def test_who_may_manage_members():
    assert fa.can_manage_members(fa.MANAGER, BOB)
    assert not fa.can_manage_members(fa.EDITOR, BOB)
    assert fa.can_manage_members(fa.VIEWER, ADMIN)
    assert fa.can_manage_members(None, SUPER)
