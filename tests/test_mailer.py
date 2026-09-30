"""Tests for outgoing email (src/server/mailer.py, emails.py). No network:
requests.post is replaced, and the unconfigured path only logs."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "server"))

import emails  # noqa: E402
import mailer  # noqa: E402

ENV = {
    "MAILERSEND_API_TOKEN": "tok",
    "MAIL_FROM_EMAIL": "no-reply@nublinc.com",
    "MAIL_REPLY_TO": "tiiltlab@gmail.com",
    "MAIL_LINK_BASE": "https://nublinc.com/",
}


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.text = ""
        self.headers = {"X-Message-Id": "m1"}


def _capture(monkeypatch, status=202):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "json": json, "headers": headers})
        return _Resp(status)

    monkeypatch.setattr(mailer.requests, "post", fake_post)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MAILERSEND_API_URL", raising=False)
    return sent


def test_unconfigured_only_logs(monkeypatch, caplog):
    monkeypatch.delenv("MAILERSEND_API_TOKEN", raising=False)
    monkeypatch.setattr(mailer.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert mailer.send("a@b.org", "Hi", "body", "<p>body</p>") is False
    assert "NOT sent" in caplog.text


def test_payload_shape_and_auth(monkeypatch):
    sent = _capture(monkeypatch)
    assert mailer.send("a@b.org", "Hi", "text", "<p>html</p>", background=False) is True
    call = sent[0]
    assert call["url"] == mailer.API_URL
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["json"]["from"] == {"email": "no-reply@nublinc.com", "name": "BLINC"}
    assert call["json"]["to"] == [{"email": "a@b.org"}]
    assert call["json"]["reply_to"] == {"email": "tiiltlab@gmail.com"}


def test_rejection_reported(monkeypatch):
    _capture(monkeypatch, status=422)
    assert mailer.send("a@b.org", "Hi", "t", "h", background=False) is False


def test_links_use_link_base(monkeypatch):
    monkeypatch.setenv("MAIL_LINK_BASE", "https://nublinc.com/")
    assert mailer.link("/reset-password?token=x") == "https://nublinc.com/reset-password?token=x"


def test_folder_names_are_escaped_in_html(monkeypatch):
    sent = []
    monkeypatch.setenv("MAIL_LINK_BASE", "https://nublinc.com")
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html: sent.append(html) or True)
    emails.folder_shared("a@b.org", "x@y.org", '<script>alert(1)</script>', 7, "viewer")
    emails.invite("a@b.org", "x@y.org", "tok", folder_name='"><img src=x>', level="editor")
    assert "<script>" not in sent[0] and "&lt;script&gt;" in sent[0]
    assert "<img" not in sent[1]


def test_invite_and_reset_links(monkeypatch):
    sent = []
    monkeypatch.setenv("MAIL_LINK_BASE", "https://nublinc.com")
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html: sent.append(text) or True)
    emails.invite("a@b.org", "x@y.org", "INV")
    emails.password_reset("a@b.org", "RST")
    assert "https://nublinc.com/reset-password?token=INV&invite=1" in sent[0]
    assert "https://nublinc.com/reset-password?token=RST" in sent[1]
