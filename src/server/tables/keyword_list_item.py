from app import db
from utility import verify_characters

class KeywordListItem(db.Model):
    __tablename__ = 'keyword_list_item'
    keyword_list_id = db.Column(db.Integer, db.ForeignKey('keyword_list.id', ondelete='CASCADE'), primary_key=True, nullable=False)
    keyword = db.Column(db.String(64), primary_key=True, nullable=False)

    KEYWORD_MAX_LENGTH = 64
    KEYWORD_CHARS = 'a-zA-Z0-9\''

    def __hash__(self):
        return hash((self.keyword_list_id, self.keyword))

    def __init__(self, keyword_list_id, keyword):
        self.keyword_list_id = keyword_list_id
        self.keyword = keyword

    def json(self):
        return dict(
            keyword_list_id=self.keyword_list_id,
            keyword=self.keyword
        )

    @staticmethod
    def verify_fields(keyword=None):
        message = None
        if keyword is not None:
            if len(keyword) > KeywordListItem.KEYWORD_MAX_LENGTH:
                message = 'Keyword must not exceed {0} characters.'.format(KeywordListItem.KEYWORD_MAX_LENGTH)
            if not verify_characters(keyword, KeywordListItem.KEYWORD_CHARS):
                message = 'Invalid characters in keyword.'
        return message is None, message
