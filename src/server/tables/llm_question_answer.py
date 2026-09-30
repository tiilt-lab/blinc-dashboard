from app import db
from datetime import datetime, timezone

class LLMQuestionAnswer(db.Model):
    __tablename__ = 'llm_question_answer'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id', ondelete='CASCADE'), nullable=False)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id',ondelete="CASCADE"), nullable=False)
    speaker_username = db.Column(db.String(64),nullable=False)
    default_question_id = db.Column(db.Integer,nullable=False)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    creation_date = db.Column(db.DateTime, nullable=False) 

    session_device = db.relationship("SessionDevice", back_populates="llmquestionanswer")

   
    def __hash__(self):
        return hash(self.id)

    def __init__(self, session_id, session_device_id, speaker_username,default_question_id,question,answer):
        self.session_id = session_id
        self.session_device_id = session_device_id
        self.speaker_username = speaker_username
        self.default_question_id = default_question_id
        self.question = question
        self.answer = answer
        self.creation_date = datetime.now(timezone.utc).replace(tzinfo=None)

    def json(self):
        return dict(
            id=self.id,
            session_id=self.session_id,
            session_device_id=self.session_device_id,
            speaker_username=self.speaker_username,
            default_question_id = self.default_question_id,
            question = str(self.question),
            answer = self.answer,
            creation_date=str(self.creation_date) + ' UTC'
        )

