"""negotiation coding: session.negotiation_coding + the run and code tables

A Kellogg negotiation class wants each pod's transcript coded per utterance
(emotion, rights/interests/power, frame, listening) by the local LLM and
rolled up per team (src/server/negotiation_coding.py).

* session.negotiation_coding (nullable boolean): NULL/FALSE = off, the
  historic behaviour; TRUE = end_session queues a coding run per pod.
* negotiation_coding_run: one coding pass per pod (status, model, codebook
  version, teams, summary, timing). Cascades from session_device.
* negotiation_code: the four codes of one utterance in one run. Cascades
  from the run and from the transcript row.

Every step is guarded on the inspector so the migration is a no-op wherever
it already applied.

Revision ID: 4c5d6e7f8091
Revises: 3b4c5d6e7f80
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa


revision = '4c5d6e7f8091'
down_revision = '3b4c5d6e7f80'
branch_labels = None
depends_on = None


def _has_column(table, column):
    insp = sa.inspect(op.get_bind())
    return any(c['name'] == column for c in insp.get_columns(table))


def _has_table(table):
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade():
    if not _has_column('session', 'negotiation_coding'):
        op.add_column('session', sa.Column('negotiation_coding', sa.Boolean(), nullable=True))
    if not _has_table('negotiation_coding_run'):
        op.create_table(
            'negotiation_coding_run',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('session_device_id', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('model', sa.String(length=128), nullable=True),
            sa.Column('codebook_version', sa.String(length=32), nullable=True),
            sa.Column('teams', sa.Text(), nullable=True),
            sa.Column('summary', sa.Text(), nullable=True),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('utterances_coded', sa.Integer(), nullable=True),
            sa.Column('invalid_codes', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['session_device_id'], ['session_device.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_negotiation_coding_run_session_device_id', 'negotiation_coding_run', ['session_device_id'])
    if not _has_table('negotiation_code'):
        op.create_table(
            'negotiation_code',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('run_id', sa.Integer(), nullable=False),
            sa.Column('transcript_id', sa.Integer(), nullable=False),
            sa.Column('emotion', sa.String(length=16), nullable=True),
            sa.Column('rip', sa.String(length=16), nullable=True),
            sa.Column('frame', sa.String(length=32), nullable=True),
            sa.Column('listening', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['run_id'], ['negotiation_coding_run.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['transcript_id'], ['transcript.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_negotiation_code_run_id', 'negotiation_code', ['run_id'])
        op.create_index('ix_negotiation_code_transcript_id', 'negotiation_code', ['transcript_id'])


def downgrade():
    # drop_table takes the indexes and FKs with it (as b7e4d2a1c9f0 notes).
    if _has_table('negotiation_code'):
        op.drop_table('negotiation_code')
    if _has_table('negotiation_coding_run'):
        op.drop_table('negotiation_coding_run')
    if _has_column('session', 'negotiation_coding'):
        op.drop_column('session', 'negotiation_coding')
