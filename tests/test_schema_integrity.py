"""Schema referential-integrity contracts (architecture fix #5).

Integrity was enforced by application code that every delete path had to
remember (and forgot — orphan rater/rating/survey rows, and the delete_user FK
crash). The database should enforce it. These are source-level contracts (the
models can't be imported under CI, which has no Flask): they assert the FK +
ON DELETE CASCADE declarations are present so a careless edit can't quietly
drop them. The actual cascade is verified against the live DB at migration time.
"""
import os

TABLES = os.path.join(os.path.dirname(__file__), "..", "src", "server", "tables")


def _read(name):
    with open(os.path.join(TABLES, name)) as f:
        return f.read()


def test_rating_rater_survey_fk_session_with_cascade():
    for fn in ("rating.py", "rater.py", "survey_response.py"):
        s = _read(fn)
        assert "ForeignKey('session.id'" in s or 'ForeignKey("session.id"' in s, \
            f"{fn}: sessionid must be a FK to session.id"
        assert "ondelete='CASCADE'" in s or 'ondelete="CASCADE"' in s, \
            f"{fn}: sessionid FK must be ON DELETE CASCADE"


def test_metrics_fk_device_with_cascade():
    for fn in ("speaker_video_metrics.py", "speaker_hr_metrics.py",
               "speaker_eeg_metrics.py", "sync_marker.py", "wearable_raw_block.py"):
        s = _read(fn)
        assert "ForeignKey('session_device.id'" in s or 'ForeignKey("session_device.id"' in s, \
            f"{fn}: session_device_id must be a FK to session_device.id"
        assert "ondelete='CASCADE'" in s or 'ondelete="CASCADE"' in s, \
            f"{fn}: session_device_id FK must be ON DELETE CASCADE"


def test_integrity_migration_exists():
    versions = os.path.join(os.path.dirname(__file__), "..", "src", "server",
                            "migrations", "versions")
    found = False
    for fn in os.listdir(versions):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(versions, fn)) as f:
            text = f.read()
        if "create_foreign_key" in text and "ondelete" in text and "CASCADE" in text:
            found = True
            break
    assert found, "no migration adds the ON DELETE CASCADE foreign keys"
