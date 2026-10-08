"""The words: every subject and body, and the line a triage alert carries about the email check."""

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

import messages  # noqa: E402
from mailcheck import Message, Result  # noqa: E402
from rule import Request, compose, decide  # noqa: E402

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc
MIN = timedelta(minutes=1)


def ct(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CT).astimezone(UTC)


NOW = ct(2026, 10, 7, 14, 0)
LINE = "Email check: test emails are arriving (last arrived 1:02 PM CT)."


def silent(at, n=4, last_det_minutes_ago=93):
    reqs = [Request(req_ts=at - timedelta(minutes=30 + i), det_ts=None) for i in range(n)]
    reqs.append(Request(req_ts=at - timedelta(hours=5), det_ts=at - timedelta(minutes=last_det_minutes_ago)))
    return reqs


def every_message():
    """One of each kind: name -> (subject, body)."""
    d_alert = decide(silent(NOW), NOW)
    d_repeat = replace(d_alert, kind="repeat")
    d_resumed = replace(d_alert, kind="ended", ended_reason="determination")
    d_window = replace(d_alert, kind="ended", ended_reason="window_closed")
    d_fell = replace(d_alert, kind="ended", ended_reason="waiting_fell")
    since = ct(2026, 10, 7, 13, 2)
    sent = (ct(2026, 10, 7, 14, 0), ct(2026, 10, 7, 14, 10))
    return {
        "alert": messages.triage(d_alert, LINE, NOW),
        "repeat": messages.triage(d_repeat, LINE, NOW),
        "resumed": messages.triage(d_resumed, None, NOW),
        "window_closed": messages.triage(d_window, None, NOW),
        "waiting_fell": messages.triage(d_fell, None, NOW),
        "email_alert": messages.email_check(Message("email_alert", NOW + 20 * MIN, since, sent), NOW + 20 * MIN),
        "email_repeat": messages.email_check(Message("email_repeat", NOW + 140 * MIN, since, sent), NOW + 140 * MIN),
        "email_ended": messages.email_check(
            Message("email_ended", NOW + 190 * MIN, since, arrived=ct(2026, 10, 7, 17, 2)), NOW + 190 * MIN),
    }


def state_word(subject):
    """The capitals a subject opens with, after the tag: 'TRIAGE SILENT', 'EMAIL ARRIVING AGAIN', ..."""
    assert subject.startswith(messages.PREFIX)
    return subject[len(messages.PREFIX):].split(":")[0]


def test_every_subject_opens_with_its_own_state():
    words = {name: state_word(subject) for name, (subject, _) in every_message().items()}
    assert words == {
        "alert": "TRIAGE SILENT", "repeat": "TRIAGE STILL SILENT", "resumed": "TRIAGE RESUMED",
        "window_closed": "TRIAGE STILL SILENT", "waiting_fell": "TRIAGE NOT RESOLVED",
        "email_alert": "EMAIL NOT ARRIVING", "email_repeat": "EMAIL STILL NOT ARRIVING", "email_ended": "EMAIL ARRIVING AGAIN",
    }


def test_a_closing_message_shares_no_opening_word_with_the_alert_it_closes():
    m = every_message()
    # the first word names what the message is about; the word after it is the state
    first = lambda name: state_word(m[name][0]).split()[1]  # noqa: E731
    assert {state_word(m[n][0]).split()[0] for n in m} == {"TRIAGE", "EMAIL"}
    assert first("resumed") not in (first("alert"), first("repeat"))
    assert first("waiting_fell") not in (first("alert"), first("repeat"))
    assert m["waiting_fell"][0] == "[Early Alert] TRIAGE NOT RESOLVED: alert ended, fewer than 3 requests waiting"
    assert m["waiting_fell"][1].startswith("Triage is NOT resolved.")
    assert first("email_ended") not in (first("email_alert"), first("email_repeat"))
    # the 8 PM message is not a closing message: it must read as still on
    assert first("window_closed") == "STILL" and "NOT resolved" in m["window_closed"][1]


def test_every_message_is_plain_ascii_and_says_what_it_checks():
    for name, (subject, body) in every_message().items():
        subject.encode("ascii")
        body.encode("ascii")
        assert "What this checks:" in body, name
        # plain words for people: no table, no column, no error class
        for word in ("BigQuery", "RESPONSES", "triage-message-data", "determination_time", "Error", "Exception"):
            assert word not in subject and word not in body, (name, word)
        assert len(subject) <= 100, (name, len(subject))


def test_the_alert_and_the_reminder_carry_the_email_line_and_the_numbers():
    m = every_message()
    for name in ("alert", "repeat"):
        subject, body = m[name]
        assert LINE in body and "1 h 33 min" in subject and "4 requests waiting" in subject
        assert "12:27 PM CT" in body
    for name in ("resumed", "window_closed", "waiting_fell"):
        assert "Email check" not in m[name][1]


