"""Edge tests for the triage-silence rule and the tick handler (evidence plan row 3)."""

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

import main  # noqa: E402
from rule import Request, compose, decide, in_window, measure, next_due, next_window_start, tick_floor  # noqa: E402

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc


def ct(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CT).astimezone(UTC)


def silent_with_waiting(at, n=3, last_det_minutes_ago=100):
    """n requests that reach 30 minutes of waiting exactly at `at` (so the episode starts at `at`,
    not on the tick before); last determination well before."""
    reqs = [Request(req_ts=at - timedelta(minutes=30 + i), det_ts=None) for i in range(n)]
    old = at - timedelta(hours=5)
    reqs.append(Request(req_ts=old, det_ts=at - timedelta(minutes=last_det_minutes_ago)))
    return reqs


# ---- window edges, standard time (December) and daylight time (July) -------

def test_window_edges_standard_time():
    assert not in_window(ct(2026, 12, 10, 10, 29))
    assert in_window(ct(2026, 12, 10, 10, 30))
    assert in_window(ct(2026, 12, 10, 19, 59))
    assert not in_window(ct(2026, 12, 10, 20, 0))
    # 10:30 AM CST is 16:30 UTC
    assert ct(2026, 12, 10, 10, 30) == datetime(2026, 12, 10, 16, 30, tzinfo=UTC)


def test_window_edges_daylight_time():
    assert not in_window(ct(2026, 7, 15, 10, 29))
    assert in_window(ct(2026, 7, 15, 10, 30))
    assert in_window(ct(2026, 7, 15, 19, 59))
    assert not in_window(ct(2026, 7, 15, 20, 0))
    # 10:30 AM CDT is 15:30 UTC
    assert ct(2026, 7, 15, 10, 30) == datetime(2026, 7, 15, 15, 30, tzinfo=UTC)


def test_rule_does_not_fire_at_1029_and_fires_at_1030():
    for day in ((2026, 12, 10), (2026, 7, 15)):
        before = ct(*day, 10, 25)
        reqs = silent_with_waiting(before)
        assert not decide(reqs, before).now.fires
        at = ct(*day, 10, 30)
        d = decide(reqs, at)
        assert d.now.fires and d.kind == "alert"


def test_rule_stops_at_2000_with_window_closed_reason():
    for day in ((2026, 12, 10), (2026, 7, 15)):
        at = ct(*day, 19, 55)
        reqs = silent_with_waiting(at)
        assert decide(reqs, at).now.fires
        d = decide(reqs, ct(*day, 20, 0))
        assert not d.now.fires and d.kind == "ended" and d.ended_reason == "window_closed"


def test_next_window_start_rolls_to_tomorrow_across_dst():
    # 8 PM CDT on the last daylight day -> 10:30 AM CST next day (an extra hour long night)
    t = ct(2026, 10, 31, 20, 0)
    nxt = next_window_start(t)
    assert nxt == ct(2026, 11, 1, 10, 30)
    assert nxt - t == timedelta(hours=15, minutes=30)


# ---- waiting age 29 / 30 minutes and the 14-hour cap ------------------------

def test_waiting_age_29_vs_30_minutes():
    at = ct(2026, 7, 15, 12, 0)
    reqs = [Request(req_ts=at - timedelta(minutes=29, seconds=59), det_ts=None)]
    assert measure(reqs, at).waiting == 0
    reqs = [Request(req_ts=at - timedelta(minutes=30), det_ts=None)]
    assert measure(reqs, at).waiting == 1


def test_waiting_ages_out_at_14_hours():
    at = ct(2026, 7, 15, 12, 0)
    reqs = [Request(req_ts=at - timedelta(hours=14), det_ts=None)]
    assert measure(reqs, at).waiting == 0  # exactly 14 h is out ("at most 14 hours" means older is out)
    reqs = [Request(req_ts=at - timedelta(hours=14) + timedelta(seconds=1), det_ts=None)]
    assert measure(reqs, at).waiting == 1


