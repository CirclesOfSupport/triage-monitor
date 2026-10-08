"""The service with the email check and its own emails wired in. TextIt, the mailbox and SMTP
are replaced at the module seam; nothing here touches the network."""

import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

import main  # noqa: E402
from mailcheck import Arrival, MailCheck, reference  # noqa: E402
from rule import Request  # noqa: E402

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc
MIN = timedelta(minutes=1)

TOKEN = "tok-3f9a1c77e2b04d55"
PASSWORD = "abcdefghijklmnop"
SETTINGS = {
    "EMAIL_CHECK": "on",
    "TEXTIT_TOKEN": TOKEN,
    "TEXTIT_FLOW_UUID": "11111111-1111-1111-1111-111111111111",
    "TEXTIT_CONTACT_UUID": "22222222-2222-2222-2222-222222222222",
    "MAILCHECK_ADDRESS": "probe@example.org",
    "MAIL_USERNAME": "sender@example.org",
    "MAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
    "ALERT_FROM": "Monitor <alerts@example.net>",
    "ALERT_RECIPIENTS": "first@example.com, second@example.com",
}
NAMES = list(SETTINGS)


def ct(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CT).astimezone(UTC)


NOON = ct(2026, 7, 15, 12, 0)  # 17:00 UTC, on the hour, inside the window


class Fakes:
    def __init__(self):
        self.starts = []
        self.start_answer = (True, "201")
        self.start_raises = None
        self.box = []
        self.read_raises = None
        self.reads = 0
        self.sent = []
        self.send_results = []

    def start_flow(self, token, flow, contact, params, **kw):
        if self.start_raises:
            raise self.start_raises
        self.starts.append({"token": token, "flow": flow, "contact": contact, "params": params})
        return self.start_answer

    def read_tests(self, username, password, address, now, **kw):
        self.reads += 1
        if self.read_raises:
            raise self.read_raises
        return [a for a in self.box if a.arrived <= now]

    def clean(self, username, password, address, now, **kw):
        return 0

    def send(self, username, password, from_header, recipients, subject, body, **kw):
        self.sent.append({"username": username, "password": password, "from": from_header,
                          "to": tuple(recipients), "subject": subject, "body": body})
        return self.send_results.pop(0) if self.send_results else {"outcome": "sent", "attempts": 1, "reason": ""}


@pytest.fixture
def svc(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    f = Fakes()
    monkeypatch.setattr(main.textit, "start_flow", f.start_flow)
    monkeypatch.setattr(main.mailread, "read_tests", f.read_tests)
    monkeypatch.setattr(main.mailread, "clean", f.clean)
    monkeypatch.setattr(main.mailer, "send", f.send)
    monkeypatch.setattr(main, "_mailcheck", MailCheck(main._start_test, main._read_tests, main._clean_tests))
    main._memory["next_due"] = None
    return f


def configure(monkeypatch, **overrides):
    for name, value in {**SETTINGS, **overrides}.items():
        monkeypatch.setenv(name, value)


def quiet(now):
    return [{"triage_request_id": "A", "req_ts": now - timedelta(hours=3), "det_ts": now - timedelta(minutes=10)}]


def alerting(now):
    reqs = [Request(req_ts=now - timedelta(minutes=30 + i), det_ts=None) for i in range(3)]
    reqs.append(Request(req_ts=now - timedelta(hours=5), det_ts=now - timedelta(minutes=100)))
    return [{"triage_request_id": str(i), "req_ts": r.req_ts, "det_ts": r.det_ts} for i, r in enumerate(reqs)]


def lines(capsys):
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]


def events(logged, event):
    return [x for x in logged if x.get("event") == event]


# ---- off is today's behaviour, exactly --------------------------------------------------

def test_with_nothing_set_the_tick_behaves_as_before(svc, capsys):
    body, status = main.run_tick(NOON, reader=alerting)
    logged = lines(capsys)
    assert status == 200 and body["alerting"] is True and body["email_check"] == "off"
    assert svc.starts == [] and svc.reads == 0 and svc.sent == []
    notify = events(logged, "notify")
    assert len(notify) == 1 and notify[0]["kind"] == "alert"
    assert notify[0]["headline"].endswith("Email check: off.")
    own = events(logged, "own_email")
    assert len(own) == 1 and own[0]["outcome"] == "not_configured"
    assert events(logged, "mailcheck") == []


def test_switched_off_with_everything_else_set_starts_and_reads_nothing(svc, monkeypatch, capsys):
    configure(monkeypatch, EMAIL_CHECK="off")
    for i in range(13):
        main.run_tick(NOON + i * 5 * MIN, reader=quiet)
    assert svc.starts == [] and svc.reads == 0
    assert events(lines(capsys), "mailcheck") == []
    assert main.email_line(NOON) == "Email check: off."


