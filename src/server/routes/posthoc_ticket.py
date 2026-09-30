"""Mint a short-lived ticket that lets the caller drive the post-hoc websockets
for ONE pod (common/posthoc_ticket). Gated exactly like a re-run: session write
access, and the pod must belong to that session.
"""
from flask import Blueprint, session as flask_session
from app import limiter
from utility import json_response
import authz
import wrappers
from redis_helper import RedisPosthocTicket

api_routes = Blueprint('posthoc_ticket', __name__)


def _caller_key():
    # Per signed-in account (as folder sharing does), not per IP.
    return str((flask_session.get('user') or {}).get('id', 'anon'))


@api_routes.route('/api/v1/sessions/<int:session_id>/devices/<int:session_device_id>/posthoc_ticket', methods=['POST'])
@wrappers.verify_login(public=True)
@limiter.limit("60 per hour", key_func=_caller_key)
@wrappers.verify_session_access
def mint_posthoc_ticket(session_id, session_device_id, **kwargs):
    if authz.device_in_session(session_device_id, session_id) is None:
        # Denied and missing look the same: ids are sequential.
        return json_response({'message': 'Does not exist.'}, 404)
    return json_response({'ticket': RedisPosthocTicket.mint(session_device_id),
                          'ttl': RedisPosthocTicket.TTL})