def test_determined_request_is_not_waiting_and_future_determination_is_as_of():
    at = ct(2026, 7, 15, 12, 0)
    r = Request(req_ts=at - timedelta(hours=1), det_ts=at - timedelta(minutes=10))
    assert measure([r], at).waiting == 0
    # as-of: a determination after t does not count at t
    assert measure([r], at - timedelta(minutes=20)).waiting == 1


def test_silence_90_minutes_edge():
    at = ct(2026, 7, 15, 12, 0)
    reqs = silent_with_waiting(at, last_det_minutes_ago=89)
    assert not decide(reqs, at).now.fires
    reqs = silent_with_waiting(at, last_det_minutes_ago=90)
    assert decide(reqs, at).now.fires


def test_fewer_than_three_waiting_does_not_fire():
    at = ct(2026, 7, 15, 12, 0)
    assert not decide(silent_with_waiting(at, n=2), at).now.fires
    assert decide(silent_with_waiting(at, n=3), at).now.fires


# ---- split rows counted once -----------------------------------------------

def test_split_rows_are_merged_by_request_id_with_min():
    at = ct(2026, 7, 15, 12, 0)
    rows = [
        {"triage_request_id": "A", "req_ts": at - timedelta(hours=1), "det_ts": None},
        {"triage_request_id": "A", "req_ts": at - timedelta(hours=1), "det_ts": at - timedelta(minutes=50)},
        {"triage_request_id": "B", "req_ts": at - timedelta(minutes=40), "det_ts": None},
    ]
    reqs = main.rows_to_requests(rows)
    assert len(reqs) == 2
    m = measure(reqs, at)
    assert m.waiting == 1  # A is determined; B waits
    assert m.last_det == at - timedelta(minutes=50)


# ---- episode logic: start, repeat at 2 h, ended by determination -----------

def test_episode_repeat_every_two_hours_and_ended_by_determination():
    start = ct(2026, 7, 15, 12, 0)
    reqs = silent_with_waiting(start)
    assert decide(reqs, start).kind == "alert"
    assert decide(reqs, start + timedelta(minutes=5)).kind is None
    assert decide(reqs, start + timedelta(hours=2)).kind == "repeat"
    assert decide(reqs, start + timedelta(hours=2, minutes=5)).kind is None
    assert decide(reqs, start + timedelta(hours=4)).kind == "repeat"
    # a determination lands at +4h03 -> next tick ends the episode
    landed = start + timedelta(hours=4, minutes=3)
    reqs2 = reqs + [Request(req_ts=start - timedelta(hours=2), det_ts=landed)]
    d = decide(reqs2, start + timedelta(hours=4, minutes=5))
    assert d.kind == "ended" and d.ended_reason == "determination"
    msg = compose(d)
    assert "triage resumed" in msg["subject"] and "4:03 PM" in msg["last_determination"]


def test_tick_floor_and_jitter():
    t = datetime(2026, 7, 15, 17, 7, 42, 123, tzinfo=UTC)
    assert tick_floor(t) == datetime(2026, 7, 15, 17, 5, tzinfo=UTC)


# ---- next-read-due is never late -------------------------------------------

def test_next_due_is_after_last_det_plus_90_and_after_third_waiting():
    at = ct(2026, 7, 15, 12, 0)
    last = at - timedelta(minutes=10)
    reqs = [Request(req_ts=at - timedelta(hours=3), det_ts=last)]
    m = measure(reqs, at)
    assert next_due(reqs, at, m) == last + timedelta(minutes=90)
    # three fresh undetermined requests: the third reaches 30 min at +30 min, but A (+80) governs
    reqs += [Request(req_ts=at - timedelta(minutes=i), det_ts=None) for i in (1, 2, 3)]
    m = measure(reqs, at)
    assert next_due(reqs, at, m) == last + timedelta(minutes=90)
    # a stale determination (3 h ago): B governs -> the third undetermined + 30 min
    reqs = [Request(req_ts=at - timedelta(hours=5), det_ts=at - timedelta(hours=3))]
    reqs += [Request(req_ts=at - timedelta(minutes=i), det_ts=None) for i in (1, 2, 3)]
    m = measure(reqs, at)
    assert next_due(reqs, at, m) == (at - timedelta(minutes=1)) + timedelta(minutes=30)


