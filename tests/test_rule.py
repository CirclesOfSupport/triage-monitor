"""Edge tests for the triage-silence rule and the tick handler (evidence plan row 3)."""

import random
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

import main  # noqa: E402
from rule import TICK, Request, compose, decide, in_window, measure, next_due, next_window_start, tick_floor  # noqa: E402

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


# ---- Amendment 2 (2026-10-14): waiting = came in after the last determination --

def todays_shape():
    """2026-10-14, the first live false alarm: the last determination at 8:48:44 AM CT, and the
    three undetermined requests all from before it (10-13 8:41:37 PM and 8:45:45 PM, 10-14
    4:33:33 AM); nothing came in after 8:47:15 AM."""
    det = datetime(2026, 10, 14, 8, 48, 44, tzinfo=CT).astimezone(UTC)
    return [
        Request(req_ts=datetime(2026, 10, 13, 20, 41, 37, tzinfo=CT).astimezone(UTC), det_ts=None),
        Request(req_ts=datetime(2026, 10, 13, 20, 45, 45, tzinfo=CT).astimezone(UTC), det_ts=None),
        Request(req_ts=datetime(2026, 10, 14, 4, 33, 33, tzinfo=CT).astimezone(UTC), det_ts=None),
        Request(req_ts=datetime(2026, 10, 14, 8, 47, 15, tzinfo=CT).astimezone(UTC), det_ts=det),
    ]


def test_todays_shape_three_older_than_the_last_determination_never_alerts():
    reqs = todays_shape()
    for minutes in range(0, 120, 5):  # 10:30 AM to 12:25 PM
        at = ct(2026, 10, 14, 10, 30) + timedelta(minutes=minutes)
        d = decide(reqs, at)
        assert d.now.waiting == 0 and d.kind is None and not d.now.fires, at
    m = measure(reqs, ct(2026, 10, 14, 10, 30))
    assert m.silence_minutes > 90 and m.in_window  # the silence and the window held; the count did not


def test_three_that_came_in_after_the_last_determination_alert():
    reqs = todays_shape()
    det = reqs[-1].det_ts
    reqs += [Request(req_ts=det + timedelta(minutes=1 + i), det_ts=None) for i in range(3)]
    d = decide(reqs, ct(2026, 10, 14, 10, 30))
    assert d.now.waiting == 3 and d.kind == "alert"


def test_two_older_and_two_newer_do_not_alert_and_a_third_newer_does():
    at = ct(2026, 7, 15, 12, 0)
    det = at - timedelta(minutes=100)
    older = [Request(req_ts=det - timedelta(minutes=10 + i), det_ts=None) for i in range(2)]
    newer = [Request(req_ts=det + timedelta(minutes=5 + i), det_ts=None) for i in range(2)]
    reqs = older + newer + [Request(req_ts=det - timedelta(hours=1), det_ts=det)]
    m = measure(reqs, at)
    assert m.waiting == 2 and not m.fires
    reqs.append(Request(req_ts=det + timedelta(minutes=20), det_ts=None))
    m = measure(reqs, at)
    assert m.waiting == 3 and m.fires


def test_a_request_at_the_same_second_as_the_last_determination_does_not_count():
    at = ct(2026, 7, 15, 12, 0)
    det = at - timedelta(hours=2)
    landed = Request(req_ts=det - timedelta(minutes=5), det_ts=det)
    assert measure([landed, Request(req_ts=det, det_ts=None)], at).waiting == 0
    assert measure([landed, Request(req_ts=det + timedelta(seconds=1), det_ts=None)], at).waiting == 1


def test_with_no_determination_in_the_lookback_every_request_counts():
    at = ct(2026, 7, 15, 12, 0)
    reqs = [Request(req_ts=at - timedelta(hours=h), det_ts=None) for h in (1, 5, 13)]
    m = measure(reqs, at)
    assert m.last_det is None and m.waiting == 3 and m.fires


def test_a_determination_after_t_does_not_move_the_line_at_t():
    # as-of: a determination that lands later is invisible at t, so it excludes nothing at t
    at = ct(2026, 7, 15, 12, 0)
    old = Request(req_ts=at - timedelta(hours=6), det_ts=at - timedelta(hours=4))
    waiting = [Request(req_ts=at - timedelta(hours=3, minutes=i), det_ts=None) for i in range(3)]
    later = Request(req_ts=at - timedelta(hours=1), det_ts=at + timedelta(minutes=10))
    m = measure([old, later] + waiting, at)
    assert m.last_det == old.det_ts and m.waiting == 4 and m.fires  # `later` itself still waits at t
    m = measure([old, later] + waiting, at + timedelta(minutes=10))
    assert m.waiting == 0 and not m.fires  # once it lands, the three came in before it


