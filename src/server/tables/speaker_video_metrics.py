from app import db

class SpeakerVideoMetrics(db.Model):
    __tablename__ = 'speaker_video_metrics'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False)
    student_username = db.Column(db.String(20), nullable=False, index=True)
    time_stamp = db.Column(db.Integer, nullable=False)
    facial_emotion = db.Column(db.String(64), nullable=False)
    attention_level = db.Column(db.Integer)
    object_on_focus = db.Column(db.String(128))

    # (session_device_id, time_stamp): the per-pod timeline reads
    # (migration 1f2e3d4c5b6a records the live index).
    __table_args__ = (db.Index('idx_svm_device_time', 'session_device_id', 'time_stamp'),)

    # keywords = db.relationship("KeywordUsage", lazy='joined', uselist=True)
    # metrics = db.relationship("SpeakerTranscriptMetrics", back_populates="transcript", cascade="all, delete",passive_deletes=True)

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_device_id, student_username, time_stamp, facial_emotion,attention_level,object_on_focus):
        self.session_device_id = session_device_id
        self.student_username = student_username
        self.time_stamp = time_stamp
        self.facial_emotion = facial_emotion
        self.attention_level = attention_level
        self.object_on_focus = object_on_focus


    def json(self):
        return dict(
            id=self.id,
            session_device_id=self.session_device_id,
            student_username=self.student_username,
            time_stamp=self.time_stamp,
            facial_emotion=self.facial_emotion,
            attention_level=self.attention_level,
            object_on_focus=self.object_on_focus
        )
