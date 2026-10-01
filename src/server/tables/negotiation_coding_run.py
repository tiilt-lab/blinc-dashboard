from app import db
from datetime import datetime, timezone
import json


def _utc(dt):
    return str(dt) + ' UTC' if dt else None


class NegotiationCodingRun(db.Model):
    """One negotiation-coding pass over a pod's transcript (negotiation_coding.py).
    Created queued by the API or end_session, moved through running -> done |
    error by the coordinator's coding leg; its codes are negotiation_code rows.
    Mapped columns: migration 4c5d6e7f8091 must be applied before this code
    is deployed."""
    __tablename__ = 'negotiation_coding_run'
    STATUSES = ('queued', 'running', 'done', 'error')

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_device_id = db.Column(db.Integer, db.ForeignKey('session_device.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(db.String(16), nullable=False)
    model = db.Column(db.String(128))
    codebook_version = db.Column(db.String(32))
    teams = db.Column(db.Text)      # JSON {speaker_tag: "Pat" | "Sandy"}
    summary = db.Column(db.Text)    # JSON, negotiation_coding.summarize()
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    utterances_coded = db.Column(db.Integer)
    invalid_codes = db.Column(db.Integer)

    def __hash__(self):
        return hash(self.id)

    def __init__(self, session_device_id, model, codebook_version, teams=None):
        self.session_device_id = session_device_id
        self.status = 'queued'
        self.model = model
        self.codebook_version = codebook_version
        self.teams = json.dumps(teams or {})
        self.created_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def _json_field(self, raw, default):
        # Bad or empty JSON never breaks the run's json().
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def teams_dict(self):
        teams = self._json_field(self.teams, {})
        return teams if isinstance(teams, dict) else {}

    def summary_dict(self):
        return self._json_field(self.summary, None)

    def json(self):
        return dict(
            id=self.id,
            session_device_id=self.session_device_id,
            status=self.status,
            model=self.model,
            codebook_version=self.codebook_version,
            created_at=_utc(self.created_at),
            started_at=_utc(self.started_at),
            finished_at=_utc(self.finished_at),
            error=self.error,
            utterances_coded=self.utterances_coded,
            invalid_codes=self.invalid_codes,
        )
