"""The emails BLINC sends. Each builds its text and HTML bodies and hands
them to mailer.send; links point at the frontend pages that handle them."""
from html import escape

import mailer

_LEVEL_WORDS = {
    'viewer': 'view',
    'editor': 'view and edit',
    'manager': 'view, edit and manage',
}


def _html(heading, paragraphs, button_text, url, footer):
    body = ''.join('<p style="margin:0 0 14px">{0}</p>'.format(p) for p in paragraphs)
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:520px;margin:0 auto;padding:24px;color:#1f1a2e;font-size:15px;line-height:1.5">'
        '<div style="font-weight:700;font-size:18px;color:#3b2263;margin-bottom:18px">BLINC</div>'
        '<h1 style="font-size:20px;margin:0 0 14px">{heading}</h1>{body}'
        '<p style="margin:22px 0"><a href="{url}" style="background:#3b2263;color:#ffffff;'
        'padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block">'
        '{button}</a></p>'
        '<p style="margin:0 0 14px;font-size:13px;color:#6b6480">Or open this link: '
        '<a href="{url}" style="color:#3b2263">{url}</a></p>'
        '<p style="margin:22px 0 0;font-size:12px;color:#8a849c">{footer}</p></div>'
    ).format(heading=escape(heading), body=body, url=escape(url, quote=True),
             button=escape(button_text), footer=footer)


def folder_shared(to, sharer_email, folder_name, folder_id, level, changed=False):
    url = mailer.link('sessions?folder={0}'.format(folder_id))
    verb = 'changed your access to' if changed else 'shared'
    if changed:
        subject = 'Your access to "{0}" changed'.format(folder_name)
    else:
        subject = '{0} shared "{1}" with you'.format(sharer_email, folder_name)
    can = _LEVEL_WORDS.get(level, level)
    text = ('{sharer} {verb} the BLINC folder "{folder}"{to_you}. You can now {can} '
            'the sessions in it, including every folder inside it.\n\nOpen it: {url}\n').format(
        sharer=sharer_email, verb=verb, folder=folder_name, to_you='' if changed else ' with you',
        can=can, url=url)
    html = _html(
        'A folder was shared with you' if not changed else 'Your folder access changed',
        ['<b>{0}</b> {1} the folder <b>{2}</b>{3}.'.format(
            escape(sharer_email), verb, escape(folder_name), '' if changed else ' with you'),
         'You can now {0} the sessions in it, including every folder inside it.'.format(escape(can))],
        'Open folder', url,
        'You received this because someone gave your BLINC account access to a folder.')
    return mailer.send(to, subject, text, html)


def invite(to, inviter_email, token, folder_name=None, level=None):
    url = mailer.link('reset-password?token={0}&invite=1'.format(token))
    if folder_name:
        subject = '{0} invited you to "{1}" on BLINC'.format(inviter_email, folder_name)
        lead = '{0} invited you to the BLINC folder "{1}" (you can {2} it).'.format(
            inviter_email, folder_name, _LEVEL_WORDS.get(level, level))
        lead_html = '<b>{0}</b> invited you to the BLINC folder <b>{1}</b> (you can {2} it).'.format(
            escape(inviter_email), escape(folder_name), escape(_LEVEL_WORDS.get(level, level)))
    else:
        subject = 'You have been invited to BLINC'
        lead = '{0} created a BLINC account for you.'.format(inviter_email)
        lead_html = '<b>{0}</b> created a BLINC account for you.'.format(escape(inviter_email))
    text = ('{lead}\n\nBLINC records and analyses small-group discussions. Choose a password to '
            'finish setting up your account (this link works for 7 days):\n{url}\n').format(lead=lead, url=url)
    html = _html(
        "You're invited to BLINC",
        [lead_html,
         'BLINC records and analyses small-group discussions. Choose a password to finish '
         'setting up your account. This link works for 7 days.'],
        'Set up my account', url,
        'If you were not expecting this, you can ignore this email and no account will be activated.')
    return mailer.send(to, subject, text, html)


def password_reset(to, token):
    url = mailer.link('reset-password?token={0}'.format(token))
    text = ('Someone asked to reset the password for your BLINC account ({email}).\n\n'
            'Choose a new password (this link works for 1 hour and can be used once):\n{url}\n\n'
            'If it was not you, ignore this email; your password stays the same.\n').format(email=to, url=url)
    html = _html(
        'Reset your password',
        ['Someone asked to reset the password for your BLINC account (<b>{0}</b>).'.format(escape(to)),
         'This link works for 1 hour and can be used once.'],
        'Choose a new password', url,
        "If it wasn't you, ignore this email; your password stays the same.")
    return mailer.send(to, 'Reset your BLINC password', text, html)
