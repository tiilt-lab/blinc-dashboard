from app import db
from datetime import datetime, timezone


# One person's access to one folder (and, through inheritance, everything
# beneath it). The folder's owner is implicitly its manager and has no row
# here. Levels and their meaning live in folder_access.py.
class FolderMember(db.Model):
    __tablename__ = 'folder_member'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    folder_id = db.Column(db.Integer, db.ForeignKey('folder.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    level = db.Column(db.String(16), nullable=False)
    granted_by = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'), nullable=True)
    granted_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (db.UniqueConstraint('folder_id', 'user_id', name='uq_folder_member_folder_user'),)

    def __init__(self, folder_id, user_id, level, granted_by=None):
        self.folder_id = folder_id
        self.user_id = user_id
        self.level = level
        self.granted_by = granted_by
        self.granted_at = datetime.now(timezone.utc).replace(tzinfo=None)
