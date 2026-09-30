from app import db

class Keyword(db.Model):
    __tablename__ = 'keyword'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id', ondelete='CASCADE'), nullable=False)
    keyword = db.Column(db.String(64))

    KEYWORD_MAX_LENGTH = 64

    def __hash__(self):
        return hash((self.id))

    def __init__(self, session_id, keyword):
        self.session_id = session_id
        self.keyword = keyword

    def json(self):
        return dict(
            id=self.id,
            session_id=self.session_id,
            keyword=self.keyword
        )

    @staticmethod
    def verify_fields(keyword=None):
        message = None
        if keyword is not None:
            if len(keyword) > Keyword.KEYWORD_MAX_LENGTH:
                message = 'Keyword must not exceed {0} characters.'.format(Keyword.KEYWORD_MAX_LENGTH)
        return message is None, message