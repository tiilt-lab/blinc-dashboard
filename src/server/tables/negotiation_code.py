from app import db
import json


class NegotiationCode(db.Model):
    """The four codes of one utterance in one negotiation-coding run. Goes
    with its run (re-runs replace the set) and with its transcript row (a
    post-hoc audio re-run that rewrites the transcript drops the codes)."""
    __tablename__ = 'negotiation_code'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(db.Integer, db.ForeignKey('negotiation_coding_run.id', ondelete='CASCADE'), nullable=False, index=True)
    transcript_id = db.Column(db.Integer, db.ForeignKey('transcript.id', ondelete='CASCADE'), nullable=False, index=True)
    emotion = db.Column(db.String(16))
    rip = db.Column(db.String(16))
    frame = db.Column(db.String(32))
    listening = db.Column(db.Text)  # JSON list of listening codes

    def __hash__(self):
        return hash(self.id)

    def __init__(self, run_id, transcript_id, emotion, rip, frame, listening=None):
        self.run_id = run_id
        self.transcript_id = transcript_id
        self.emotion = emotion
        self.rip = rip
        self.frame = frame
        self.listening = json.dumps(list(listening or []))

    def listening_list(self):
        try:
            value = json.loads(self.listening) if self.listening else []
        except Exception:
            value = []
        return value if isinstance(value, list) else []

    def json(self):
        return dict(
            id=self.id,
            run_id=self.run_id,
            transcript_id=self.transcript_id,
            emotion=self.emotion,
            rip=self.rip,
            frame=self.frame,
            listening=self.listening_list(),
        )
