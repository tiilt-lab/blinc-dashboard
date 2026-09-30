from app import db

class SpeakerTranscriptMetrics(db.Model):
    __tablename__ = 'speaker_transcript_metrics'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    speaker_id = db.Column(db.Integer, db.ForeignKey('speaker.id', ondelete='SET NULL'), nullable=True)
    transcript_id = db.Column(db.Integer, db.ForeignKey('transcript.id', ondelete="CASCADE"), nullable=False)
    participation_score = db.Column(db.Float)
    internal_cohesion = db.Column(db.Float)
    responsivity =  db.Column(db.Float)
    social_impact = db.Column(db.Float)
    newness =  db.Column(db.Float)
    communication_density = db.Column(db.Float)
    
    transcript = db.relationship("Transcript", back_populates="metrics")


    def __hash__(self):
      return hash((self.id))

    def __init__(self, speaker_id, transcript_id, participation_score, internal_cohesion, responsivity, social_impact, newness, communication_density):
       self.speaker_id = speaker_id
       self.transcript_id = transcript_id
       self.participation_score = participation_score
       self.internal_cohesion = internal_cohesion
       self.responsivity = responsivity
       self.social_impact = social_impact
       self.newness = newness
       self.communication_density = communication_density

    def json(self):
       return(dict(
          id=self.id,
          speaker_id=self.speaker_id,
          transcript_id=self.transcript_id,
          participation_score=self.participation_score,
          internal_cohesion=self.internal_cohesion,
          responsivity=self.responsivity,
          social_impact=self.social_impact,
          newness=self.newness,
          communication_density=self.communication_density
       ))
