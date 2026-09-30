"""Phase 1 indexes: hot-path lookups and the two check-then-insert races

Adds (see docs/infra-audit-2026-09-30.md, section F "Missing indexes"):

  ux_sd_processing_key      UNIQUE session_device(processing_key)
      Resolved on every pod request and every processing callback; today a
      full scan, and nothing stops two pods sharing a key.
  ux_user_email             UNIQUE user(email)
      register / invite check-then-insert; the constraint closes the race.
  ix_transcript_speaker_id  transcript(speaker_id)
      get_transcripts(speaker_id=) was a table scan.
  ix_session_passcode       session(passcode)
      The anonymous student join lookup.
  ix_session_owner_created  session(owner_id, creation_date)
      The per-owner sessions list in creation order.

Production was checked for duplicate processing_key / email values before
this was written (none). UNIQUE on a nullable column allows many NULLs in
MySQL, so pods that have not been issued a key are unaffected. The FK on
session.owner_id keeps its own auto-created index, so the downgrade's drop
of the composite index is safe. The models declare the same names, so
autogenerate stays quiet.

Revision ID: f8ae4e72c79c
Revises: b7e4d2a1c9f0
Create Date: 2026-09-30

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'f8ae4e72c79c'
down_revision = 'b7e4d2a1c9f0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ux_sd_processing_key', 'session_device', ['processing_key'], unique=True)
    op.create_index('ux_user_email', 'user', ['email'], unique=True)
    op.create_index('ix_transcript_speaker_id', 'transcript', ['speaker_id'])
    op.create_index('ix_session_passcode', 'session', ['passcode'])
    op.create_index('ix_session_owner_created', 'session', ['owner_id', 'creation_date'])


def downgrade():
    op.drop_index('ix_session_owner_created', table_name='session')
    op.drop_index('ix_session_passcode', table_name='session')
    op.drop_index('ix_transcript_speaker_id', table_name='transcript')
    op.drop_index('ux_user_email', table_name='user')
    op.drop_index('ux_sd_processing_key', table_name='session_device')
