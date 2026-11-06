"""Email sending. Protocol + SMTP implementation + recording stub for tests/dev."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol


class Emailer(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None:
        ...


class RecordingEmailer:
    """Sends nothing — records instead (tests/dev)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


class SMTPEmailer:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        sender: str,
        use_tls: bool = True,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.use_tls = use_tls

    def send(self, *, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        # timeout is MANDATORY: the scheduler is single-threaded and reminders runs before
        # invoice_expiry/node_health — a hung relay without a timeout would freeze all
        # lifecycle work.
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(msg)


def emailer_from_settings(settings) -> Emailer | None:
    if not settings.smtp_host or not settings.smtp_from:
        return None
    return SMTPEmailer(
        settings.smtp_host,
        settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        sender=settings.smtp_from,
        use_tls=settings.smtp_use_tls,
    )
