from app import db
from datetime import datetime, timezone


# A single-use link emailed to someone: a password reset, or an invitation to
# finish setting up an account someone else created. Only the SHA-256 of the
# token is stored, so a database leak does not hand out working links.
class AccountToken(db.Model):
    __tablename__ = 'account_token'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    purpose = db.Column(db.String(16), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)

    RESET = 'reset'
    INVITE = 'invite'

    def __init__(self, user_id, purpose, token_hash, expires_at):
        self.user_id = user_id
        self.purpose = purpose
        self.token_hash = token_hash
        self.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.expires_at = expires_at
