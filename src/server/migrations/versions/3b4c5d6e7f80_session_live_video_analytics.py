"""session.live_video_analytics: per-session "record now, analyse later" video

Live video analytics is the tightest resource in a class (one ffmpeg decode
plus the GPU detectors per pod, ~3 cores each). A session created with
live_video_analytics = FALSE is recorded by the video service exactly as
before but not analysed during class; end_session queues a post-hoc VIDEO
leg for every pod that recorded video instead.

NULL (every row from before this migration) and TRUE both mean the historic
behaviour, so nothing changes for existing sessions. Guarded on the inspector
so the step is a no-op wherever the column already exists.

Revision ID: 3b4c5d6e7f80
Revises: 2a3b4c5d6e7f
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa


revision = '3b4c5d6e7f80'
down_revision = '2a3b4c5d6e7f'
branch_labels = None
depends_on = None


def _has_column(table, column):
    insp = sa.inspect(op.get_bind())
    return any(c['name'] == column for c in insp.get_columns(table))


def upgrade():
    if not _has_column('session', 'live_video_analytics'):
        op.add_column('session', sa.Column('live_video_analytics', sa.Boolean(), nullable=True))


def downgrade():
    if _has_column('session', 'live_video_analytics'):
        op.drop_column('session', 'live_video_analytics')