def test_switched_on_with_a_setting_missing_says_so_and_does_nothing(svc, monkeypatch, capsys):
    configure(monkeypatch, TEXTIT_TOKEN="")
    main.run_tick(NOON, reader=quiet)
    logged = events(lines(capsys), "mailcheck")
    assert len(logged) == 1 and logged[0]["action"] == "not_configured"
    assert logged[0]["missing_settings"] == ["TEXTIT_TOKEN"]
    assert svc.starts == [] and main.email_line(NOON) == "Email check: not configured."


# ---- the hour, through the service ---------------------------------------------------------

def test_a_normal_hour_one_start_one_read_and_the_seconds_are_logged(svc, monkeypatch, capsys):
    configure(monkeypatch)
    main.run_tick(NOON + timedelta(seconds=1), reader=quiet)
    assert len(svc.starts) == 1
    start = svc.starts[0]
    assert start["token"] == TOKEN and start["flow"] == SETTINGS["TEXTIT_FLOW_UUID"]
    assert start["contact"] == SETTINGS["TEXTIT_CONTACT_UUID"]
    assert start["params"] == {"reference": reference(NOON), "sent_ct": "12:00 PM CT on Wed Jul 15"}
    svc.box.append(Arrival(reference(NOON), NOON + timedelta(seconds=48)))
    main.run_tick(NOON + 5 * MIN, reader=quiet)
    assert svc.reads == 0
    main.run_tick(NOON + 10 * MIN, reader=quiet)
    logged = lines(capsys)
    seen = [x for x in events(logged, "mailcheck") if x["action"] == "seen"]
    assert svc.reads == 1 and len(seen) == 1 and seen[0]["slowest_seconds"] == 47
    assert events(logged, "notify") == [] and svc.sent == []
    assert len(events(logged, "ran")) == 3
    assert main.email_line(NOON + 11 * MIN) == "Email check: test emails are arriving (last arrived 12:00 PM CT)."


def test_two_misses_send_the_not_arriving_email_and_then_the_all_clear(svc, monkeypatch, capsys):
    configure(monkeypatch)
    svc.box.append(Arrival(reference(NOON - 60 * MIN), NOON - 59 * MIN))
    for i in range(5):
        main.run_tick(NOON + i * 5 * MIN, reader=quiet)
    logged = lines(capsys)
    notify = events(logged, "notify")
    assert [n["kind"] for n in notify] == ["email_alert"]
    assert notify[0]["subject"] == "Early Alert: EMAIL NOT ARRIVING: TextIt test emails, none since 11:01 AM CT"
    assert len(svc.sent) == 1
    mail = svc.sent[0]
    assert mail["subject"] == "[Early Alert] EMAIL NOT ARRIVING: TextIt test emails, none since 11:01 AM CT"
    assert mail["to"] == ("first@example.com", "second@example.com")
    assert mail["from"] == SETTINGS["ALERT_FROM"] and mail["username"] == "sender@example.org"
    assert mail["password"] == PASSWORD  # spaces in the stored app password are removed
    own = events(logged, "own_email")
    assert own[0]["outcome"] == "sent" and own[0]["recipients"] == 2
    # next hour the test arrives
    nxt = NOON + 60 * MIN
    main.run_tick(nxt, reader=quiet)
    svc.box.append(Arrival(reference(nxt), nxt + timedelta(seconds=30)))
    main.run_tick(nxt + 10 * MIN, reader=quiet)
    assert [m["subject"] for m in svc.sent][1] == "[Early Alert] EMAIL ARRIVING AGAIN: TextIt test email at 1:00 PM CT"
    assert [n["kind"] for n in events(lines(capsys), "notify")] == ["email_ended"]


# ---- the triage message never waits on the email check --------------------------------------

def test_a_hanging_textit_and_a_hanging_mailbox_do_not_stop_the_triage_alert(svc, monkeypatch, capsys):
    configure(monkeypatch)
    svc.start_raises = TimeoutError("timed out")
    svc.read_raises = TimeoutError("timed out")
    body, status = main.run_tick(NOON + 5 * MIN, reader=alerting)
    logged = lines(capsys)
    assert status == 200 and body["alerting"] is True
    notify = events(logged, "notify")
    assert len(notify) == 1 and notify[0]["kind"] == "alert"
    assert notify[0]["headline"].endswith("Email check: result unavailable (the test mailbox could not be read).")
    assert len(svc.sent) == 1 and "Email check: result unavailable (the test mailbox could not be read)." in svc.sent[0]["body"]
    assert "TimeoutError" not in svc.sent[0]["body"] and "TimeoutError" not in notify[0]["headline"]
    order = [x["event"] for x in logged]
    assert order.index("ran") < order.index("notify") < order.index("own_email")
    assert order.index("own_email") < order.index("mailcheck")  # the email check runs after the triage message


