"""wearable raw blocks: native-rate EEG and accelerometer

New table wearable_raw_block: one row per wearer per stream per second
holding the device's raw integer counts (raw_signals.py), a Muse's 256 Hz
EEG and 52 Hz accelerometer. Separate from speaker_eeg_metrics, which keeps
the per-second band power the dashboard charts.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-04 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'wearable_raw_block',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('session_device_id', sa.Integer(), nullable=False),
        sa.Column('speaker_id', sa.Integer(), nullable=True),
        sa.Column('speaker_alias', sa.String(length=64), nullable=False),
        sa.Column('sensor_kind', sa.String(length=16), nullable=False),
        sa.Column('sensor_name', sa.String(length=64), nullable=True),
        sa.Column('stream', sa.String(length=8), nullable=False),
        sa.Column('channels', sa.String(length=64), nullable=False),
        sa.Column('rate', sa.Float(), nullable=False),
        sa.Column('scale', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(length=8), nullable=False),
        sa.Column('sample_index', sa.BigInteger(), nullable=False),
        sa.Column('time_ms', sa.BigInteger(), nullable=False),
        sa.Column('n_samples', sa.Integer(), nullable=False),
        sa.Column('n_missing', sa.Integer(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(['session_device_id'], ['session_device.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_wearable_raw_block_device', 'wearable_raw_block',
                    ['session_device_id', 'stream', 'time_ms'])


def downgrade():
    # drop_table takes the index with it (the FK needs it; see e5f6a7b8c9d0).
    op.drop_table('wearable_raw_block')
