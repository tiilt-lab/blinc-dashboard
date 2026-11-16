"""wearable time sync: ms columns, EEG metrics, sync markers

Millisecond session-clock columns for the streams that get aligned with
each other: speaker_hr_metrics.time_ms/beat_ms (heart-rate strap
notifications and their last beat) and transcript.start_ms/length_ms (the
audio service's own float stamps, which start_time/length round to whole
seconds). All nullable: rows stored before this revision keep NULL and read
as before. New tables speaker_eeg_metrics (Muse band power per electrode
per second) and sync_marker (sync taps, sync_taps.py).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-03 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('speaker_hr_metrics') as batch:
        batch.add_column(sa.Column('time_ms', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('beat_ms', sa.BigInteger(), nullable=True))
    with op.batch_alter_table('transcript') as batch:
        batch.add_column(sa.Column('start_ms', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('length_ms', sa.Integer(), nullable=True))

    op.create_table(
        'speaker_eeg_metrics',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('session_device_id', sa.Integer(), nullable=False),
        sa.Column('speaker_id', sa.Integer(), nullable=True),
        sa.Column('speaker_alias', sa.String(length=64), nullable=False),
        sa.Column('sensor_name', sa.String(length=64), nullable=True),
        sa.Column('time_stamp', sa.Integer(), nullable=False),
        sa.Column('time_ms', sa.BigInteger(), nullable=False),
        sa.Column('channel', sa.String(length=8), nullable=False),
        sa.Column('delta', sa.Float(), nullable=True),
        sa.Column('theta', sa.Float(), nullable=True),
        sa.Column('alpha', sa.Float(), nullable=True),
        sa.Column('beta', sa.Float(), nullable=True),
        sa.Column('gamma', sa.Float(), nullable=True),
        sa.Column('std_uv', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['session_device_id'], ['session_device.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_speaker_eeg_metrics_device', 'speaker_eeg_metrics',
                    ['session_device_id', 'time_stamp'])

    op.create_table(
        'sync_marker',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('session_device_id', sa.Integer(), nullable=False),
        sa.Column('speaker_id', sa.Integer(), nullable=True),
        sa.Column('sync_id', sa.String(length=40), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('sensor_kind', sa.String(length=16), nullable=True),
        sa.Column('sensor_name', sa.String(length=64), nullable=True),
        sa.Column('time_ms', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['session_device_id'], ['session_device.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sync_marker_device', 'sync_marker', ['session_device_id', 'time_ms'])


def downgrade():
    # drop_table takes each index with it; dropping the index first fails
    # (1553: the FK on session_device_id needs it).
    op.drop_table('sync_marker')
    op.drop_table('speaker_eeg_metrics')
    with op.batch_alter_table('transcript') as batch:
        batch.drop_column('length_ms')
        batch.drop_column('start_ms')
    with op.batch_alter_table('speaker_hr_metrics') as batch:
        batch.drop_column('beat_ms')
        batch.drop_column('time_ms')