def test_an_email_check_that_blows_up_does_not_change_the_ticks_answer(svc, monkeypatch, capsys):
    configure(monkeypatch)

    class Broken:
        def step(self, now):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "_mailcheck", Broken())
    body, status = main.run_tick(NOON, reader=quiet)
    logged = lines(capsys)
    assert status == 200 and len(events(logged, "ran")) == 1
    assert events(logged, "mailcheck")[0]["action"] == "error"


def test_the_triage_alert_carries_the_not_arriving_line(svc, monkeypatch, capsys):
    configure(monkeypatch)
    svc.box.append(Arrival(reference(NOON - 60 * MIN), NOON - 59 * MIN))
    for i in range(5):
        main.run_tick(NOON + i * 5 * MIN, reader=quiet)
    main._memory["next_due"] = None
    main.run_tick(NOON + 25 * MIN, reader=alerting)
    assert svc.sent[-1]["subject"].startswith("[Early Alert] TRIAGE SILENT:")
    assert ("Email check: test emails are NOT arriving (last arrived 11:01 AM CT; tests since then have not arrived)."
            in svc.sent[-1]["body"])


# ---- the service's own email ---------------------------------------------------------------

def test_own_email_failure_is_logged_after_the_notify_line(svc, monkeypatch, capsys):
    configure(monkeypatch)
    svc.send_results = [{"outcome": "failed", "attempts": 2, "reason": "SMTPAuthenticationError 535"}]
    body, status = main.run_tick(NOON + 5 * MIN, reader=alerting)
    logged = lines(capsys)
    assert status == 200 and len(events(logged, "notify")) == 1
    own = events(logged, "own_email")[0]
    assert own["outcome"] == "failed" and own["attempts"] == 2 and own["severity"] == "ERROR"
    assert own["reason"] == "SMTPAuthenticationError 535"


def test_own_email_that_raises_is_caught(svc, monkeypatch, capsys):
    configure(monkeypatch)

    def boom(*a, **k):
        raise OSError("network is unreachable")

    monkeypatch.setattr(main.mailer, "send", boom)
    body, status = main.run_tick(NOON + 5 * MIN, reader=alerting)
    own = events(lines(capsys), "own_email")[0]
    assert status == 200 and own["outcome"] == "failed" and own["reason"] == "OSError"


def test_no_recipients_means_no_own_email(svc, monkeypatch, capsys):
    configure(monkeypatch, ALERT_RECIPIENTS="")
    main.run_tick(NOON + 5 * MIN, reader=alerting)
    assert svc.sent == [] and events(lines(capsys), "own_email")[0]["outcome"] == "not_configured"


# ---- the test switch -------------------------------------------------------------------------

def test_switch_alert_then_ended_sends_two_test_emails_that_cannot_be_confused(svc, monkeypatch, capsys):
    configure(monkeypatch)
    main.run_tick(NOON + 2 * MIN, reader=quiet, test="alert")
    main.run_tick(NOON + 7 * MIN, reader=quiet, test="ended")
    subjects = [m["subject"] for m in svc.sent]
    assert subjects[0].startswith("[Early Alert] TEST - TRIAGE SILENT:")
    assert subjects[1].startswith("[Early Alert] TEST - TRIAGE RESUMED: determination at")
    assert svc.starts == []  # a triage test call does not run the email check
    notify = events(lines(capsys), "notify")
    assert [n["kind"] for n in notify] == ["alert", "ended"] and all(n["test"] for n in notify)


def test_switch_email_misses_then_the_next_tick_sends_the_all_clear(svc, monkeypatch, capsys):
    configure(monkeypatch)
    svc.box.append(Arrival(reference(NOON - 60 * MIN), NOON - 60 * MIN + timedelta(seconds=30)))
    main.run_tick(NOON, reader=quiet)
    svc.box.append(Arrival(reference(NOON), NOON + timedelta(seconds=40)))
    main.run_tick(NOON + 10 * MIN, reader=quiet)
    main.run_tick(NOON + 12 * MIN, reader=quiet, test="email_misses")
    main.run_tick(NOON + 15 * MIN, reader=quiet)
    subjects = [m["subject"] for m in svc.sent]
    assert subjects == ["[Early Alert] TEST - EMAIL NOT ARRIVING: TextIt test emails, none since 11:00 AM CT",
                        "[Early Alert] TEST - EMAIL ARRIVING AGAIN: TextIt test email at 12:00 PM CT"]
    bodies = [m["body"] for m in svc.sent]
    # every time in a test message is real: no invented send times
    assert "Two tests were sent" not in bodies[0] and "The last test that arrived: 11:00 AM CT." in bodies[0]
    assert "arrived at 12:00 PM CT" in bodies[1] and "None had arrived since 11:00 AM CT." in bodies[1]
    assert len(svc.starts) == 1


