"""add account_token for emailed password-reset and invite links

Revision ID: b7e4d2a1c9f0
Revises: a9f3c1d2e4b5
Create Date: 2026-09-24

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e4d2a1c9f0'
down_revision = 'a9f3c1d2e4b5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'account_token',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(length=16), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_account_token_user_id', 'account_token', ['user_id'])


def downgrade():
    # drop_table takes the index with it; dropping it first fails (1553,
    # the FK on user_id needs it).
    op.drop_table('account_token')
