"""reconcile the chain with the live schema (audit H3)

Production was ahead of this chain: objects created by hand, or by an older
SQLAlchemy, that no migration recorded, so a fresh `upgrade head` never
matched the live database (docs/infra-audit-2026-09-30.md, F). Every step is
guarded by the inspector: on production, and on any database that already
has the object, it is a no-op.

  session_device.posthoc_analyzed_date  stamped when a post-hoc run completes
  idx_transcript_device_start           transcript(session_device_id, start_time)
  idx_svm_device_time                   speaker_video_metrics(session_device_id, time_stamp)
  <table>_chk_N  CHECK (col IN (0,1))   SQLAlchemy < 1.4 emitted these for Boolean
  user_system_interaction               model-less table written by other tooling
  plain FK indexes (owner_id, ...)      MySQL silently drops an auto-created FK
                                        index once a composite index can serve
                                        the FK, and renames it after a named FK
                                        re-add; production's (dump-restored)
                                        indexes are not flagged, so fresh builds
                                        drifted. Restored under the live names.

NOT added: session_device.unique_session_name. The model declared it but no
deployment ever had it and the join path handles duplicate names itself; the
model was corrected instead (see SessionDevice).

Revision ID: 1f2e3d4c5b6a
Revises: f8ae4e72c79c
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa


revision = '1f2e3d4c5b6a'
down_revision = 'f8ae4e72c79c'
branch_labels = None
depends_on = None


# (table, constraint name, column) — names match what MySQL generated live.
CHECKS = [
    ('device', 'device_chk_1', 'connected'),
    ('device', 'device_chk_2', 'archived'),
    ('device', 'device_chk_3', 'is_pod'),
    ('session_device', 'session_device_chk_1', 'connected'),
    ('session_device', 'session_device_chk_2', 'button_pressed'),
    ('session_device', 'session_device_chk_3', 'removed'),
    ('transcript', 'transcript_chk_1', 'question'),
    ('user', 'user_chk_1', 'change_password'),
    ('user', 'user_chk_2', 'locked'),
]

# (table, index name, columns) — composite indexes created by hand on prod.
COMPOSITES = [
    ('transcript', 'idx_transcript_device_start', ['session_device_id', 'start_time']),
    ('speaker_video_metrics', 'idx_svm_device_time', ['session_device_id', 'time_stamp']),
]

# (table, column) whose FK index is named after the column on prod.
FK_INDEXES = [
    ('session', 'owner_id'),
    ('transcript', 'session_device_id'),
    ('speaker_video_metrics', 'session_device_id'),
]


def _insp():
    return sa.inspect(op.get_bind())


def _indexes(table):
    return {i['name']: list(i['column_names']) for i in _insp().get_indexes(table)}


def _checks(table):
    return {c['name'] for c in _insp().get_check_constraints(table)}


def _has_column(table, column):
    return any(c['name'] == column for c in _insp().get_columns(table))


def ensure_fk_index(table, column, name=None):
    # Make sure a single-column index named `name` (default: the column)
    # exists, renaming a MySQL-generated one on the same column if that is
    # what is there.
    name = name or column
    indexes = _indexes(table)
    if name in indexes:
        return
    for other, cols in indexes.items():
        if cols == [column] and other.startswith(table + '_ibfk_'):
            op.execute('ALTER TABLE `{0}` RENAME INDEX `{1}` TO `{2}`'.format(table, other, name))
            return
    op.create_index(name, table, [column])


def upgrade():
    if not _has_column('session_device', 'posthoc_analyzed_date'):
        # AFTER keeps the live column order (posthoc_models came later).
        op.execute('ALTER TABLE session_device ADD COLUMN posthoc_analyzed_date DATETIME NULL AFTER embeddings')

    for table, name, columns in COMPOSITES:
        if name not in _indexes(table):
            op.create_index(name, table, columns)
    for table, column in FK_INDEXES:
        ensure_fk_index(table, column)

    for table, name, column in CHECKS:
        if name not in _checks(table):
            op.execute('ALTER TABLE `{0}` ADD CONSTRAINT `{1}` CHECK (`{2}` IN (0,1))'.format(table, name, column))

    if not _insp().has_table('user_system_interaction'):
        op.create_table(
            'user_system_interaction',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('sessionid', sa.Integer(), nullable=False),
            sa.Column('sessiondeviceid', sa.Integer(), nullable=False),
            sa.Column('username', sa.String(length=30), nullable=False),
            sa.Column('action', sa.Text(), nullable=False),
            sa.Column('creation_date', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade():
    # The plain FK indexes stay: the FKs need them and the tables' own
    # migrations drop them with the table.
    if _insp().has_table('user_system_interaction'):
        op.drop_table('user_system_interaction')
    for table, name, column in CHECKS:
        if name in _checks(table):
            op.execute('ALTER TABLE `{0}` DROP CHECK `{1}`'.format(table, name))
    for table, name, columns in COMPOSITES:
        if name in _indexes(table):
            op.drop_index(name, table_name=table)
    if _has_column('session_device', 'posthoc_analyzed_date'):
        op.drop_column('session_device', 'posthoc_analyzed_date')
