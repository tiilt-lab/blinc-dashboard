"""Short-lived tickets that authorise post-hoc websocket messages for ONE pod.

The post-hoc audio/video sockets used to act on a bare ``sessiondeviceid``:
anyone with the URL could wipe a pod's analysis (start -> post_posthoc_reset),
cancel someone else's run, or load 7-10 GB of models onto the shared GPU.

Now the API mints a ticket after its own session write check
(POST /api/v1/sessions/<sid>/devices/<did>/posthoc_ticket) and stores it as
``posthoc_ticket:<ticket> -> <session_device_id>`` with a 15-minute TTL. Every
socket message that names a pod must carry ``"ticket"``; the services look it
up and require the stored id to equal the requested one. Tickets are reusable
within their TTL (the trigger UI opens several sockets per page and re-asks
for status every 15 s).

The Redis client is passed in so this is unit-testable with a fake.
"""
import logging
import secrets

TICKET_PREFIX = 'posthoc_ticket:'
TICKET_TTL = 15 * 60
_MAX_TICKET_LEN = 128  # token_urlsafe(32) is 43 chars; bound attacker-chosen keys


def make_key(ticket):
    return TICKET_PREFIX + ticket


def mint(r, session_device_id, ttl=TICKET_TTL):
    """Store and return a fresh ticket for ``session_device_id``."""
    ticket = secrets.token_urlsafe(32)
    r.set(make_key(ticket), str(int(session_device_id)), ex=ttl)
    return ticket


def ticket_allows(r, ticket, session_device_id):
    """True iff ``ticket`` is a live ticket minted for ``session_device_id``."""
    if not isinstance(ticket, str) or not ticket or len(ticket) > _MAX_TICKET_LEN:
        return False
    try:
        stored = r.get(make_key(ticket))
    except Exception as e:
        # Redis down: deny. Failing open would re-expose the pods.
        logging.warning('posthoc ticket lookup failed: %s', e)
        return False
    if stored is None:
        return False
    if isinstance(stored, bytes):
        stored = stored.decode('utf-8', 'replace')
    try:
        return str(stored) == str(int(session_device_id))
    except (TypeError, ValueError):
        return False
