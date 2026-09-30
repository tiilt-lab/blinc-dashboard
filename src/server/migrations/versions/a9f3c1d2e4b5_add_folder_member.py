"""add folder_member for folder sharing

One row per (folder, user) grant at level viewer / editor / manager. Grants
inherit down the folder tree; see src/server/folder_access.py. Rows go away
with their folder or user (ON DELETE CASCADE).

Revision ID: a9f3c1d2e4b5
Revises: e2b3c4d5f6a7
Create Date: 2026-09-24

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9f3c1d2e4b5'
down_revision = 'e2b3c4d5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'folder_member',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('folder_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('level', sa.String(length=16), nullable=False),
        sa.Column('granted_by', sa.Integer(), nullable=True),
        sa.Column('granted_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['folder_id'], ['folder.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['granted_by'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('folder_id', 'user_id', name='uq_folder_member_folder_user'),
    )
    op.create_index('ix_folder_member_folder_id', 'folder_member', ['folder_id'])
    op.create_index('ix_folder_member_user_id', 'folder_member', ['user_id'])


def downgrade():
    # drop_table takes the indexes with it; dropping them first fails
    # (1553, the FKs need them).
    op.drop_table('folder_member')
