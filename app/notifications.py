"""Outbound notification helpers for admin alerts.

All functions in this module are designed to be safe to call in a FastAPI
BackgroundTask: they catch and log every exception so a delivery failure never
propagates back to the request that triggered the notification.
"""

import logging
import smtplib
import ssl
from html import escape
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import (
    get_notify_admin_emails,
    get_notify_from_email,
    get_smtp_host,
    get_smtp_password,
    get_smtp_port,
    get_smtp_username,
    is_email_notifications_configured,
)

logger = logging.getLogger(__name__)


def send_new_member_notification(
    user_email: str | None,
    display_name: str | None,
) -> None:
    """Send an email to every configured admin address when a new member joins.

    Silently skips (with a debug log) when email notifications are not
    configured.  Any SMTP error is caught and logged at ERROR level so the
    caller's request / background task is never disrupted.

    Args:
        user_email: The new member's email address.
        display_name: The new member's display name (may be the same as email).
    """
    if not is_email_notifications_configured():
        logger.debug("Email notifications not configured — skipping new-member alert")
        return

    admin_emails = get_notify_admin_emails()
    from_addr = get_notify_from_email()
    if not from_addr:
        logger.warning("NOTIFY_FROM_EMAIL / SMTP_USERNAME not set — cannot send notification")
        return

    joined_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = "StockSentinal — new member joined"

    label = display_name or user_email or "unknown"
    safe_user_email = escape(user_email) if user_email else "(not available)"
    safe_label = escape(label)
    body_text = (
        f"A new member has joined StockSentinal.\n\n"
        f"  Email:        {user_email or '(not available)'}\n"
        f"  Display name: {label}\n"
        f"  Joined at:    {joined_at}\n"
    )
    body_html = f"""\
<html><body>
<p>A new member has joined <strong>StockSentinal</strong>.</p>
<table cellpadding="4" style="border-collapse:collapse;">
  <tr><td><strong>Email</strong></td><td>{safe_user_email}</td></tr>
  <tr><td><strong>Display name</strong></td><td>{safe_label}</td></tr>
  <tr><td><strong>Joined at</strong></td><td>{joined_at}</td></tr>
</table>
</body></html>"""

    try:
        _send_email(
            from_addr=from_addr,
            to_addrs=admin_emails,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
        logger.info(
            "New-member notification sent to %d admin(s) for user %s",
            len(admin_emails),
            user_email,
        )
    except Exception:
        logger.error(
            "Failed to send new-member notification for user %s",
            user_email,
            exc_info=True,
        )


def _send_email(
    *,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str,
) -> None:
    """Build and deliver a multipart/alternative email via SMTP.

    Uses STARTTLS (SMTP on port 587 by default).  If the port is 465 an
    implicit TLS connection (SMTP_SSL) is used instead.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    host = get_smtp_host()
    port = get_smtp_port()
    username = get_smtp_username()
    password = get_smtp_password()

    context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            if username and password:
                server.login(username, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if username and password:
                server.login(username, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
