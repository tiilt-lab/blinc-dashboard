from app import db
from datetime import datetime, timezone

class LLMFeedbackReport(db.Model):
    __tablename__ = 'llm_feedback_report'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id', ondelete='CASCADE'), nullable=False)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id',ondelete="CASCADE"), nullable=False)
    speaker_username = db.Column(db.String(64),nullable=False)
    feedback_analysis = db.Column(db.Text, nullable=False)
    creation_date = db.Column(db.DateTime, nullable=False) 

    session_device = db.relationship("SessionDevice", back_populates="llmfeedbackreports")

   
    def __hash__(self):
        return hash(self.id)

    def __init__(self, session_id, session_device_id, speaker_username,feedback_analysis):
        self.session_id = session_id
        self.session_device_id = session_device_id
        self.speaker_username = speaker_username
        self.feedback_analysis = feedback_analysis
        self.creation_date = datetime.now(timezone.utc).replace(tzinfo=None)

    def json(self):
        return dict(
            id=self.id,
            session_id=self.session_id,
            session_device_id=self.session_device_id,
            speaker_username=self.speaker_username,
            feedback_analysis=self.feedback_analysis,
            creation_date=str(self.creation_date) + ' UTC'
        )

