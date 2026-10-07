"""Prove the mail route by hand, with the service's own code, then read the test back from the mailbox.

Two ways to send the test:

    python tools/mail_proof.py --username <mailbox sign-in> --to <test address> --sender "Name <address>"
        sends it as the mailbox user through the SMTP relay;

    python tools/mail_proof.py --username <mailbox sign-in> --to <test address> --flow <flow uuid> --contact <contact uuid>
        has TextIt start the test flow on the test contact, exactly as the hourly check does.

The app password (and, for a flow, the TextIt API token) are asked for at hidden prompts; they
are never printed, logged or written to a file. Run this after the app password, the test
address, the flow or the contact changes, before trusting the service. Needs the tzdata
package on Windows (the service's rule module names a time zone).
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 2)[0] + "/src")

import mailer  # noqa: E402
import mailread  # noqa: E402
import textit  # noqa: E402
from mailcheck import reference  # noqa: E402
from rule import CENTRAL  # noqa: E402

END = "=== mail proof: END ==="
PLAIN = {
    "SMTPAuthenticationError": "the relay refused the sign-in (app password wrong, mistyped or revoked)",
    "SMTPRecipientsRefused": "the relay refused the test address",
    "SMTPSenderRefused": "the relay refused the From address",
    "TimeoutError": "no answer in time",
    "gaierror": "the server name could not be looked up",
}


def clock(dt: datetime) -> str:
    return dt.astimezone(CENTRAL).strftime("%-I:%M:%S %p CT" if sys.platform != "win32" else "%#I:%M:%S %p CT")


def gmail_labels(username: str, password: str, address: str, ref: str, now: datetime) -> str:
    """Where the mail provider filed the message (information only; Gmail's label extension)."""
    conn = mailread._open(username, password, mailread.imaplib.IMAP4_SSL)
    try:
        conn.select(mailread._folder(conn, rb"\All"), readonly=True)
        typ, data = conn.uid("SEARCH", None, "TO", f'"{address}"', "SINCE", mailread._imap_date(now - timedelta(days=1)))
        uids = (data[0] or b"").split()
        if not uids:
            return "not found"
        typ, data = conn.uid("FETCH", b",".join(uids).decode(), "(X-GM-LABELS BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
        items = list(data or [])
        for i, item in enumerate(items):
            if isinstance(item, tuple) and ref.encode() in item[1]:
                text = item[0] + (items[i + 1] if i + 1 < len(items) and isinstance(items[i + 1], bytes) else b"")
                m = re.search(rb"X-GM-LABELS \(([^)]*)\)", text)
                labels = m.group(1).decode().replace('"', "").replace("\\\\", "\\").strip() if m else ""
                return labels or "no label (All Mail only)"
        return "not found"
    finally:
        mailread._close(conn)


def run(username: str, sender: str, to: str, password: str, wait_seconds: int = 120, pause: int = 10,
        sleep=time.sleep, out=print, flow: str = "", contact: str = "", token: str = "") -> int:
    now = datetime.now(timezone.utc)
    ref = reference(now)
    subject = f"Early Alert email test {ref}"
    sent_at = datetime.now(timezone.utc)  # the moment the send was asked for; arrival is measured from here
    if flow:
        out(f"1 of 3  asking TextIt to start the test flow on the test contact, reference {ref}")
        local = sent_at.astimezone(CENTRAL)
        sent_ct = f"{local.hour % 12 or 12}:{local:%M %p} CT on {local:%a %b} {local.day}"  # the service's own wording
        ok, reason = textit.start_flow(token, flow, contact, {"reference": ref, "sent_ct": sent_ct})
        if not ok:
            out(f"        TEXTIT DID NOT ACCEPT THE START: {reason}")
            out("RESULT: FAIL - no test email was asked for")
            out(END)
            return 2
        out(f"        ACCEPTED by TextIt at {clock(sent_at)} (TextIt now sends the email on its own)")
        what = "after TextIt was asked"
    else:
        out(f"1 of 3  sending one test email to the test address, subject: {subject}")
        r = mailer.send(username, password, sender, [to], subject,
                        "This is a test of the mail route used by the triage monitor. Nothing is wrong and no reply is needed.\n")
        if r["outcome"] != "sent":
            name = r["reason"].split()[0] if r["reason"] else "unknown"
            out(f"        SEND FAILED after {r['attempts']} attempts: {r['reason']} - {PLAIN.get(name, 'see the reason')}")
            out("RESULT: FAIL - nothing was sent")
            out(END)
            return 2
        out(f"        SENT at {clock(sent_at)} (attempts: {r['attempts']})")
        what = "after the send began"
    out("2 of 3  reading the mailbox (All Mail, messages addressed to the test address, headers only)")
    waited = 0
    while True:
        try:
            found = [a for a in mailread.read_tests(username, password, to, datetime.now(timezone.utc)) if a.reference == ref]
        except Exception as exc:  # noqa: BLE001
            out(f"        READ FAILED: {type(exc).__name__}: {str(exc)[:160]}")
            out("RESULT: FAIL - the email was sent but the mailbox could not be read")
            out(END)
            return 3
        if found:
            arrived = max(a.arrived for a in found)
            # The mailbox keeps whole seconds; a sub-second arrival can read as 0 s.
            seconds = max(0, int((arrived - sent_at.replace(microsecond=0)).total_seconds()))
            out(f"        FOUND after {waited} s of looking: {len(found)} message, received {clock(arrived)}, "
                f"{seconds} s {what}")
            break
        if waited >= wait_seconds:
            out(f"        NOT FOUND after {waited} s")
            out("RESULT: FAIL - the email was sent but did not appear in the mailbox")
            out(END)
            return 4
        out(f"        not there yet ({waited} s); looking again in {pause} s")
        sleep(pause)
        waited += pause
    try:
        out(f"3 of 3  where the mail provider filed it (information only): {gmail_labels(username, password, to, ref, sent_at)}")
    except Exception as exc:  # noqa: BLE001
        out(f"3 of 3  where the mail provider filed it: could not be read ({type(exc).__name__})")
    if flow:
        out("RESULT: PASS - TextIt's flow sent the test, it reached the test address, read back")
    else:
        out("RESULT: PASS - sent as the mailbox user, delivered to the test address, read back")
    out(END)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--username", required=True, help="the mailbox user's sign-in address")
    p.add_argument("--to", required=True, help="the test address")
    p.add_argument("--sender", default="", help='relay mode: the From line, e.g. "Name <address>"')
    p.add_argument("--flow", default="", help="flow mode: the test flow's uuid")
    p.add_argument("--contact", default="", help="flow mode: the test contact's uuid")
    args = p.parse_args()
    if bool(args.flow) != bool(args.contact) or not (args.flow or args.sender):
        p.error("give --sender (relay mode), or both --flow and --contact (flow mode)")
    secret = getpass.getpass("App password (paste it; nothing will show), then Enter: ").replace(" ", "").strip()
    token = getpass.getpass("TextIt API token (paste it; nothing will show), then Enter: ").strip() if args.flow else ""
    if not secret or (args.flow and not token):
        print("a password or token was not entered")
        print("RESULT: FAIL - nothing was sent")
        print(END)
        sys.exit(1)
    sys.exit(run(args.username, args.sender, args.to, secret, wait_seconds=300 if args.flow else 120,
                 flow=args.flow, contact=args.contact, token=token))
