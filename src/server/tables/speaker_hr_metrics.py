from app import db

# One row per heart-rate strap notification (Polar H10: ~1/second), streamed
# from the byod join page over Web Bluetooth. rr_ms holds the notification's
# beat-to-beat RR intervals as a comma-separated list of milliseconds — the
# raw material for HRV (RMSSD/SDNN); a notification can carry 0-3 of them.
class SpeakerHrMetrics(db.Model):
    __tablename__ = 'speaker_hr_metrics'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False)
    speaker_id = db.Column(db.Integer, nullable=True)
    speaker_alias = db.Column(db.String(64), nullable=False)
    sensor_name = db.Column(db.String(64))
    time_stamp = db.Column(db.Integer, nullable=False)
    heart_rate = db.Column(db.Integer, nullable=False)
    rr_ms = db.Column(db.String(64))

    # Declared by migration c4d5e6f7a8b9 but never in the model, so
    # autogenerate kept proposing to drop it.
    __table_args__ = (db.Index('ix_speaker_hr_metrics_device', 'session_device_id', 'time_stamp'),)

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_device_id, speaker_id, speaker_alias, sensor_name, time_stamp, heart_rate, rr_ms):
        self.session_device_id = session_device_id
        self.speaker_id = speaker_id
        self.speaker_alias = speaker_alias
        self.sensor_name = sensor_name
        self.time_stamp = time_stamp
        self.heart_rate = heart_rate
        self.rr_ms = rr_ms

    def json(self):
        return dict(
            id=self.id,
            session_device_id=self.session_device_id,
            speaker_id=self.speaker_id,
            speaker_alias=self.speaker_alias,
            sensor_name=self.sensor_name,
            time_stamp=self.time_stamp,
            heart_rate=self.heart_rate,
            rr_ms=self.rr_ms)
