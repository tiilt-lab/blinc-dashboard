"""add speaker_hr_metrics table

Heart-rate/RR samples from Polar straps streamed off the byod join page.

NOTE (same caveat as f1a2b3c4d5e6): this repo's migration history has three
divergent heads and the live DB tracks the 42a3c6485423 lineage. Apply this
one with a targeted upgrade, not a blanket one:

    flask --app app db upgrade c4d5e6f7a8b9

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'speaker_hr_metrics',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('session_device_id', sa.Integer(), nullable=False),
        sa.Column('speaker_id', sa.Integer(), nullable=True),
        sa.Column('speaker_alias', sa.String(length=64), nullable=False),
        sa.Column('sensor_name', sa.String(length=64), nullable=True),
        sa.Column('time_stamp', sa.Integer(), nullable=False),
        sa.Column('heart_rate', sa.Integer(), nullable=False),
        sa.Column('rr_ms', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['session_device_id'], ['session_device.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_speaker_hr_metrics_device', 'speaker_hr_metrics',
                    ['session_device_id', 'time_stamp'])


def downgrade():
    # drop_table takes the index with it; dropping it first fails (1553,
    # the FK on session_device_id needs it).
    op.drop_table('speaker_hr_metrics')
