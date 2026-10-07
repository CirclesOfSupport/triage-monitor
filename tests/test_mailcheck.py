"""The hourly email check, with TextIt and the mailbox faked. Nothing here touches the network."""

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

from mailcheck import Arrival, MailCheck, hour_of, reference, reference_time  # noqa: E402

UTC = timezone.utc
MIN = timedelta(minutes=1)
H0 = datetime(2026, 10, 7, 21, 0, tzinfo=UTC)  # 4:00 PM Central
ACCEPT = (True, "201")


class World:
    """A fake TextIt and a fake mailbox. `latency` is how long a test takes to arrive
    (None: never); `answers` are TextIt's replies to successive starts (default: accepted)."""

    def __init__(self, latency=timedelta(seconds=40), answers=None):
        self.latency = latency
        self.answers = list(answers or [])
        self.now = None
        self.started = []
        self.box = []
        self.reads = 0
        self.read_error = None
        self.cleans = 0

    def start(self, ref, tick):
        ok, reason = self.answers.pop(0) if self.answers else ACCEPT
        if ok:
            self.started.append(ref)
            if self.latency is not None:
                self.box.append(Arrival(ref, self.now + self.latency))
        return ok, reason

    def read(self, now):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return [a for a in self.box if a.arrived <= now]

    def clean(self, now):
        self.cleans += 1
        return 0


def make(world):
    return MailCheck(world.start, world.read, world.clean)


def run(mc, world, start, end, jitter=timedelta(seconds=1)):
    """Tick every 5 minutes from start to end inclusive. Returns (logs, messages)."""
    logs, msgs = [], []
    t = start
    while t <= end:
        world.now = t + jitter
        lg, ms = mc.step(world.now)
        logs += lg
        msgs += ms
        t += 5 * MIN
    return logs, msgs


def actions(logs):
    return [entry["action"] for entry in logs]


# ---- the normal hour ----------------------------------------------------------

def test_normal_hour_one_start_one_read_arriving_and_seconds_recorded():
    w = World(latency=timedelta(seconds=40))
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
    assert w.started == [reference(H0)] and w.reads == 1 and msgs == []
    assert actions(logs) == ["started", "seen"]
    seen = logs[1]
    assert seen["slowest_seconds"] == 40 and seen["tests"][0]["exact"] is True
    assert mc.verdict == "arriving" and mc.result.state == "arriving"
    assert w.cleans == 1


def test_look_happens_ten_minutes_after_the_start_not_before():
    w = World()
    mc = make(w)
    run(mc, w, H0, H0 + 5 * MIN)
    assert w.reads == 0
    run(mc, w, H0 + 10 * MIN, H0 + 10 * MIN)
    assert w.reads == 1


# ---- missing once, missing twice ------------------------------------------------

def test_missing_once_starts_a_retry_and_does_not_alert():
    w = World(latency=None)
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 10 * MIN)
    assert actions(logs) == ["started", "missing", "started"] and msgs == []
    assert w.started == [reference(H0), reference(H0 + 10 * MIN)]
    assert mc.verdict is None


def test_missing_twice_is_not_arriving_and_alerts_once():
    w = World(latency=None)
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
    assert [m.kind for m in msgs] == ["email_alert"]
    assert msgs[0].at == H0 + 20 * MIN and len(msgs[0].sent) == 2 and msgs[0].since is None
    assert actions(logs) == ["started", "missing", "started", "not_arriving"]
    assert w.reads == 2 and len(w.started) == 2  # nothing more is sent or read after the verdict


def test_one_late_email_never_alerts():
    # the first test takes 14 minutes: missed at :10, there at :20
    w = World(latency=timedelta(minutes=14))
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
    assert msgs == [] and mc.verdict == "arriving"
    seen = [entry for entry in logs if entry["action"] == "seen"][0]
    assert seen["slowest_seconds"] == 14 * 60 and seen["starts_this_hour"] == 2


def test_a_test_from_the_hour_before_does_not_count_for_this_hour():
    w = World(latency=None)
    mc = make(w)
    w.box.append(Arrival(reference(H0 - 60 * MIN), H0 + 2 * MIN))  # last hour's test, 62 minutes late
    logs, msgs = run(mc, w, H0, H0 + 20 * MIN)
    assert [m.kind for m in msgs] == ["email_alert"]
    assert msgs[0].since == H0 + 2 * MIN  # it still counts as the last thing that arrived


# ---- TextIt will not accept the start ------------------------------------------

def test_refused_start_is_could_not_run_never_a_miss():
    for reason in ("HTTP 400: no such flow", "HTTP 401", "HTTP 500", "timeout after 8 s"):
        w = World(answers=[(False, reason)] * 20)
        mc = make(w)
        logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
        assert msgs == [] and mc.verdict is None and w.reads == 0
        assert set(actions(logs)) == {"could_not_run"} and len(logs) == 3  # three tries, then quiet
        assert mc.result.state == "could_not_run" and mc.result.reason == reason