def test_switch_email_refused_logs_the_refusal_and_sends_nothing(svc, monkeypatch, capsys):
    configure(monkeypatch)
    svc.start_answer = (False, "HTTP 400: no such flow")
    main.run_tick(NOON + 12 * MIN, reader=quiet, test="email_refused")
    logged = lines(capsys)
    assert svc.starts[-1]["flow"] == main.NO_SUCH_FLOW
    refused = [x for x in events(logged, "mailcheck") if x["action"] == "could_not_run"]
    assert len(refused) == 1 and refused[0]["reason"] == "HTTP 400: no such flow" and refused[0]["test"] is True
    assert events(logged, "notify") == [] and svc.sent == []
    assert main.email_line(NOON + 13 * MIN).startswith("Email check: could not run at 12:10 PM CT")


def test_switch_email_tests_say_so_when_the_check_is_off(svc, monkeypatch, capsys):
    main.run_tick(NOON, reader=quiet, test="email_misses")
    logged = events(lines(capsys), "mailcheck")
    assert len(logged) == 1 and logged[0]["action"] == "off" and svc.sent == []


# ---- nothing leaks ------------------------------------------------------------------------------

def test_no_log_line_carries_a_credential_an_address_or_a_body(svc, monkeypatch, capsys):
    configure(monkeypatch)
    svc.box.append(Arrival(reference(NOON - 60 * MIN), NOON - 59 * MIN))
    # a refusal whose text echoes the token, a read error that echoes the password and addresses
    svc.start_answer = (False, f"HTTP 401: bad token {TOKEN}")
    main.run_tick(NOON, reader=quiet)
    svc.start_answer = (True, "201")
    svc.read_raises = RuntimeError(f"login failed for sender@example.org with {PASSWORD} to probe@example.org")
    main.run_tick(NOON + 5 * MIN, reader=quiet)
    main.run_tick(NOON + 15 * MIN, reader=quiet)
    svc.read_raises = None
    for i in range(4, 9):
        main.run_tick(NOON + i * 5 * MIN, reader=quiet)
    main._memory["next_due"] = None
    main.run_tick(NOON + 45 * MIN, reader=alerting)
    out = capsys.readouterr().out
    assert '"event": "notify"' in out and '"event": "own_email"' in out and "[withheld]" in out
    assert TOKEN not in out and PASSWORD not in out and "abcd efgh" not in out
    assert "@" not in out
    # the line about the service's own email says what happened to it, never what it said
    logged = [json.loads(x) for x in out.splitlines() if x.strip()]
    allowed = {"event", "severity", "kind", "test", "outcome", "attempts", "reason", "recipients"}
    own = events(logged, "own_email")
    assert own and all(set(x) <= allowed for x in own)
    assert all(isinstance(x["recipients"], int) for x in own)


def test_health_reports_the_switch_and_no_values(svc, monkeypatch):
    client = main.app.test_client()
    assert client.get("/health").get_json() == {
        "status": "ok", "service": "triage-monitor", "email_check": "off", "own_email": "not configured"}
    configure(monkeypatch)
    body = client.get("/health").get_json()
    assert body["email_check"] == "on" and body["own_email"] == "configured"
    assert TOKEN not in json.dumps(body) and "@" not in json.dumps(body)


def test_tick_endpoint_refuses_an_unknown_test(svc):
    client = main.app.test_client()
    r = client.post("/tick", json={"test": "everything"})
    assert r.status_code == 400 and "email_misses" in r.get_json()["error"]


def test_skipped_tick_line_is_on_the_grid(svc, capsys):
    main.run_tick(NOON, reader=quiet)
    body, status = main.run_tick(NOON + timedelta(minutes=5, seconds=42), reader=quiet)
    assert body["read"] is False and body["tick"] == (NOON + 5 * MIN).isoformat()


def test_a_broken_email_line_is_said_in_plain_words(svc, monkeypatch):
    configure(monkeypatch)

    class Broken:
        def latest(self, now):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "_mailcheck", Broken())
    assert main.email_line(NOON) == "Email check: result unavailable (the check could not be completed)."
