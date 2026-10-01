from app import db
from datetime import datetime, timezone
from utility import verify_characters

class Session(db.Model):
    __tablename__ = 'session'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(64))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    creation_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime)
    passcode = db.Column(db.String(64))
    # SET NULL: deleting a folder or topic model lifts the session out of it;
    # owner_id keeps no rule on purpose, sessions are deleted explicitly.
    folder = db.Column(db.Integer, db.ForeignKey('folder.id', ondelete='SET NULL'), nullable=True)
    topic_model_id = db.Column(db.Integer, db.ForeignKey('topic_model.id', ondelete='SET NULL'), nullable=True)
    # Live video analytics during class (per-pod ffmpeg decode + the GPU
    # detectors, ~3 cores a pod). NULL/TRUE = analyse live, the historic
    # behaviour; FALSE = "record now, analyse later": the video service only
    # records, and end_session queues a post-hoc VIDEO leg per recorded pod.
    # Mapped column: migration 3b4c5d6e7f80 must be applied before this code
    # is deployed (a missing column raises on every Session query).
    live_video_analytics = db.Column(db.Boolean, nullable=True)
    # Negotiation coding (Kellogg negotiation class): TRUE = end_session queues
    # an LLM coding run per pod (negotiation_coding.py). NULL/FALSE = off, the
    # historic behaviour. Mapped column: migration 4c5d6e7f8091 first.
    negotiation_coding = db.Column(db.Boolean, nullable=True)

    keywords = db.relationship("Keyword", lazy='joined', uselist=True, cascade="all, delete", passive_deletes=True)

    # passcode: the anonymous join lookup; (owner_id, creation_date): the
    # per-owner sessions list order (migration f8ae4e72c79c).
    __table_args__ = (db.Index('ix_session_passcode', 'passcode'),
                      db.Index('ix_session_owner_created', 'owner_id', 'creation_date'))

    NAME_MAX_LENGTH = 64
    NAME_CHARS = 'a-zA-Z0-9\': '
    PASSCODE_MAX_LENGTH = 64

    def __hash__(self):
        return hash((self.id))

    def __init__(self, owner_id, name="Unnamed", folder=None, topic_model=None, live_video_analytics=True, negotiation_coding=False):
        self.owner_id = owner_id
        self.name = name
        self.creation_date = datetime.now(timezone.utc).replace(tzinfo=None)
        self.folder = folder
        self.topic_model_id = topic_model
        self.live_video_analytics = live_video_analytics is not False
        self.negotiation_coding = bool(negotiation_coding)


    def get_length(self):
        return max((self.end_date - self.creation_date).total_seconds() if self.end_date else (datetime.now(timezone.utc).replace(tzinfo=None) - self.creation_date).total_seconds(), 0.0)

    def json(self):
        return dict(
            id=self.id,
            name=self.name,
            passcode=self.passcode,
            creation_date=str(self.creation_date) + ' UTC',
            end_date=str(self.end_date) + ' UTC' if self.end_date else None,
            length=self.get_length(), # length is not stored in database as it can be derived
            keywords=[keyword.keyword for keyword in self.keywords],
            folder=self.folder,
            topic_model_id=self.topic_model_id,
            # NULL (rows from before the column) reads as the historic "live".
            live_video_analytics=self.live_video_analytics is not False,
            # NULL (rows from before the column) reads as off.
            negotiation_coding=bool(self.negotiation_coding)
        )

    @staticmethod
    def verify_fields(name=None):
        message = None
        if name is not None:
            if len(name) > Session.NAME_MAX_LENGTH:
                message = 'Name must not exceed {0} characters.'.format(Session.NAME_MAX_LENGTH)
            if not verify_characters(name, Session.NAME_CHARS):
                message = 'Invalid characters in name.'
        return message is None, message