def test_email_messages_name_the_last_arrival_and_the_two_tests():
    m = every_message()
    assert m["email_alert"][0] == "[Early Alert] EMAIL NOT ARRIVING: TextIt test emails, none since 1:02 PM CT"
    assert "Two tests were sent (2:00 PM CT and 2:10 PM CT) and neither reached the mailbox." in m["email_alert"][1]
    assert m["email_repeat"][0] == "[Early Alert] EMAIL STILL NOT ARRIVING: TextIt test emails, none since 1:02 PM CT"
    assert m["email_ended"][0] == "[Early Alert] EMAIL ARRIVING AGAIN: TextIt test email at 5:02 PM CT"
    assert "None had arrived since 1:02 PM CT." in m["email_ended"][1]


def test_no_arrival_on_record_is_said_plainly():
    subject, body = messages.email_check(Message("email_alert", NOW, None, (NOW, NOW + 10 * MIN)), NOW)
    assert subject.endswith("none in the last 2 days") and "No test has arrived in the last 2 days." in body


def test_a_time_on_another_day_carries_the_day():
    since = ct(2026, 10, 6, 23, 2)
    subject, _ = messages.email_check(Message("email_alert", NOW, since, ()), NOW)
    assert subject.endswith("none since 11:02 PM CT on Tue Oct 6")


def test_test_versions_are_marked_in_the_subject_and_the_first_line():
    d = decide(silent(NOW), NOW)
    subject, body = messages.triage(d, LINE, NOW, test=True)
    assert subject.startswith("[Early Alert] TEST - TRIAGE SILENT:") and body.startswith("THIS IS A TEST")
    subject, body = messages.email_check(Message("email_alert", NOW, None, (), test=True), NOW)
    assert subject.startswith("[Early Alert] TEST - EMAIL NOT ARRIVING:") and body.startswith("THIS IS A TEST")


def test_one_request_is_singular():
    d = decide(silent(NOW), NOW)
    d = replace(d, kind="ended", ended_reason="determination", now=replace(d.now, waiting=1))
    assert "1 request still waiting" in messages.triage(d, None, NOW)[1]


# ---- the line ---------------------------------------------------------------------------

def test_the_email_line_every_wording():
    last = ct(2026, 10, 7, 13, 2)
    at = ct(2026, 10, 7, 14, 0)
    line = messages.email_line
    assert line("off", None, None, "", NOW) == "Email check: off."
    assert line("not_configured", None, None, "", NOW) == "Email check: not configured."
    assert line("on", None, None, "", NOW) == "Email check: no result yet."
    assert line("on", None, last, "", NOW) == (
        "Email check: no result yet since the monitor restarted (last test email arrived 1:02 PM CT).")
    assert line("on", None, None, "unavailable (the test mailbox could not be read)", NOW) == (
        "Email check: result unavailable (the test mailbox could not be read).")
    assert line("on", Result("arriving", at), last, "", NOW) == LINE
    assert line("on", Result("not_arriving", at), last, "", NOW) == (
        "Email check: test emails are NOT arriving (last arrived 1:02 PM CT; tests since then have not arrived).")
    assert line("on", Result("not_arriving", at), None, "", NOW) == (
        "Email check: test emails are NOT arriving (none has arrived in the last 2 days).")
    assert line("on", Result("could_not_run", at, "HTTP 500"), last, "", NOW) == (
        "Email check: could not run at 2:00 PM CT (TextIt did not accept the test); last test arrived 1:02 PM CT.")


def test_compose_adds_the_line_to_alert_and_reminder_only():
    d = decide(silent(NOW), NOW)
    assert compose(d, email_line=LINE)["headline"].endswith(LINE)
    assert compose(replace(d, kind="repeat"), email_line=LINE)["headline"].endswith(LINE)
    ended = replace(d, kind="ended", ended_reason="determination")
    assert "Email check" not in compose(ended, email_line=LINE)["headline"]
    assert compose(d)["headline"] == compose(d, email_line=None)["headline"]


def test_the_carried_copy_of_an_email_message_has_every_label_the_policy_extracts():
    m = Message("email_alert", NOW, ct(2026, 10, 7, 13, 2), (NOW, NOW + 10 * MIN))
    subject, body = messages.email_check(m, NOW)
    carried = messages.email_check_google(m, subject, body)
    assert set(carried) == {"kind", "subject", "headline", "last_determination", "waiting", "checks"}
    assert carried["kind"] == "email_alert" and carried["subject"].startswith("Early Alert: EMAIL NOT ARRIVING")
    assert "has not arrived" in carried["headline"] and "\n" not in carried["headline"]
    assert all(isinstance(v, str) and v for v in carried.values())
