"""The three modules that talk to the outside (TextIt, the mailbox, SMTP), each against a fake
of the standard-library object it uses. Nothing here touches the network."""

import io
import json
import smtplib
import socket
import sys
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

import mailer  # noqa: E402
import mailread  # noqa: E402
import textit  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 21, 10, tzinfo=UTC)
ADDRESS = "probe@example.org"


# ---- TextIt -------------------------------------------------------------------------------

class Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError(textit.API, code, "x", headers or {}, io.BytesIO(body))


def test_start_flow_accepted_sends_one_contact_and_the_params():
    seen = {}

    def opener(req, timeout):
        seen["req"], seen["timeout"] = req, timeout
        return Resp(201)

    ok, reason = textit.start_flow("tok", "flow-1", "contact-1", {"reference": "EA-MC-20261007T2100Z"}, opener=opener)
    assert (ok, reason) == (True, "201") and seen["timeout"] == 8
    req = seen["req"]
    assert req.full_url == "https://textit.com/api/v2/flow_starts.json" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Token tok"
    assert json.loads(req.data) == {"flow": "flow-1", "contacts": ["contact-1"], "restart_participants": True,
                                    "params": {"reference": "EA-MC-20261007T2100Z"}}


def test_start_flow_refusals_and_timeouts_are_reasons_not_exceptions():
    def raising(exc):
        def opener(req, timeout):
            raise exc
        return opener

    assert textit.start_flow("tok", "f", "c", {}, opener=raising(http_error(400, b'{"flow":["No such object"]}'))) == (
        False, 'HTTP 400: {"flow":["No such object"]}')
    assert textit.start_flow("s3cr3t", "f", "c", {}, opener=raising(http_error(401, b'{"detail":"Invalid token s3cr3t"}'))) == (
        False, 'HTTP 401: {"detail":"Invalid token [redacted]"}')
    assert textit.start_flow("tok", "f", "c", {}, opener=raising(http_error(429, b"", {"Retry-After": "1740"}))) == (
        False, "HTTP 429 (TextIt asks to wait 1740 s)")
    assert textit.start_flow("tok", "f", "c", {}, opener=raising(http_error(502))) == (False, "HTTP 502")
    assert textit.start_flow("tok", "f", "c", {}, opener=raising(socket.timeout())) == (False, "timeout after 8 s")
    assert textit.start_flow("tok", "f", "c", {}, opener=raising(urllib.error.URLError(socket.timeout()))) == (
        False, "timeout after 8 s")
    assert textit.start_flow("tok", "f", "c", {}, opener=raising(urllib.error.URLError(OSError("down")))) == (
        False, "network error: OSError")
    assert textit.start_flow("tok", "f", "c", {}, opener=lambda req, timeout: Resp(200)) == (False, "HTTP 200")


# ---- the mailbox ------------------------------------------------------------------------------

