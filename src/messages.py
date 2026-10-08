"""The words of every email the service sends itself. Plain text, ASCII only.

A subject starts with what it is about and its state, in capitals, so the kind is read at a
glance and a "resumed" or "arriving again" can never be taken for the alert it closes:

  TRIAGE SILENT / TRIAGE STILL SILENT / TRIAGE RESUMED / TRIAGE NOT RESOLVED     the triage rule
  EMAIL NOT ARRIVING / EMAIL STILL NOT ARRIVING / EMAIL ARRIVING AGAIN          the hourly email test

The lines Cloud Monitoring carries (rule.compose) are separate and unchanged; this
module is the service's own email.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from mailcheck import Message, Result
from rule import CENTRAL, CHECKS, Decision, _duration, utc

PREFIX = "[Early Alert] "
TEST_PREFIX = "TEST - "
TEST_LEAD = "THIS IS A TEST of the monitor's messages. The numbers below are live; nothing is wrong.\n\n"

MAIL_CHECKS = (
    "Once an hour this monitor has TextIt send a test email to a mailbox it reads, and looks "
    "for it 10 minutes later. Two tests in a row must go missing before this message is sent. "
    "It does not see the counselors' own inboxes."
)


def when(dt: Optional[datetime], now: datetime) -> str:
    """'2:02 PM CT' on the same Central day as `now`, otherwise with the day."""
    if dt is None:
        return "none in the last 2 days"
    local = utc(dt).astimezone(CENTRAL)
    clock = local.strftime("%-I:%M %p CT")
    if local.date() == utc(now).astimezone(CENTRAL).date():
        return clock
    return clock + local.strftime(" on %a %b %-d")


def email_line(state: str, result: Optional[Result], last_arrival: Optional[datetime], note: str, now: datetime) -> str:
    """The one line a triage alert carries about the email check.
    state: 'off' | 'not_configured' | 'on'."""
    if state == "off":
        return "Email check: off."
    if state == "not_configured":
        return "Email check: not configured."
    if note:
        return f"Email check: result {note}."
    last = when(last_arrival, now)
    if result is None:
        if last_arrival is None:
            return "Email check: no result yet."
        return f"Email check: no result yet since the monitor restarted (last test email arrived {last})."
    if result.state == "arriving":
        return f"Email check: test emails are arriving (last arrived {last})."
    if result.state == "not_arriving":
        if last_arrival is None:
            return "Email check: test emails are NOT arriving (none has arrived in the last 2 days)."
        return f"Email check: test emails are NOT arriving (last arrived {last}; tests since then have not arrived)."
    if last_arrival is None:
        return f"Email check: could not run at {when(result.at, now)} (TextIt did not accept the test)."
    return f"Email check: could not run at {when(result.at, now)} (TextIt did not accept the test); last test arrived {last}."


def _finish(subject: str, body: str, test: bool) -> Tuple[str, str]:
    subject = PREFIX + (TEST_PREFIX if test else "") + subject
    body = (TEST_LEAD if test else "") + body
    subject.encode("ascii")
    body.encode("ascii")
    return subject, body


def triage(d: Decision, line: Optional[str], now: datetime, test: bool = False) -> Optional[Tuple[str, str]]:
    """(subject, body) of the service's own email for a triage decision, or None."""
    if d.kind is None:
        return None
    m = d.now
    waiting = f"{m.waiting} request{'' if m.waiting == 1 else 's'}"
    since = "none in the last 3 days" if m.last_det is None else when(m.last_det, now)
    silent = "" if m.silence_minutes is None else _duration(m.silence_minutes)
    silent_par = f" ({silent})" if silent else ""
    checks = "What this checks: " + CHECKS
    if d.kind == "alert":
        subject = f"TRIAGE SILENT: {silent + ', ' if silent else ''}{waiting} waiting"
        body = (
            f"No triage determination since {since}{silent_par}.\n"
            f"{waiting.capitalize()} waiting 30 minutes or more.\n"
            + (f"{line}\n" if line else "")
            + "\nPlease check that triage requests are reaching the counselors. A reminder follows every "
            "2 hours while this lasts, and one message when it ends.\n\n" + checks + "\n"
        )
    elif d.kind == "repeat":
        subject = f"TRIAGE STILL SILENT: {silent + ', ' if silent else ''}{waiting} waiting"
        body = (
            f"Still no triage determination since {since}{silent_par}.\n"
            f"{waiting.capitalize()} waiting 30 minutes or more.\n"
            + (f"{line}\n" if line else "")
            + "\nThis reminder repeats every 2 hours while triage stays silent.\n\n" + checks + "\n"
        )
    elif d.ended_reason == "determination":
        subject = f"TRIAGE RESUMED: determination at {since}"
        body = (
            f"A triage determination landed at {since}. This closes the alert sent earlier.\n"
            f"{waiting.capitalize()} still waiting 30 minutes or more.\n\n" + checks + "\n"
        )
    elif d.ended_reason == "window_closed":
        subject = f"TRIAGE STILL SILENT: monitor hours over until 10:30 AM CT, {m.waiting} waiting"
        body = (
            f"Triage is STILL silent. This is NOT resolved.\n"
            f"No determination since {since}{silent_par}; {waiting} still waiting.\n\n"
            "The monitor's hours are over for today (10:30 AM to 8:00 PM CT). Nothing more will be "
            "sent until it checks again at 10:30 AM CT.\n\n" + checks + "\n"
        )
    else:
        subject = "TRIAGE NOT RESOLVED: alert ended, fewer than 3 requests waiting"
        body = (
            f"Triage is NOT resolved. The alert sent earlier ended only because fewer than 3 requests are now waiting.\n"
            f"There is still no determination since {since}. Requests older than 14 hours are no "
            "longer counted.\n\n" + checks + "\n"
        )
    return _finish(subject, body, test)