def test_a_429_stops_starts_for_the_hour():
    w = World(answers=[(False, "HTTP 429 (TextIt asks to wait 1800 s)")] * 20)
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
    assert actions(logs) == ["could_not_run"] and msgs == []


def test_one_accepted_and_one_refused_never_alerts():
    w = World(latency=None, answers=[ACCEPT] + [(False, "HTTP 500")] * 20)
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
    assert msgs == [] and mc.verdict is None
    assert "not_arriving" not in actions(logs)


def test_refused_then_accepted_on_the_next_tick_runs_the_hour():
    w = World(answers=[(False, "HTTP 500"), ACCEPT])
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
    assert w.started == [reference(H0 + 5 * MIN)] and mc.verdict == "arriving" and msgs == []


# ---- the mailbox cannot be read ---------------------------------------------------

def test_read_error_gives_no_verdict_and_no_alert():
    w = World(latency=None)
    w.read_error = TimeoutError("timed out")
    mc = make(w)
    logs, msgs = run(mc, w, H0, H0 + 55 * MIN)
    assert msgs == [] and mc.verdict is None and len(w.started) == 1
    assert set(actions(logs)) == {"started", "read_error"}


def test_read_recovers_within_the_hour():
    w = World()
    w.read_error = TimeoutError("timed out")
    mc = make(w)
    run(mc, w, H0, H0 + 10 * MIN)
    w.read_error = None
    logs, msgs = run(mc, w, H0 + 15 * MIN, H0 + 15 * MIN)
    assert actions(logs) == ["seen"] and mc.verdict == "arriving"


# ---- the guard: memory lost ---------------------------------------------------------

def test_starts_this_instance_does_not_remember_never_make_a_verdict():
    w = World(latency=None)
    mc = make(w)
    run(mc, w, H0, H0 + 10 * MIN)  # start, miss, retry
    mc2 = make(w)  # the instance is replaced; nothing is remembered
    # at :20 the old instance would have said "not arriving"; the new one cannot, and does not
    logs, msgs = run(mc2, w, H0 + 15 * MIN, H0 + 30 * MIN)
    assert msgs == [] and "not_arriving" not in actions(logs)
    # it runs its own two tests instead; only when both of THOSE are missing does it alert
    logs, msgs = run(mc2, w, H0 + 35 * MIN, H0 + 35 * MIN)
    assert [m.kind for m in msgs] == ["email_alert"]
    assert [s.reference for s in mc2.starts] == [reference(H0 + 15 * MIN), reference(H0 + 25 * MIN)]


def test_new_instance_mid_hour_finds_the_test_that_already_arrived_and_starts_nothing():
    w = World()
    mc = make(w)
    run(mc, w, H0, H0 + 5 * MIN)
    mc2 = make(w)
    logs, msgs = run(mc2, w, H0 + 10 * MIN, H0 + 55 * MIN)
    assert len(w.started) == 1 and mc2.verdict == "arriving" and msgs == []
    seen = [entry for entry in logs if entry["action"] == "seen"][0]
    assert seen["tests"][0]["exact"] is False  # the start time is known only to the minute


def test_new_instance_mid_hour_with_nothing_arrived_runs_its_own_full_cycle():
    w = World(latency=None)
    mc = make(w)
    logs, msgs = run(mc, w, H0 + 15 * MIN, H0 + 55 * MIN)
    assert w.started == [reference(H0 + 15 * MIN), reference(H0 + 25 * MIN)]
    assert [m.kind for m in msgs] == ["email_alert"] and msgs[0].at == H0 + 35 * MIN


# ---- hour edges ----------------------------------------------------------------------

def test_a_late_tick_still_starts_on_the_hour_grid():
    w = World()
    mc = make(w)
    w.now = H0 + timedelta(seconds=90)  # the Scheduler tick logged 90 seconds late
    mc.step(w.now)
    assert w.started == [reference(H0)]


def test_a_missed_tick_on_the_hour_starts_at_five_past():
    w = World()
    mc = make(w)
    logs, msgs = run(mc, w, H0 + 5 * MIN, H0 + 55 * MIN)
    assert w.started == [reference(H0 + 5 * MIN)] and mc.verdict == "arriving"


def test_nothing_is_started_after_35_past():
    w = World(latency=None)
    mc = make(w)
    logs, msgs = run(mc, w, H0 + 40 * MIN, H0 + 55 * MIN)
    assert w.started == [] and msgs == [] and w.reads == 1  # one look for what already arrived


def test_first_start_at_35_can_still_finish_in_the_hour():
    w = World(latency=None)
    mc = make(w)
    logs, msgs = run(mc, w, H0 + 35 * MIN, H0 + 55 * MIN)
    assert [m.kind for m in msgs] == ["email_alert"] and msgs[0].at == H0 + 55 * MIN


def test_each_hour_runs_its_own_test_across_the_utc_date_roll():
    w = World()
    mc = make(w)
    start = datetime(2026, 10, 7, 23, 0, tzinfo=UTC)
    run(mc, w, start, start + 115 * MIN)
    assert w.started == ["EA-MC-20261007T2300Z", "EA-MC-20261008T0000Z"]
    assert hour_of(reference_time(w.started[1])) == datetime(2026, 10, 8, 0, 0, tzinfo=UTC)


