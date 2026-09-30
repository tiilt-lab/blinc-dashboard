"""ON DELETE rules for the session / user / folder object graphs (audit M1)

Deleting a session, user or folder was a hand-written multi-statement
cascade in database.py that every path had to get right (delete_topic_model
and delete_user failed with FK 1451 when a model or folder was in use). The
database owns the rules now; delete_* in database.py is one DELETE each.

  CASCADE   children that exist only as part of their parent
  SET NULL  optional references (a session's folder / topic model, a folder's
            parent, a metric row's speaker), so deleting the target lifts the
            referrer out instead of blocking

Deliberately unchanged: session.owner_id (a session is the unit of research
data; deleting an account must delete its sessions explicitly, never as a
side effect) and session_device.device_id (a pod in use must not vanish).

Each FK is dropped and re-created under its live name; a FK that already has
the rule is skipped, so re-running is safe. MySQL renames an auto-created FK
index to the constraint name on a named re-add (fresh builds only, see
1f2e3d4c5b6a), so the index is normalised back afterwards.

Revision ID: 2a3b4c5d6e7f
Revises: 1f2e3d4c5b6a
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa


revision = '2a3b4c5d6e7f'
down_revision = '1f2e3d4c5b6a'
branch_labels = None
depends_on = None


# (table, fk name, column, referred table, rule, index name)
RULES = [
    ('session_device', 'session_device_ibfk_2', 'session_id', 'session', 'CASCADE', 'session_id'),
    ('transcript', 'transcript_ibfk_1', 'session_device_id', 'session_device', 'CASCADE', 'session_device_id'),
    ('keyword_usage', 'keyword_usage_ibfk_1', 'transcript_id', 'transcript', 'CASCADE', 'transcript_id'),
    ('keyword', 'keyword_ibfk_1', 'session_id', 'session', 'CASCADE', 'session_id'),
    ('llm_feedback_report', 'llm_feedback_report_ibfk_2', 'session_id', 'session', 'CASCADE', 'session_id'),
    ('llm_question_answer', 'llm_question_answer_ibfk_2', 'session_id', 'session', 'CASCADE', 'session_id'),
    ('session_synthesized_report', 'session_synthesized_report_ibfk_2', 'session_id', 'session', 'CASCADE', 'session_id'),
    ('topic_model', 'topic_model_ibfk_1', 'owner_id', 'user', 'CASCADE', 'owner_id'),
    ('keyword_list', 'keyword_list_ibfk_1', 'owner_id', 'user', 'CASCADE', 'owner_id'),
    ('keyword_list_item', 'keyword_list_item_ibfk_1', 'keyword_list_id', 'keyword_list', 'CASCADE', None),
    ('api_client', 'api_client_ibfk_1', 'user_id', 'user', 'CASCADE', 'user_id'),
    ('folder', 'folder_ibfk_1', 'owner_id', 'user', 'CASCADE', 'owner_id'),
    ('session', 'session_ibfk_2', 'topic_model_id', 'topic_model', 'SET NULL', 'topic_model_id'),
    ('session', 'session_folder', 'folder', 'folder', 'SET NULL', 'session_folder'),
    ('folder', 'folder_ibfk_2', 'parent', 'folder', 'SET NULL', 'parent'),
    ('speaker_transcript_metrics', 'speaker_transcript_metrics_ibfk_1', 'speaker_id', 'speaker', 'SET NULL', 'speaker_id'),
]


def _insp():
    return sa.inspect(op.get_bind())


def _current_rule(table, name):
    for fk in _insp().get_foreign_keys(table):
        if fk['name'] == name:
            return (fk['options'].get('ondelete') or 'NO ACTION').upper()
    return None  # absent


def ensure_fk_index(table, column, name):
    indexes = {i['name']: list(i['column_names']) for i in _insp().get_indexes(table)}
    if name in indexes:
        return
    for other, cols in indexes.items():
        if cols == [column] and other.startswith(table + '_ibfk_'):
            op.execute('ALTER TABLE `{0}` RENAME INDEX `{1}` TO `{2}`'.format(table, other, name))
            return
    op.create_index(name, table, [column])


def _set_rule(table, name, column, referred, rule, index_name):
    current = _current_rule(table, name)
    if current == (rule or 'NO ACTION'):
        return
    if current is not None:
        op.drop_constraint(name, table, type_='foreignkey')
    op.create_foreign_key(name, table, referred, [column], ['id'], ondelete=rule)
    if index_name:
        ensure_fk_index(table, column, index_name)


def upgrade():
    for table, name, column, referred, rule, index_name in RULES:
        _set_rule(table, name, column, referred, rule, index_name)


def downgrade():
    for table, name, column, referred, rule, index_name in reversed(RULES):
        _set_rule(table, name, column, referred, None, index_name)
