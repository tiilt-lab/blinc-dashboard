from app import db

# One row per Muse electrode per second (four rows a second per wearer),
# streamed from the byod join page over Web Bluetooth (muse-eeg.js). Raw EEG
# runs at 256 Hz, too much for a row per sample, so the browser reduces each
# 1-second epoch to log10 band power (µV²) per band, plus the epoch's
# standard deviation, which reads as electrode contact (std_uv: a flat or
# wildly swinging channel is off the skin). time_ms is the epoch's first
# sample on the session clock, rebuilt from the headband's packet counter
# (sample-clock.js); time_stamp is the same in whole seconds.
class SpeakerEegMetrics(db.Model):
    __tablename__ = 'speaker_eeg_metrics'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False)
    speaker_id = db.Column(db.Integer, nullable=True)
    speaker_alias = db.Column(db.String(64), nullable=False)
    sensor_name = db.Column(db.String(64))
    time_stamp = db.Column(db.Integer, nullable=False)
    time_ms = db.Column(db.BigInteger, nullable=False)
    channel = db.Column(db.String(8), nullable=False)
    delta = db.Column(db.Float)
    theta = db.Column(db.Float)
    alpha = db.Column(db.Float)
    beta = db.Column(db.Float)
    gamma = db.Column(db.Float)
    std_uv = db.Column(db.Float)

    __table_args__ = (db.Index('ix_speaker_eeg_metrics_device', 'session_device_id', 'time_stamp'),)

    BANDS = ('delta', 'theta', 'alpha', 'beta', 'gamma')

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_device_id, speaker_id, speaker_alias, sensor_name, time_stamp, time_ms,
                 channel, bands, std_uv):
        self.session_device_id = session_device_id
        self.speaker_id = speaker_id
        self.speaker_alias = speaker_alias
        self.sensor_name = sensor_name
        self.time_stamp = time_stamp
        self.time_ms = time_ms
        self.channel = channel
        for name in self.BANDS:
            setattr(self, name, bands.get(name))
        self.std_uv = std_uv

    def json(self):
        return dict(
            id=self.id,
            session_device_id=self.session_device_id,
            speaker_id=self.speaker_id,
            speaker_alias=self.speaker_alias,
            sensor_name=self.sensor_name,
            time_stamp=self.time_stamp,
            time_ms=self.time_ms,
            channel=self.channel,
            delta=self.delta,
            theta=self.theta,
            alpha=self.alpha,
            beta=self.beta,
            gamma=self.gamma,
            std_uv=self.std_uv)
