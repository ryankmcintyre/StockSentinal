"""Unit tests for app.notifications — new-member email alerting."""

import smtplib
from unittest.mock import MagicMock, patch

import pytest

import app.notifications as notif_module
from app.notifications import _send_email, send_new_member_notification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_smtp_env(monkeypatch, *, host="smtp.example.com", port="587", user="u", pw="p"):
    monkeypatch.setenv("SMTP_HOST", host)
    monkeypatch.setenv("SMTP_PORT", port)
    monkeypatch.setenv("SMTP_USERNAME", user)
    monkeypatch.setenv("SMTP_PASSWORD", pw)
    monkeypatch.setenv("NOTIFY_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("NOTIFY_FROM_EMAIL", "from@example.com")


# ---------------------------------------------------------------------------
# is_email_notifications_configured
# ---------------------------------------------------------------------------


def test_not_configured_when_smtp_host_missing(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("NOTIFY_ADMIN_EMAIL", "admin@example.com")

    from app.config import is_email_notifications_configured

    assert not is_email_notifications_configured()


def test_not_configured_when_admin_email_missing(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("NOTIFY_ADMIN_EMAIL", raising=False)

    from app.config import is_email_notifications_configured

    assert not is_email_notifications_configured()


def test_configured_when_both_set(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NOTIFY_ADMIN_EMAIL", "admin@example.com")

    from app.config import is_email_notifications_configured

    assert is_email_notifications_configured()


# ---------------------------------------------------------------------------
# get_notify_admin_emails — comma-separated list parsing
# ---------------------------------------------------------------------------


def test_admin_emails_empty_when_not_set(monkeypatch):
    monkeypatch.delenv("NOTIFY_ADMIN_EMAIL", raising=False)

    from app.config import get_notify_admin_emails

    assert get_notify_admin_emails() == []


def test_admin_emails_single(monkeypatch):
    monkeypatch.setenv("NOTIFY_ADMIN_EMAIL", "a@example.com")

    from app.config import get_notify_admin_emails

    assert get_notify_admin_emails() == ["a@example.com"]


def test_admin_emails_multiple(monkeypatch):
    monkeypatch.setenv("NOTIFY_ADMIN_EMAIL", "a@example.com, b@example.com , c@example.com")

    from app.config import get_notify_admin_emails

    assert get_notify_admin_emails() == ["a@example.com", "b@example.com", "c@example.com"]


# ---------------------------------------------------------------------------
# get_notify_from_email — falls back to SMTP_USERNAME
# ---------------------------------------------------------------------------


def test_notify_from_email_explicit(monkeypatch):
    monkeypatch.setenv("NOTIFY_FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("SMTP_USERNAME", "smtp@example.com")

    from app.config import get_notify_from_email

    assert get_notify_from_email() == "from@example.com"


def test_notify_from_email_fallback_to_smtp_username(monkeypatch):
    monkeypatch.delenv("NOTIFY_FROM_EMAIL", raising=False)
    monkeypatch.setenv("SMTP_USERNAME", "smtp@example.com")

    from app.config import get_notify_from_email

    assert get_notify_from_email() == "smtp@example.com"


# ---------------------------------------------------------------------------
# send_new_member_notification — happy path
# ---------------------------------------------------------------------------


def test_send_notification_calls_smtp(monkeypatch):
    _set_smtp_env(monkeypatch)

    smtp_instance = MagicMock()
    smtp_class = MagicMock(return_value=smtp_instance)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", smtp_class):
        send_new_member_notification("new@example.com", "New User")

    smtp_class.assert_called_once_with("smtp.example.com", 587)
    smtp_instance.sendmail.assert_called_once()
    args = smtp_instance.sendmail.call_args[0]
    assert args[0] == "from@example.com"
    assert args[1] == ["admin@example.com"]
    assert "new@example.com" in args[2]
    assert "New User" in args[2]


def test_send_notification_uses_ssl_for_port_465(monkeypatch):
    _set_smtp_env(monkeypatch, port="465")

    smtp_instance = MagicMock()
    smtp_class = MagicMock(return_value=smtp_instance)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP_SSL", smtp_class):
        send_new_member_notification("new@example.com", "New User")

    smtp_class.assert_called_once()
    assert smtp_class.call_args[0][1] == 465


def test_send_notification_skipped_when_not_configured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("NOTIFY_ADMIN_EMAIL", raising=False)

    called = []

    with patch.object(notif_module, "_send_email", side_effect=lambda **kw: called.append(kw)):
        send_new_member_notification("new@example.com", "New User")

    assert called == []


def test_send_notification_swallows_smtp_error(monkeypatch):
    _set_smtp_env(monkeypatch)

    with patch("smtplib.SMTP") as smtp_class:
        smtp_class.side_effect = smtplib.SMTPException("connection refused")
        # Should NOT raise
        send_new_member_notification("new@example.com", "New User")


def test_send_notification_handles_none_user_email(monkeypatch):
    _set_smtp_env(monkeypatch)

    smtp_instance = MagicMock()
    smtp_class = MagicMock(return_value=smtp_instance)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", smtp_class):
        send_new_member_notification(None, None)

    smtp_instance.sendmail.assert_called_once()


def test_send_notification_multiple_admin_emails(monkeypatch):
    _set_smtp_env(monkeypatch)
    monkeypatch.setenv("NOTIFY_ADMIN_EMAIL", "a@example.com,b@example.com")

    smtp_instance = MagicMock()
    smtp_class = MagicMock(return_value=smtp_instance)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", smtp_class):
        send_new_member_notification("new@example.com", "New")

    recipients = smtp_instance.sendmail.call_args[0][1]
    assert recipients == ["a@example.com", "b@example.com"]
