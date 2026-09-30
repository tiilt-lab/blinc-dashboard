from app import db

class KeywordUsage(db.Model):
    __tablename__ = 'keyword_usage'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    transcript_id = db.Column(db.Integer, db.ForeignKey('transcript.id', ondelete='CASCADE'), nullable=False)
    word = db.Column(db.String(64), nullable=False)
    keyword = db.Column(db.String(64), nullable=False)
    similarity = db.Column(db.Float, nullable=False)

    WORD_MAX_LENGTH = 64
    KEYWORD_MAX_LENGTH = 64

    def __hash__(self):
        return hash((self.id))

    def __init__(self, transcript_id, word, keyword, similarity):
        self.transcript_id = transcript_id
        self.word = word
        self.keyword = keyword
        self.similarity = similarity

    def json(self, suppress=()):
        json_data = dict(
            id=self.id,
            word=self.word,
            keyword=self.keyword,
            similarity=self.similarity
        )
        if not 'transcript_id' in suppress:
            json_data['transcript_id']=self.transcript_id
        return json_data

    @staticmethod
    def verify_fields(word=None, keyword=None):
        message = None
        if word is not None:
            if len(word) > KeywordUsage.WORD_MAX_LENGTH:
                message = 'Word must not exceed {0} characters.'.format(KeywordUsage.WORD_MAX_LENGTH)
        if keyword is not None:
            if len(keyword) > KeywordUsage.KEYWORD_MAX_LENGTH:
                message = 'Keyword must not exceed {0} characters.'.format(KeywordUsage.KEYWORD_MAX_LENGTH)
        return message is None, message
