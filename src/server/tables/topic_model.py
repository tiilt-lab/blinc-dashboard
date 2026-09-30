from app import db
from datetime import datetime, timezone
from utility import verify_characters

class TopicModel(db.Model):
    __tablename__ = 'topic_model'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    creation_date = db.Column(db.DateTime, nullable=False)
    name = db.Column(db.String(64))
    summary = db.Column(db.String(8000))

    NAME_MAX_LENGTH = 64
    NAME_CHARS = 'a-zA-Z0-9\': '

    def __hash__(self):
        return hash((self.id))

    def __init__(self, owner_id, name, summary):
        self.owner_id = owner_id
        self.creation_date = datetime.now(timezone.utc).replace(tzinfo=None)
        self.name = name
        self.summary = summary

    def get_name(self):
        if not self.name:
            return 'List {0}'.format(self.id)
        else:
            return self.name

    def json(self):
        return dict(
            id=self.id,
            creation_date=str(self.creation_date) + ' UTC',
            name=self.get_name(),
            summary=self.summary
        )

    @staticmethod
    def verify_fields(name=None):
        message = None
        if name is not None:
            if len(name) > TopicModel.NAME_MAX_LENGTH:
                message = 'Name must not exceed {0} characters.'.format(TopicModel.NAME_MAX_LENGTH)
            if not verify_characters(name, TopicModel.NAME_CHARS):
                message = 'Invalid characters in list name.'
        return message is None, message
