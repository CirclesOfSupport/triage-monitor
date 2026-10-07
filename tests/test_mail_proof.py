"""The hand-run mail proof, with the send and the read faked."""

import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")
sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/tools")

import mail_proof  # noqa: E402
from mailcheck import Arrival  # noqa: E402

SECRET = "abcdefghijklmnop"


def setup(monkeypatch, send_result, arrive_after_reads=1, read_error=None):
    state = {"reads": 0, "sent": []}

    def send(username, password, sender, to, subject, body, **kw):
        state["sent"].append((username, password, sender, tuple(to), subject))
        return send_result

    def read_tests(username, password, address, now, **kw):
        state["reads"] += 1
        if read_error:
            raise read_error
        if arrive_after_reads and state["reads"] >= arrive_after_reads and state["sent"]:
            ref = state["sent"][-1][4].split()[-1]
            return [Arrival(ref, datetime.now(timezone.utc) + timedelta(seconds=3))]
        return []

    monkeypatch.setattr(mail_proof.mailer, "send", send)
    monkeypatch.setattr(mail_proof.mailread, "read_tests", read_tests)
    monkeypatch.setattr(mail_proof, "gmail_labels", lambda *a: "\\Inbox")
    return state


def go(**kw):
    lines = []
    code = mail_proof.run("sender@example.org", "Monitor <alerts@example.net>", "probe@example.org", SECRET,
                          sleep=lambda s: None, out=lines.append, **kw)
    return code, lines


def test_sent_and_read_back_passes(monkeypatch):
    state = setup(monkeypatch, {"outcome": "sent", "attempts": 1, "reason": ""}, arrive_after_reads=2)
    code, lines = go()
    assert code == 0 and state["reads"] == 2
    assert state["sent"][0][3] == ("probe@example.org",) and state["sent"][0][4].startswith("Early Alert email test EA-MC-")
    assert lines[-2].startswith("RESULT: PASS") and lines[-1] == mail_proof.END
    assert any("FOUND" in x for x in lines) and any("\\Inbox" in x for x in lines)
    assert not any(SECRET in x for x in lines)


def test_a_refused_send_fails_in_plain_words_and_reads_nothing(monkeypatch):
    state = setup(monkeypatch, {"outcome": "failed", "attempts": 2, "reason": "SMTPAuthenticationError 535"})
    code, lines = go()
    assert code == 2 and state["reads"] == 0
    assert "the relay refused the sign-in" in lines[1] and lines[-2] == "RESULT: FAIL - nothing was sent"


def test_never_arriving_fails_after_the_wait(monkeypatch):
    state = setup(monkeypatch, {"outcome": "sent", "attempts": 1, "reason": ""}, arrive_after_reads=0)
    code, lines = go(wait_seconds=30, pause=10)
    assert code == 4 and state["reads"] == 4
    assert lines[-2] == "RESULT: FAIL - the email was sent but did not appear in the mailbox"


def test_an_unreadable_mailbox_fails_and_says_why(monkeypatch):
    setup(monkeypatch, {"outcome": "sent", "attempts": 1, "reason": ""}, read_error=RuntimeError("the mailbox sign-in was refused"))
    code, lines = go()
    assert code == 3 and "READ FAILED: RuntimeError: the mailbox sign-in was refused" in lines[-3]
    assert lines[-1] == mail_proof.END


def test_flow_mode_starts_the_flow_with_the_reference_and_reads_it_back(monkeypatch):
    state = setup(monkeypatch, {"outcome": "sent", "attempts": 1, "reason": ""}, arrive_after_reads=0)
    starts = []

    def start_flow(token, flow, contact, params, **kw):
        starts.append((token, flow, contact, params))
        state["sent"].append(("", "", "", (), "Early Alert email test " + params["reference"]))
        return True, "201"

    monkeypatch.setattr(mail_proof.textit, "start_flow", start_flow)
    monkeypatch.setattr(mail_proof.mailread, "read_tests", lambda u, p, a, now, **kw: [
        Arrival(starts[-1][3]["reference"], datetime.now(timezone.utc) + timedelta(seconds=40))])
    code, lines = go(flow="11111111-1111-1111-1111-111111111111", contact="22222222-2222-2222-2222-222222222222",
                     token="tok123")
    assert code == 0 and len(starts) == 1
    token, flow, contact, params = starts[0]
    assert (token, flow, contact) == ("tok123", "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")
    assert params["reference"].startswith("EA-MC-")
    assert re.fullmatch(r"\d{1,2}:\d{2} (AM|PM) CT on \w{3} \w{3} \d{1,2}", params["sent_ct"]), params["sent_ct"]
    assert any("after TextIt was asked" in x for x in lines) and lines[-2].startswith("RESULT: PASS - TextIt")
    assert not any("tok123" in x for x in lines)


def test_flow_mode_refused_start_fails_and_reads_nothing(monkeypatch):
    state = setup(monkeypatch, {"outcome": "sent", "attempts": 1, "reason": ""})
    monkeypatch.setattr(mail_proof.textit, "start_flow", lambda *a, **k: (False, "HTTP 400: no such flow"))
    code, lines = go(flow="f", contact="c", token="t")
    assert code == 2 and state["reads"] == 0 and "HTTP 400" in lines[1]


def test_arrival_seconds_are_never_negative(monkeypatch):
    state = setup(monkeypatch, {"outcome": "sent", "attempts": 1, "reason": ""})
    monkeypatch.setattr(mail_proof.mailread, "read_tests", lambda u, p, a, now, **kw: [
        Arrival(state["sent"][-1][4].split()[-1], now - timedelta(seconds=5))])
    code, lines = go()
    found = [x for x in lines if "FOUND" in x][0]
    assert code == 0 and " 0 s after the send began" in found
