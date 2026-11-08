from app import db

# Moments of the session protocol pressed on a join-page laptop: joining,
# starting the recording, and the baseline that follows (eyes-open and
# eyes-closed rest, the group fruit game, the start of the discussion). One
# row per press or phase boundary, on the session clock in ms like every
# wearable stream (physio_time.py), so heart-rate, EEG and imported device
# files line up with them by time. sensors lists the wearables that laptop
# had connected at that moment (e.g. "polar:Polar H10 A1B2C3D4 (Ana)").
# marker_id is the page's own id for the press: a batch re-sent after a lost
# reply is stored once.
class EventMarker(db.Model):
    __tablename__ = 'event_marker'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False)
    marker_id = db.Column(db.String(40), nullable=False)
    label = db.Column(db.String(32), nullable=False)
    time_stamp = db.Column(db.Integer, nullable=False)
    time_ms = db.Column(db.BigInteger, nullable=False)
    sensors = db.Column(db.String(255))
    detail = db.Column(db.String(255))

    __table_args__ = (db.Index('ix_event_marker_device', 'session_device_id', 'time_ms'),
                      db.UniqueConstraint('session_device_id', 'marker_id', name='uq_event_marker_device_marker'))

    # In protocol order; the join page sends only these.
    LABELS = (
        'session_joined',
        'recording_started',
        'baseline_started',
        'eyes_open_start',
        'eyes_open_end',
        'eyes_closed_start',
        'eyes_closed_end',
        'game_start',
        'game_end',
        'discussion_start',
        'baseline_cancelled',
    )

    # The markers CSV (routes/event_markers.py, and the session data export).
    CSV_COLUMNS = ('pod_id', 'pod_name', 'label', 'time_s', 'time_ms', 'sensors', 'detail')

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_device_id, marker_id, label, time_stamp, time_ms, sensors=None, detail=None):
        self.session_device_id = session_device_id
        self.marker_id = marker_id
        self.label = label
        self.time_stamp = time_stamp
        self.time_ms = time_ms
        self.sensors = sensors
        self.detail = detail

    def json(self):
        return dict(
            id=self.id,
            session_device_id=self.session_device_id,
            label=self.label,
            time_stamp=self.time_stamp,
            time_ms=self.time_ms,
            sensors=self.sensors,
            detail=self.detail)

    def csv_row(self, pod):
        return [self.session_device_id, pod.name if pod is not None else '', self.label,
                round(self.time_ms / 1000.0, 3), self.time_ms, self.sensors or '', self.detail or '']
