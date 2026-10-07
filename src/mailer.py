"""Send one plain-text email through the Workspace SMTP relay, signed in as the mailbox user.

One retry after a short pause; the caller logs the outcome. Nothing here logs.
The sign-in name's domain is used for EHLO. Recipients and the From line are
settings of the running service; none is in this repository.
"""

from __future__ import annotations

import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Sequence

HOST = "smtp-relay.gmail.com"
PORT = 587
TIMEOUT_SECONDS = 10
RETRY_AFTER_SECONDS = 5


def build(from_header: str, recipients: Sequence[str], subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_header
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    domain = parseaddr(from_header)[1].rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    return msg


def send(username: str, password: str, from_header: str, recipients: Sequence[str],
         subject: str, body: str, factory=smtplib.SMTP, sleep=time.sleep) -> dict:
    """Returns {'outcome': 'sent'|'failed', 'attempts': n, 'reason': str}."""
    msg = build(from_header, recipients, subject, body)
    sender = parseaddr(from_header)[1]
    ehlo = username.rpartition("@")[2] or None
    reason = ""
    for attempt in (1, 2):
        try:
            smtp = factory(HOST, PORT, local_hostname=ehlo, timeout=TIMEOUT_SECONDS)
            try:
                smtp.starttls()
                smtp.login(username, password)
                refused = smtp.send_message(msg, from_addr=sender, to_addrs=list(recipients))
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
            finally:
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001
                    pass
            return {"outcome": "sent", "attempts": attempt, "reason": ""}
        except smtplib.SMTPRecipientsRefused as exc:
            reason = f"SMTPRecipientsRefused ({len(exc.recipients)} of {len(recipients)})"
        except smtplib.SMTPResponseException as exc:
            reason = f"{type(exc).__name__} {exc.smtp_code}"
        except Exception as exc:  # noqa: BLE001
            reason = type(exc).__name__
        if attempt == 1:
            sleep(RETRY_AFTER_SECONDS)
    return {"outcome": "failed", "attempts": 2, "reason": reason}
