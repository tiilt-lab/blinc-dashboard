"""Outgoing email, sent through MailerSend's HTTP API.

Configured from the environment — on the production host that is
/etc/blinc/secrets.env, which the systemd unit loads:

    MAILERSEND_API_TOKEN   API token with "Email: full access"
    MAIL_FROM_EMAIL        sender on a domain verified in MailerSend,
                           e.g. no-reply@nublinc.com
    MAIL_FROM_NAME         display name (default "BLINC")
    MAIL_REPLY_TO          optional reply-to address, e.g. tiiltlab@gmail.com
    MAIL_LINK_BASE         optional site URL for links in emails; defaults to
                           the first domain in config.ini
    MAILERSEND_API_URL     optional endpoint override, for tests against a
                           local stand-in

Without a token nothing is sent: the message is written to the log instead,
so development instances work and nobody is emailed by accident. Sends run
on a background thread so a slow mail API never holds up a request.
"""
import logging
import os
import threading

import requests

import config as cf

API_URL = 'https://api.mailersend.com/v1/email'
TIMEOUT_SECONDS = 15


def configured():
    return bool(os.environ.get('MAILERSEND_API_TOKEN') and os.environ.get('MAIL_FROM_EMAIL'))


def link(path):
    base = os.environ.get('MAIL_LINK_BASE') or cf.domain()
    return base.rstrip('/') + '/' + path.lstrip('/')


def build_payload(to, subject, text, html):
    payload = {
        'from': {'email': os.environ.get('MAIL_FROM_EMAIL'),
                 'name': os.environ.get('MAIL_FROM_NAME', 'BLINC')},
        'to': [{'email': to}],
        'subject': subject,
        'text': text,
        'html': html,
    }
    reply_to = os.environ.get('MAIL_REPLY_TO')
    if reply_to:
        payload['reply_to'] = {'email': reply_to}
    return payload


def send(to, subject, text, html, background=True):
    """Queue one email. Returns False when mail is not configured (the message
    is logged instead), True once the send has been handed off."""
    if not configured():
        logging.warning('Email NOT sent (MailerSend not configured) to=%s subject=%r\n%s',
                        to, subject, text)
        return False
    payload = build_payload(to, subject, text, html)
    if background:
        threading.Thread(target=_post, args=(payload,), daemon=True, name='mailer').start()
        return True
    return _post(payload)


def _post(payload):
    to = payload['to'][0]['email']
    try:
        response = requests.post(
            os.environ.get('MAILERSEND_API_URL') or API_URL, json=payload, timeout=TIMEOUT_SECONDS,
            headers={'Authorization': 'Bearer ' + os.environ['MAILERSEND_API_TOKEN']})
    except requests.RequestException as e:
        logging.error('Email to %s failed: %s', to, e)
        return False
    if response.status_code >= 300:
        # MailerSend explains rejections (unverified domain, trial limits,
        # bad token) in the body; the token itself is never logged.
        logging.error('Email to %s rejected by MailerSend (%s): %s',
                      to, response.status_code, response.text[:500])
        return False
    logging.info('Email to %s accepted by MailerSend (message id %s).',
                 to, response.headers.get('X-Message-Id'))
    return True