def test_14_hour_edge_after_a_determination_15_hours_ago():
    at = ct(2026, 7, 15, 12, 0)
    det = Request(req_ts=at - timedelta(hours=16), det_ts=at - timedelta(hours=15))
    assert measure([det, Request(req_ts=at - timedelta(hours=14), det_ts=None)], at).waiting == 0
    assert measure([det, Request(req_ts=at - timedelta(hours=14) + timedelta(seconds=1), det_ts=None)], at).waiting == 1


def test_episode_start_waits_for_the_third_request_after_the_last_determination():
    # silence is over 90 minutes from the window's start, three leftovers wait from before the
    # last determination, and three new requests reach 30 minutes at 11:15: the episode starts
    # at 11:15, not at 10:30
    day = (2026, 7, 15)
    det = ct(*day, 8, 0)
    reqs = [Request(req_ts=det - timedelta(minutes=1), det_ts=det)]
    reqs += [Request(req_ts=det - timedelta(hours=2, minutes=i), det_ts=None) for i in range(3)]
    reqs += [Request(req_ts=ct(*day, 10, 45) - timedelta(minutes=i), det_ts=None) for i in range(3)]
    assert decide(reqs, ct(*day, 10, 30)).kind is None
    assert not measure(reqs, ct(*day, 11, 10)).fires
    d = decide(reqs, ct(*day, 11, 15))
    assert d.kind == "alert" and d.episode_start == ct(*day, 11, 15) and d.now.waiting == 3
    later = decide(reqs, ct(*day, 12, 0))
    assert later.kind is None and later.episode_start == ct(*day, 11, 15)


def test_after_a_determination_ends_an_episode_the_count_is_always_zero():
    start = ct(2026, 7, 15, 12, 0)
    reqs = silent_with_waiting(start, n=5)
    assert decide(reqs, start).kind == "alert"
    reqs = reqs + [Request(req_ts=start - timedelta(hours=1), det_ts=start + timedelta(minutes=3))]
    d = decide(reqs, start + timedelta(minutes=5))
    assert d.kind == "ended" and d.ended_reason == "determination" and d.now.waiting == 0
    # so Google's copy of "resumed" gives no count (it would always say 0 still waiting)
    msg = compose(d)
    assert msg["headline"] == "A triage determination landed at 12:03 PM CT on Wed Jul 15."


def test_the_waiting_fell_line_says_the_requests_came_in_after_the_last_determination():
    # requests from before the last determination may still be waiting; the old line
    # ("Fewer than 3 requests are now waiting") would be false then
    at = ct(2026, 7, 15, 12, 0)
    reqs = silent_with_waiting(at)
    d = decide(reqs, at)
    msg = compose(replace(d, kind="ended", ended_reason="waiting_fell"))
    assert msg["headline"].startswith("Fewer than 3 requests that came in after the last determination are now waiting")


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


def test_next_due_counts_requests_from_before_the_last_determination_so_it_can_only_be_early():
    # Amendment 2 leaves B unfiltered (the record says why): with silence past 90 minutes and
    # three leftovers from before the last determination, B is already past and the next read
    # is the next tick, although the waiting count is 0. Early, never late.
    at = ct(2026, 7, 15, 12, 0)
    det = at - timedelta(minutes=100)
    reqs = [Request(req_ts=det - timedelta(minutes=1), det_ts=det)]
    reqs += [Request(req_ts=det - timedelta(hours=1, minutes=i), det_ts=None) for i in range(3)]
    m = measure(reqs, at)
    assert m.waiting == 0 and not m.fires
    assert next_due(reqs, at, m) == at + timedelta(minutes=5)


def test_next_due_is_never_late_with_requests_before_and_after_the_last_determination():
    # Random histories around one tick: determined, undetermined, determined later, and new
    # requests arriving after the tick. From the tick to the moment next_due names, the rule
    # must not hold on any tick (the tick sees only the requests that had come in by then).
    rng = random.Random(20261014)
    base = ct(2026, 10, 14, 10, 30)
    checked = 0
    for _ in range(400):
        reqs = []
        for _ in range(rng.randint(0, 4)):
            rq = base - timedelta(minutes=rng.randint(60, 1200))
            reqs.append(Request(req_ts=rq, det_ts=rq + timedelta(minutes=rng.randint(1, 700))))
        for _ in range(rng.randint(0, 9)):
            rq = base + timedelta(minutes=rng.randint(-900, 400))
            det = None if rng.random() < 0.6 else rq + timedelta(minutes=rng.randint(1, 400))
            reqs.append(Request(req_ts=rq, det_ts=det))
        tick = base + TICK * rng.randint(0, 100)
        seen = [r for r in reqs if r.req_ts <= tick]
        m = measure(seen, tick)
        due = next_due(seen, tick, m)
        t = tick + TICK
        while t < due:
            assert not measure([r for r in reqs if r.req_ts <= t], t).fires, (tick, due, t)
            t += TICK
            checked += 1
    assert checked > 1000


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
    assert '"event": "notify"' in out and '"kind": "alert"' in out and "3 new requests waiting" in out


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
