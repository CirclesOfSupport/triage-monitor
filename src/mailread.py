"""Read the test mailbox over IMAP, and keep it from growing.

Only messages addressed to the test address are ever searched, fetched or moved.
Only two header fields are fetched (To, Subject) plus the time the mailbox received
the message; no body is fetched, so none can be logged.

The folder read is All Mail, not the inbox: Gmail keeps a message a user sends to
their own alternate address out of the inbox ("You can find the message in Sent
Mail or All Mail"), and after the sender change TextIt sends as this same user.
The folders are found by their special-use flags (\\All, \\Trash), not by name,
because the names follow the account's language.
"""

from __future__ import annotations

import email
import email.header
import imaplib
import re
from datetime import datetime, timedelta, timezone
from typing import List

from mailcheck import KEEP, REFERENCE_RE, Arrival

HOST = "imap.gmail.com"
PORT = 993
TIMEOUT_SECONDS = 10
MAX_MOVED_PER_CLEAN = 200

_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
_DATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


def _imap_date(d: datetime) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def _folder(conn, flag: bytes) -> str:
    typ, data = conn.list()
    if typ != "OK":
        raise RuntimeError("the folder list could not be read")
    for raw in data or []:
        if not raw:
            continue
        m = _LIST_RE.match(raw if isinstance(raw, bytes) else raw[0])
        if m and flag.lower() in m.group("flags").lower().split():
            return m.group("name").decode("utf-8", "replace").strip()
    raise RuntimeError(f"no folder carries the {flag.decode()} flag")


def _decode(value) -> str:
    out = []
    for part, enc in email.header.decode_header(value or ""):
        out.append(part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part)
    return "".join(out)


def _open(username: str, password: str, factory):
    conn = factory(HOST, PORT, timeout=TIMEOUT_SECONDS)
    typ, _ = conn.login(username, password)
    if typ != "OK":
        raise RuntimeError("the mailbox sign-in was refused")
    return conn


def _close(conn) -> None:
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        pass


def read_tests(username: str, password: str, address: str, now: datetime,
               factory=imaplib.IMAP4_SSL) -> List[Arrival]:
    """Every test message addressed to `address` and received in the last KEEP days."""
    conn = _open(username, password, factory)
    try:
        typ, _ = conn.select(_folder(conn, rb"\All"), readonly=True)
        if typ != "OK":
            raise RuntimeError("the All Mail folder could not be opened")
        since = _imap_date(now.astimezone(timezone.utc) - KEEP - timedelta(days=1))
        typ, data = conn.uid("SEARCH", None, "TO", f'"{address}"', "SINCE", since)
        if typ != "OK":
            raise RuntimeError("the mailbox search failed")
        uids = (data[0] or b"").split()
        if not uids:
            return []
        typ, data = conn.uid("FETCH", b",".join(uids).decode(),
                             "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (TO SUBJECT)])")
        if typ != "OK":
            raise RuntimeError("the mailbox fetch failed")
        found: List[Arrival] = []
        items = list(data or [])
        for i, item in enumerate(items):
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta, header = item[0], item[1]
            # A server may give the received time before the header block or after it.
            when = _DATE_RE.search(meta)
            if not when and i + 1 < len(items) and isinstance(items[i + 1], bytes):
                when = _DATE_RE.search(items[i + 1])
            if not when:
                continue
            msg = email.message_from_bytes(header)
            if address.lower() not in _decode(msg.get("To")).lower():
                continue
            ref = REFERENCE_RE.search(_decode(msg.get("Subject")))
            if not ref:
                continue
            arrived = datetime.strptime(when.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z")
            found.append(Arrival(reference=ref.group(0), arrived=arrived.astimezone(timezone.utc)))
        return found
    finally:
        _close(conn)


def clean(username: str, password: str, address: str, now: datetime,
          factory=imaplib.IMAP4_SSL) -> int:
    """Move messages addressed to `address` and older than KEEP days to Trash (which the
    mail provider empties on its own schedule). Returns how many were moved."""
    conn = _open(username, password, factory)
    try:
        allmail = _folder(conn, rb"\All")
        trash = _folder(conn, rb"\Trash")
        typ, _ = conn.select(allmail, readonly=False)
        if typ != "OK":
            raise RuntimeError("the All Mail folder could not be opened")
        before = _imap_date(now.astimezone(timezone.utc) - KEEP)
        typ, data = conn.uid("SEARCH", None, "TO", f'"{address}"', "BEFORE", before)
        if typ != "OK":
            raise RuntimeError("the mailbox search failed")
        uids = (data[0] or b"").split()[:MAX_MOVED_PER_CLEAN]
        if not uids:
            return 0
        ids = b",".join(uids).decode()
        # Copy to Trash, then mark the original deleted: this works on every IMAP server and
        # every Python version (the MOVE command is not in older standard libraries).
        typ, _ = conn.uid("COPY", ids, trash)
        if typ != "OK":
            raise RuntimeError("the copy to Trash failed")
        conn.uid("STORE", ids, "+FLAGS.SILENT", r"(\Deleted)")
        return len(uids)
    finally:
        _close(conn)
