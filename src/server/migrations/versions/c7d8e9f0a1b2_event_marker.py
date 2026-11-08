"""event markers: session protocol moments from the join page

New table event_marker: one row per join, recording start and baseline phase
boundary (eyes open / eyes closed rest, the group game, the discussion
start) pressed on a join-page laptop, on the session clock in ms
(tables/event_marker.py).

Revises main's head, so the branches that carry it (session-exports and
physio-time-sync) add the same file; physio-time-sync joins it to its own
chain with a merge revision (d8e9f0a1b2c3).

Revision ID: c7d8e9f0a1b2
Revises: d4e5f6a7b8c9
Create Date: 2026-11-07 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7d8e9f0a1b2'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'event_marker',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('session_device_id', sa.Integer(), nullable=False),
        sa.Column('marker_id', sa.String(length=40), nullable=False),
        sa.Column('label', sa.String(length=32), nullable=False),
        sa.Column('time_stamp', sa.Integer(), nullable=False),
        sa.Column('time_ms', sa.BigInteger(), nullable=False),
        sa.Column('sensors', sa.String(length=255), nullable=True),
        sa.Column('detail', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['session_device_id'], ['session_device.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_device_id', 'marker_id', name='uq_event_marker_device_marker'),
    )
    op.create_index('ix_event_marker_device', 'event_marker', ['session_device_id', 'time_ms'])


def downgrade():
    # drop_table takes the index with it; MySQL refuses to drop an index
    # the FK still needs on its own.
    op.drop_table('event_marker')