def test_next_due_outside_window_is_next_window_start():
    at = ct(2026, 7, 15, 21, 0)
    reqs = silent_with_waiting(at)
    m = measure(reqs, at)
    assert next_due(reqs, at, m) == ct(2026, 7, 16, 10, 30)


def test_next_due_while_alerting_is_next_tick():
    at = ct(2026, 7, 15, 12, 0)
    reqs = silent_with_waiting(at)
    d = decide(reqs, at)
    assert d.now.fires and d.next_due == at + timedelta(minutes=5)


# ---- the tick handler: no-memory reads, memory skips, BigQuery error -------

def _reset_memory():
    main._memory["next_due"] = None


def test_no_memory_path_reads_and_memory_path_skips(capsys):
    _reset_memory()
    at = ct(2026, 7, 15, 12, 0)
    calls = []

    def reader(now):
        calls.append(now)
        last = now - timedelta(minutes=10)
        return [{"triage_request_id": "A", "req_ts": now - timedelta(hours=3), "det_ts": last}]

    body, status = main.run_tick(at, reader=reader)
    assert status == 200 and body["read"] is True and body["reason"] == "no_memory"
    assert len(calls) == 1
    body, status = main.run_tick(at + timedelta(minutes=5), reader=reader)
    assert status == 200 and body["read"] is False and body["reason"] == "not_due"
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert out.count('"event": "ran"') == 2


def test_bigquery_error_logs_error_line_and_returns_500(capsys):
    _reset_memory()
    at = ct(2026, 7, 15, 12, 0)

    def reader(now):
        raise RuntimeError("403 Access Denied: BigQuery")

    body, status = main.run_tick(at, reader=reader)
    assert status == 500 and body["event"] == "error" and "403" in body["error"]
    out = capsys.readouterr().out
    assert '"severity": "ERROR"' in out


def test_alert_tick_writes_notify_line(capsys):
    _reset_memory()
    at = ct(2026, 7, 15, 12, 0)

    def reader(now):
        reqs = silent_with_waiting(now)
        return [{"triage_request_id": str(i), "req_ts": r.req_ts, "det_ts": r.det_ts} for i, r in enumerate(reqs)]

    body, status = main.run_tick(at, reader=reader)
    assert status == 200 and body["alerting"] is True and body["kind"] == "alert"
    out = capsys.readouterr().out
    assert '"event": "notify"' in out and '"kind": "alert"' in out and "3 requests waiting" in out


def test_test_switch_writes_alert_then_ended_one_tick_apart(capsys):
    _reset_memory()
    at = ct(2026, 7, 15, 12, 0)

    def reader(now):
        last = now - timedelta(minutes=10)
        return [{"triage_request_id": "A", "req_ts": now - timedelta(hours=3), "det_ts": last}]

    body, status = main.run_tick(at, reader=reader, test="alert")
    assert status == 200 and body["test"] == "alert"
    out = capsys.readouterr().out
    assert out.count('"event": "notify"') == 1 and '"kind": "alert"' in out and "TEST" in out
    body, status = main.run_tick(at + timedelta(minutes=5), reader=reader, test="ended")
    assert status == 200 and body["test"] == "ended"
    out = capsys.readouterr().out
    assert out.count('"event": "notify"') == 1 and '"kind": "ended"' in out and "TEST" in out


def test_window_closed_message_says_still_silent_and_hours_over():
    at = ct(2026, 7, 15, 19, 55)
    reqs = silent_with_waiting(at)
    d = decide(reqs, ct(2026, 7, 15, 20, 0))
    msg = compose(d)
    assert msg["subject"].startswith("Early Alert: triage STILL silent")
    assert msg["headline"].startswith("Triage is STILL silent")
    assert "hours are over" in msg["headline"] and "10:30 AM" in msg["headline"]
    assert "resolved" not in msg["subject"] and "resumed" not in msg["headline"]
