"""Short-lived tokens that authorise biometric enrollment for ONE alias.

The live audio socket (server.py) and the post-hoc video socket
(server_posthoc.py) used to accept ``save-audio-video-fingerprinting`` plus
media for any alias with no credential: anyone could replace or delete a
student's voice/face print (a failed quality gate deletes the existing
.wav/.emb.npy) or burn CPU/GPU decoding media.

Now POST /api/v1/student/addstudent mints a token (on create, and on the
name-matched re-enrol path) and stores it as
``enroll_token:<token> -> <username>`` with a 30-minute TTL. The enrollment
message on both sockets must carry ``"token"``; the services look it up and
require the stored username to equal the requested alias before storing any
state, so before any binary media is accepted. Tokens are reusable within
their TTL (the sign-up page sends the same one on two sockets) but bound to
the alias.

The Redis client is passed in so this is unit-testable with a fake.
"""
import logging
import secrets

TOKEN_PREFIX = 'enroll_token:'
TOKEN_TTL = 30 * 60
_MAX_TOKEN_LEN = 128  # token_urlsafe(32) is 43 chars; bound attacker-chosen keys


def make_key(token):
    return TOKEN_PREFIX + token


def mint(r, username, ttl=TOKEN_TTL):
    """Store and return a fresh token for ``username``."""
    token = secrets.token_urlsafe(32)
    r.set(make_key(token), str(username), ex=ttl)
    return token


def enrollment_allows(r, token, alias):
    """True iff ``token`` is a live token minted for exactly ``alias``."""
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LEN:
        return False
    if not isinstance(alias, str) or not alias:
        return False
    try:
        stored = r.get(make_key(token))
    except Exception as e:
        # Redis down: deny. Failing open would re-expose every voice print.
        logging.warning('enrollment token lookup failed: %s', e)
        return False
    if stored is None:
        return False
    if isinstance(stored, bytes):
        stored = stored.decode('utf-8', 'replace')
    return stored == alias