def header(to, subject):
    return f"To: {to}\r\nSubject: {subject}\r\n\r\n".encode()


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL. `messages`: uid -> (to, subject, internal date)."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.selected = None
        FakeIMAP.instances.append(self)

    messages = {}
    login_ok = True

    def login(self, user, password):
        self.calls.append(("login", user))
        return ("OK" if self.login_ok else "NO", [b""])

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
            b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"',
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Papierkorb"',
        ]

    def select(self, name, readonly=False):
        self.selected = (name, readonly)
        self.calls.append(("select", name, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            to = args[2].strip('"')
            uids = [str(u).encode() for u, (mto, _, _) in sorted(self.messages.items()) if to.lower() in mto.lower()]
            return "OK", [b" ".join(uids)]
        if command == "FETCH":
            out = []
            for u in args[0].split(","):
                mto, subject, date = self.messages[int(u)]
                meta = f'{u} (UID {u} INTERNALDATE "{date}" BODY[HEADER.FIELDS (TO SUBJECT)] {{60}}'.encode()
                out += [(meta, header(mto, subject)), b")"]
            return "OK", out
        return "OK", [b""]

    def logout(self):
        self.calls.append(("logout",))


def fresh_imap(messages, login_ok=True):
    FakeIMAP.instances = []
    FakeIMAP.messages = messages
    FakeIMAP.login_ok = login_ok
    return FakeIMAP


def test_read_tests_opens_all_mail_read_only_by_its_flag_and_fetches_headers_only():
    factory = fresh_imap({
        7: (ADDRESS, "Early Alert email test EA-MC-20261007T2100Z", "07-Oct-2026 21:00:48 +0000"),
        9: (f"Probe <{ADDRESS.upper()}>", "Fwd: =?utf-8?q?EA-MC-20261007T2000Z?=", "07-Oct-2026 15:01:02 -0500"),
    })
    found = mailread.read_tests("sender@example.org", "pw", ADDRESS, NOW, factory=factory)
    assert [(a.reference, a.arrived) for a in found] == [
        ("EA-MC-20261007T2100Z", datetime(2026, 10, 7, 21, 0, 48, tzinfo=UTC)),
        ("EA-MC-20261007T2000Z", datetime(2026, 10, 7, 20, 1, 2, tzinfo=UTC)),
    ]
    conn = factory.instances[0]
    assert (conn.host, conn.port, conn.timeout) == ("imap.gmail.com", 993, 10)
    assert ("select", '"[Gmail]/Alle Nachrichten"', True) in conn.calls  # found by \All, opened read-only
    search = [c for c in conn.calls if c[0] == "SEARCH"][0]
    assert search == ("SEARCH", None, "TO", f'"{ADDRESS}"', "SINCE", "04-Oct-2026")
    fetch = [c for c in conn.calls if c[0] == "FETCH"][0]
    assert fetch[2] == "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (TO SUBJECT)])"  # no body, and PEEK marks nothing read
    assert conn.calls[-1] == ("logout",)


def test_read_tests_ignores_what_is_not_a_test_or_not_to_the_test_address():
    factory = fresh_imap({
        1: (ADDRESS, "Lunch on Friday?", "07-Oct-2026 21:00:48 +0000"),
        2: ("someone-else@example.org", "EA-MC-20261007T2100Z", "07-Oct-2026 21:00:48 +0000"),
    })
    # uid 2 is not addressed to the test address, so the search never returns it
    assert mailread.read_tests("u", "pw", ADDRESS, NOW, factory=factory) == []


def test_read_tests_with_an_empty_mailbox_fetches_nothing():
    factory = fresh_imap({})
    assert mailread.read_tests("u", "pw", ADDRESS, NOW, factory=factory) == []
    assert not [c for c in factory.instances[0].calls if c[0] == "FETCH"]


def test_a_refused_sign_in_raises_and_still_logs_out():
    factory = fresh_imap({}, login_ok=False)
    try:
        mailread.read_tests("u", "pw", ADDRESS, NOW, factory=factory)
    except RuntimeError as exc:
        assert "refused" in str(exc) and "pw" not in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_clean_moves_only_old_messages_addressed_to_the_test_address():
    factory = fresh_imap({
        3: (ADDRESS, "EA-MC-20261004T0100Z", "04-Oct-2026 01:00:30 +0000"),
        4: (ADDRESS, "EA-MC-20261004T0200Z", "04-Oct-2026 02:00:30 +0000"),
        5: ("staff@example.org", "Board minutes", "01-Sep-2026 09:00:00 +0000"),
    })
    moved = mailread.clean("u", "pw", ADDRESS, NOW, factory=factory)
    conn = factory.instances[0]
    assert moved == 2
    search = [c for c in conn.calls if c[0] == "SEARCH"][0]
    assert search == ("SEARCH", None, "TO", f'"{ADDRESS}"', "BEFORE", "05-Oct-2026")  # older than 2 days
    assert ("COPY", "3,4", '"[Gmail]/Papierkorb"') in conn.calls  # Trash, found by its flag
    assert ("STORE", "3,4", "+FLAGS.SILENT", r"(\Deleted)") in conn.calls
    assert ("select", '"[Gmail]/Alle Nachrichten"', False) in conn.calls
    assert not any("5" in str(c[1]).split(",") for c in conn.calls if c[0] in ("COPY", "STORE"))


def test_received_time_after_the_header_block_is_read_too():
    class Trailing(FakeIMAP):
        def uid(self, command, *args):
            if command != "FETCH":
                return super().uid(command, *args)
            return "OK", [(b'1 (UID 7 BODY[HEADER.FIELDS (TO SUBJECT)] {60}', header(ADDRESS, "EA-MC-20261007T2100Z")),
                          b' INTERNALDATE "07-Oct-2026 21:00:48 +0000")']

    fresh_imap({7: (ADDRESS, "EA-MC-20261007T2100Z", "unused")})
    found = mailread.read_tests("u", "pw", ADDRESS, NOW, factory=Trailing)
    assert [(a.reference, a.arrived) for a in found] == [("EA-MC-20261007T2100Z", datetime(2026, 10, 7, 21, 0, 48, tzinfo=UTC))]


def test_clean_with_nothing_old_moves_nothing():
    factory = fresh_imap({})
    assert mailread.clean("u", "pw", ADDRESS, NOW, factory=factory) == 0
    assert not [c for c in factory.instances[0].calls if c[0] in ("COPY", "STORE")]


# ---- the service's own email ---------------------------------------------------------------------

class FakeSMTP:
    script = []  # one entry per connection: None (works) or an exception to raise at login
    log = []

    def __init__(self, host, port, local_hostname=None, timeout=None):
        FakeSMTP.log.append(("connect", host, port, local_hostname, timeout))
        self.fail = FakeSMTP.script.pop(0) if FakeSMTP.script else None

    def starttls(self):
        FakeSMTP.log.append(("starttls",))

    def login(self, user, password):
        if self.fail:
            raise self.fail
        FakeSMTP.log.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        FakeSMTP.log.append(("send", from_addr, tuple(to_addrs), msg))
        return {}

    def quit(self):
        FakeSMTP.log.append(("quit",))


def run_send(script):
    FakeSMTP.script, FakeSMTP.log = list(script), []
    naps = []
    r = mailer.send("sender@example.org", "pw", "Monitor <alerts@example.net>",
                    ["first@example.com", "second@example.com"], "[Early Alert] SILENT: triage", "line one\n",
                    factory=FakeSMTP, sleep=naps.append)
    return r, naps


def test_send_goes_through_the_relay_with_tls_and_the_sign_in():
    r, naps = run_send([None])
    assert r == {"outcome": "sent", "attempts": 1, "reason": ""} and naps == []
    assert FakeSMTP.log[0] == ("connect", "smtp-relay.gmail.com", 587, "example.org", 10)
    assert FakeSMTP.log[1] == ("starttls",) and FakeSMTP.log[2] == ("login", "sender@example.org", "pw")
    _, sender, to, msg = FakeSMTP.log[3]
    assert sender == "alerts@example.net" and to == ("first@example.com", "second@example.com")
    assert msg["From"] == "Monitor <alerts@example.net>" and msg["To"] == "first@example.com, second@example.com"
    assert msg["Subject"] == "[Early Alert] SILENT: triage" and msg["Auto-Submitted"] == "auto-generated"
    assert msg["Message-ID"].endswith("@example.net>") and msg.get_content_type() == "text/plain"
    assert msg.get_content() == "line one\n"


def test_send_retries_once_after_five_seconds():
    r, naps = run_send([OSError("network is unreachable"), None])
    assert r == {"outcome": "sent", "attempts": 2, "reason": ""} and naps == [5]


def test_send_that_fails_twice_reports_the_class_and_code_only():
    r, naps = run_send([smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted pw")] * 2)
    assert r == {"outcome": "failed", "attempts": 2, "reason": "SMTPAuthenticationError 535"} and naps == [5]
    assert "pw" not in r["reason"].split()
