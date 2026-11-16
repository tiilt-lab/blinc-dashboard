from app import db

# Sync taps (sync_taps.py). One "Sync" press on the join page is a sync_id:
# a 'prompt' row when it was pressed, then a 'tap' row for each knock the
# sensor's accelerometer felt, all on the session clock in ms. The knocks are
# found again in the pod's audio when the sync report is read, so the
# sensor-vs-audio offset is measured, not stored, and improves if the
# detector does. source 'import' marks taps read from a device's own export
# (an Empatica E4's tags), timed on that device's clock rather than synced.
class SyncMarker(db.Model):
    __tablename__ = 'sync_marker'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False)
    speaker_id = db.Column(db.Integer, nullable=True)
    sync_id = db.Column(db.String(40), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    source = db.Column(db.String(16), nullable=False)
    sensor_kind = db.Column(db.String(16))
    sensor_name = db.Column(db.String(64))
    time_ms = db.Column(db.BigInteger, nullable=False)

    __table_args__ = (db.Index('ix_sync_marker_device', 'session_device_id', 'time_ms'),)

    KINDS = ('prompt', 'tap')
    SOURCES = ('live', 'import')

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_device_id, speaker_id, sync_id, kind, source, sensor_kind, sensor_name, time_ms):
        self.session_device_id = session_device_id
        self.speaker_id = speaker_id
        self.sync_id = sync_id
        self.kind = kind
        self.source = source
        self.sensor_kind = sensor_kind
        self.sensor_name = sensor_name
        self.time_ms = time_ms

    def json(self):
        return dict(
            id=self.id,
            session_device_id=self.session_device_id,
            speaker_id=self.speaker_id,
            sync_id=self.sync_id,
            kind=self.kind,
            source=self.source,
            sensor_kind=self.sensor_kind,
            sensor_name=self.sensor_name,
            time_ms=self.time_ms)