def test_daylight_saving_changes_neither_skip_nor_double_an_hour():
    for day in (datetime(2026, 11, 1, 0, 0, tzinfo=UTC), datetime(2027, 3, 14, 0, 0, tzinfo=UTC)):
        w = World()
        mc = make(w)
        run(mc, w, day, day + timedelta(hours=24) - 5 * MIN)
        assert len(w.started) == 24 and len(set(w.started)) == 24


# ---- reminders and the all-clear -----------------------------------------------------

def test_failed_hours_alert_then_remind_every_two_hours_then_one_all_clear():
    w = World(latency=None)
    mc = make(w)
    w.box.append(Arrival(reference(H0 - 60 * MIN), H0 - 59 * MIN))  # the last good test
    logs, msgs = run(mc, w, H0, H0 + timedelta(hours=5) - 5 * MIN)  # five failed hours
    assert [(m.kind, m.at) for m in msgs] == [
        ("email_alert", H0 + 20 * MIN),
        ("email_repeat", H0 + timedelta(hours=2, minutes=20)),
        ("email_repeat", H0 + timedelta(hours=4, minutes=20)),
    ]
    assert all(m.since == H0 - 59 * MIN for m in msgs)
    w.latency = timedelta(seconds=30)  # email works again
    logs, msgs = run(mc, w, H0 + timedelta(hours=5), H0 + timedelta(hours=7) - 5 * MIN)
    assert [m.kind for m in msgs] == ["email_ended"]
    assert msgs[0].since == H0 - 59 * MIN and msgs[0].arrived == H0 + timedelta(hours=5, seconds=31)
    assert mc.episode is None


def test_a_refused_hour_inside_an_episode_neither_alerts_nor_closes_it():
    w = World(latency=None)
    mc = make(w)
    run(mc, w, H0, H0 + 55 * MIN)  # hour 1 fails: alert
    w.answers = [(False, "HTTP 500")] * 3
    logs, msgs = run(mc, w, H0 + 60 * MIN, H0 + 115 * MIN)  # hour 2: TextIt refuses
    assert msgs == [] and mc.episode is not None
    logs, msgs = run(mc, w, H0 + 120 * MIN, H0 + 175 * MIN)  # hour 3 fails: two hours since the alert
    assert [m.kind for m in msgs] == ["email_repeat"]


def test_recycle_during_an_outage_sends_a_fresh_alert_the_known_bound():
    w = World(latency=None)
    mc = make(w)
    run(mc, w, H0, H0 + 55 * MIN)
    mc2 = make(w)  # the episode is forgotten
    logs, msgs = run(mc2, w, H0 + 60 * MIN, H0 + 115 * MIN)
    assert [m.kind for m in msgs] == ["email_alert"]


# ---- the test switch ------------------------------------------------------------------

def test_force_misses_sends_a_test_alert_then_the_next_tick_sends_the_all_clear():
    w = World()
    mc = make(w)
    run(mc, w, H0, H0 + 10 * MIN)  # the hour's real test arrived
    w.now = H0 + 12 * MIN
    logs, msgs = mc.force_misses(w.now)
    assert [m.kind for m in msgs] == ["email_alert"] and msgs[0].test is True
    logs, msgs = run(mc, w, H0 + 15 * MIN, H0 + 15 * MIN)
    assert [m.kind for m in msgs] == ["email_ended"] and msgs[0].test is True
    assert len(w.started) == 1 and mc.episode is None


def test_force_misses_sends_nothing_when_the_mailbox_cannot_be_read():
    w = World()
    mc = make(w)
    w.read_error = TimeoutError("timed out")
    logs, msgs = mc.force_misses(H0 + 12 * MIN)
    assert msgs == [] and actions(logs) == ["read_error"] and mc.episode is None


def test_refused_test_records_could_not_run_and_sends_nothing():
    w = World()
    mc = make(w)
    run(mc, w, H0, H0 + 10 * MIN)
    logs = mc.record_refused_test(H0 + 12 * MIN, False, "HTTP 400: no such flow")
    assert actions(logs) == ["could_not_run"] and logs[0]["test"] is True
    assert mc.result.state == "could_not_run" and mc.verdict == "arriving" and mc.episode is None


# ---- what a triage alert is told -------------------------------------------------------

def test_latest_reads_the_mailbox_once_when_nothing_is_remembered():
    w = World()
    w.box.append(Arrival(reference(H0 - 60 * MIN), H0 - 59 * MIN))
    mc = make(w)
    result, last, note = mc.latest(H0 + 3 * MIN)
    assert result is None and last == H0 - 59 * MIN and note == "" and w.reads == 1
    mc.latest(H0 + 4 * MIN)
    assert w.reads == 1


def test_latest_says_unavailable_when_that_read_fails():
    w = World()
    w.read_error = TimeoutError("timed out")
    mc = make(w)
    result, last, note = mc.latest(H0)
    assert result is None and last is None and note == "unavailable (the test mailbox could not be read)"
