from app import db

class Transcript(db.Model):
    __tablename__ = 'transcript'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False)
    start_time = db.Column(db.Integer, nullable=False)
    length = db.Column(db.Integer, nullable=False)
    question = db.Column(db.Boolean, nullable=False)
    transcript = db.Column(db.Text, nullable=False)
    word_count = db.Column(db.Integer, nullable=False)
    direction = db.Column(db.Integer)
    emotional_tone_value = db.Column(db.Integer)
    analytic_thinking_value = db.Column(db.Integer)
    clout_value = db.Column(db.Integer)
    authenticity_value = db.Column(db.Integer)
    certainty_value = db.Column(db.Integer)
    topic_id = db.Column(db.Integer)
    speaker_tag = db.Column(db.String(64), index=True)
    # get_transcripts(speaker_id=) scanned the table (migration f8ae4e72c79c).
    speaker_id = db.Column(db.Integer, index=True)
    # JSON blob of per-utterance voice features (prosody #6 + vocal emotion #5),
    # nullable. Requires migration; older rows read as None.
    voice_features = db.Column(db.Text, nullable=True)

    keywords = db.relationship("KeywordUsage", lazy='joined', uselist=True, cascade="all, delete", passive_deletes=True)
    metrics = db.relationship("SpeakerTranscriptMetrics", back_populates="transcript", cascade="all, delete",passive_deletes=True)

    # (session_device_id, start_time): every per-pod transcript window read
    # (migration 1f2e3d4c5b6a records the live index).
    __table_args__ = (db.Index('idx_transcript_device_start', 'session_device_id', 'start_time'),)

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_device_id, start_time, length, transcript, question, direction, emotional_tone, analytic_thinking, clout, authenticity, certainty, topic_id, speaker_tag, speaker_id, voice_features=None):
        self.session_device_id = session_device_id
        self.start_time = start_time
        self.length = length
        self.question = question
        self.transcript = transcript
        self.word_count = len(transcript.split())
        self.direction = direction
        self.emotional_tone_value = emotional_tone
        self.analytic_thinking_value = analytic_thinking
        self.clout_value = clout
        self.authenticity_value = authenticity
        self.certainty_value = certainty
        self.topic_id = topic_id
        self.speaker_tag = speaker_tag
        self.speaker_id = speaker_id
        import json as _json
        self.voice_features = _json.dumps(voice_features) if voice_features else None


    def _voice_features_json(self):
        import json as _json
        raw = getattr(self, 'voice_features', None)
        if not raw:
            return None
        try:
            return _json.loads(raw)
        except Exception:
            return None

    def json(self):
        return dict(
            id=self.id,
            session_device_id=self.session_device_id,
            start_time=self.start_time,
            length=self.length,
            question=self.question,
            transcript=self.transcript,
            direction=self.direction,
            emotional_tone_value=self.emotional_tone_value,
            analytic_thinking_value=self.analytic_thinking_value,
            clout_value=self.clout_value,
            authenticity_value=self.authenticity_value,
            certainty_value=self.certainty_value,
            word_count=self.word_count,
            topic_id=self.topic_id,
            speaker_tag=self.speaker_tag,
            speaker_id=self.speaker_id,
            voice_features=self._voice_features_json(),
            keywords=[keyword.json(suppress=['transcript_id']) for keyword in self.keywords]
        )

    @staticmethod
    def verify_fields(name=None):
        message = None
        # Not implemented
        return message is None, message
