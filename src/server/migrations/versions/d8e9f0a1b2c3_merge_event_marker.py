"""merge: wearable time sync chain + event markers

event_marker (c7d8e9f0a1b2) revises main's head so session-exports and this
branch carry the same file; this empty revision joins it to the wearable
chain (e5f6a7b8c9d0 -> f6a7b8c9d0e1) so `upgrade head` has one head.

Revision ID: d8e9f0a1b2c3
Revises: f6a7b8c9d0e1, c7d8e9f0a1b2
Create Date: 2026-11-07 12:00:00
"""

revision = 'd8e9f0a1b2c3'
down_revision = ('f6a7b8c9d0e1', 'c7d8e9f0a1b2')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