def email_check(msg: Message, now: datetime) -> Tuple[str, str]:
    """(subject, body) of the service's own email for an email-check message."""
    since = when(msg.since, now)
    checks = "What this checks: " + MAIL_CHECKS
    if msg.kind == "email_ended":
        arrived = when(msg.arrived, now)
        subject = f"EMAIL ARRIVING AGAIN: TextIt test email at {arrived}"
        was = "No test had arrived in the 2 days before it." if msg.since is None else f"None had arrived since {since}."
        body = (
            f"A test email sent through TextIt arrived at {arrived}. This closes the alert sent earlier.\n"
            f"{was}\n\n" + checks + "\n"
        )
        return _finish(subject, body, msg.test)
    tail = "none in the last 2 days" if msg.since is None else f"none since {since}"
    sent = " and ".join(when(s, now) for s in msg.sent)
    tests = f"Two tests were sent ({sent}) and neither reached the mailbox.\n" if sent else ""
    last = "No test has arrived in the last 2 days." if msg.since is None else f"The last test that arrived: {since}."
    if msg.kind == "email_alert":
        subject = f"EMAIL NOT ARRIVING: TextIt test emails, {tail}"
        body = (
            "The hourly test email sent through TextIt has not arrived.\n"
            + tests + last + "\n\n"
            "Emails sent by TextIt flows, including triage requests to counselors, may not be arriving. "
            "A reminder follows every 2 hours while this lasts, and one message when a test arrives again.\n\n"
            + checks + "\n"
        )
    else:
        subject = f"EMAIL STILL NOT ARRIVING: TextIt test emails, {tail}"
        body = (
            "The hourly test email sent through TextIt is still not arriving.\n"
            + tests + last + "\n\n"
            "This reminder repeats every 2 hours while tests stay missing.\n\n" + checks + "\n"
        )
    return _finish(subject, body, msg.test)


def email_check_google(msg: Message, subject: str, body: str) -> dict:
    """The same message in the shape the existing log-based policy extracts
    (kind, subject, headline, last_determination, waiting, checks)."""
    lead = body[len(TEST_LEAD):] if body.startswith(TEST_LEAD) else body
    headline = " ".join(part.strip() for part in lead.split("\n\n")[0].splitlines() if part.strip())
    return {
        "kind": msg.kind,
        "subject": "Early Alert: " + subject[len(PREFIX):],
        "headline": headline,
        "last_determination": "n/a - email check",
        "waiting": "n/a - email check",
        "checks": MAIL_CHECKS,
    }
