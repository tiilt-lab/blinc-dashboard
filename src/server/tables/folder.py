from app import db
from datetime import datetime, timezone
from utility import verify_characters


class Folder(db.Model):
    __tablename__ = 'folder'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(64))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    parent = db.Column(db.Integer, db.ForeignKey('folder.id', ondelete='SET NULL'), nullable=True)
    creation_date = db.Column(db.DateTime, nullable=True)

    NAME_MAX_LENGTH = 64
    NAME_CHARS = 'a-zA-Z0-9\': '

    def __hash__(self):
        return hash((self.id))

    def __init__(self, owner_id, name= None, parent=None):
        self.owner_id = owner_id
        self.name = name
        self.parent = parent
        self.creation_date = datetime.now(timezone.utc).replace(tzinfo=None)

    def json(self):
        return dict(
            id=self.id,
            name=self.name,
            parent= self.parent,
            creation_date=str(self.creation_date) + ' UTC',
        )

    @staticmethod
    def verify_fields(name=None):
        message = None
        if name is not None:
            if len(name) > Folder.NAME_MAX_LENGTH:
                message = 'Name must not exceed {0} characters.'.format(Folder.NAME_MAX_LENGTH)
            if not verify_characters(name, Folder.NAME_CHARS):
                message = 'Invalid characters in name.'
        return message is None, message
