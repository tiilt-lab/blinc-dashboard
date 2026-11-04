from app import db

# Raw wearable signal at its native rate, one row per stream per second per
# wearer (raw_signals.py has the layout): a Muse's 256 Hz EEG (TP9, AF7,
# AF8, TP10) and 52 Hz accelerometer (x, y, z). data holds the device's
# integer counts, interleaved by sample, int16 little-endian; scale converts
# a count to unit. time_ms is the block's first sample on the session clock
# (sample-clock.js), sample_index that sample's count since the device
# connected (it restarts on a reconnect). Separate from the per-second
# metrics tables, which the dashboard reads; this one is for export.
class WearableRawBlock(db.Model):
    __tablename__ = 'wearable_raw_block'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False)
    speaker_id = db.Column(db.Integer, nullable=True)
    speaker_alias = db.Column(db.String(64), nullable=False)
    sensor_kind = db.Column(db.String(16), nullable=False)
    sensor_name = db.Column(db.String(64))
    stream = db.Column(db.String(8), nullable=False)
    channels = db.Column(db.String(64), nullable=False)
    rate = db.Column(db.Float, nullable=False)
    scale = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(8), nullable=False)
    sample_index = db.Column(db.BigInteger, nullable=False)
    time_ms = db.Column(db.BigInteger, nullable=False)
    n_samples = db.Column(db.Integer, nullable=False)
    n_missing = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)

    __table_args__ = (db.Index('ix_wearable_raw_block_device', 'session_device_id', 'stream', 'time_ms'),)

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_device_id, speaker_id, speaker_alias, sensor_kind, sensor_name, stream,
                 channels, rate, scale, unit, sample_index, time_ms, n_samples, n_missing, data):
        self.session_device_id = session_device_id
        self.speaker_id = speaker_id
        self.speaker_alias = speaker_alias
        self.sensor_kind = sensor_kind
        self.sensor_name = sensor_name
        self.stream = stream
        self.channels = channels
        self.rate = rate
        self.scale = scale
        self.unit = unit
        self.sample_index = sample_index
        self.time_ms = time_ms
        self.n_samples = n_samples
        self.n_missing = n_missing
        self.data = data
