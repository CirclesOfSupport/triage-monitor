"""Replay the deployed rule over the table's history at every 5-minute tick.

Row 2: every alert / repeat / ended event the rule would have produced, with
the numbers it would have sent.
Row 4: the same replay with the service's read-only-when-due schedule; the two
must fire on identical ticks.
Daily cap: Cloud Monitoring delivers at most 20 notifications a day (and one
every 5 minutes) for a log-based alerting policy. The replay reports the most
messages the rule sent in any one Central day over the history and over two
synthetic worst days, and fails if any reaches 20.

Input: a CSV of one row per triage request (req_epoch, det_epoch or blank),
the same three-column read the service makes, exported from BigQuery (see the
query in src/main.py). The path is required; the file is not part of this
repository. Each tick is fed the requests from the last 3 days, exactly as the
service's query returns them.

    python3 replay/replay.py <history.csv> 2026-08-01T05:00:00Z 2026-10-03T04:55:00Z
"""

import bisect
import csv
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")
from rule import CENTRAL, Request, TICK, compose, decide, in_window  # noqa: E402

LOOKBACK = timedelta(days=3)
DAILY_CAP = 20  # Cloud Monitoring: notifications per day for one log-based alerting policy


def load(path):
    reqs = []
    with open(path) as fh:
        for row in csv.DictReader(fh):
            r = datetime.fromtimestamp(int(row["req_epoch"]), tz=timezone.utc)
            d = row["det_epoch"]
            d = datetime.fromtimestamp(int(d), tz=timezone.utc) if d else None
            reqs.append(Request(req_ts=r, det_ts=d))
    reqs.sort(key=lambda x: x.req_ts)
    return reqs


def slice_for(reqs, keys, t):
    lo = bisect.bisect_left(keys, t - LOOKBACK)
    hi = bisect.bisect_right(keys, t)
    return reqs[lo:hi]


def ct(dt):
    return dt.astimezone(CENTRAL).strftime("%a %Y-%m-%d %-I:%M %p")


def run(reqs, t0, t1):
    """Returns (events_every_tick, events_when_due, ticks, reads, in_window_ticks).
    An event is (tick, kind, ended_reason, waiting, silence_minutes, subject)."""
    reqs = sorted(reqs, key=lambda x: x.req_ts)
    keys = [r.req_ts for r in reqs]
    every, due_events = [], []
    ticks = reads = in_window_ticks = 0
    next_due = None
    t = t0
    while t <= t1:
        ticks += 1
        if in_window(t):
            in_window_ticks += 1
        d = decide(slice_for(reqs, keys, t), t)
        if d.kind:
            every.append((d.tick, d.kind, d.ended_reason, d.now.waiting, d.now.silence_minutes, compose(d)["subject"]))
        if next_due is None or t >= next_due:
            reads += 1
            next_due = d.next_due
            if d.kind:
                due_events.append((d.tick, d.kind))
        t += TICK
    return every, due_events, ticks, reads, in_window_ticks


def messages_per_central_day(events):
    c = Counter(e[0].astimezone(CENTRAL).date() for e in events)
    if not c:
        return 0, None
    day, n = max(c.items(), key=lambda kv: kv[1])
    return n, day


def _window_start(day):
    return datetime(day.year, day.month, day.day, 10, 30, tzinfo=CENTRAL).astimezone(timezone.utc)


def synthetic_determination_cycle_day(day=datetime(2026, 7, 15)):
    """The fastest alert/ended cycling the rule allows by determinations: an episode starts
    at 10:30 AM, a determination lands a minute later (ended on the next tick), and silence
    must then build 90 minutes before the next episode. Three requests always waiting."""
    start = _window_start(day)
    end = start + timedelta(hours=9, minutes=30)
    reqs = []
    # three undetermined requests that never age out inside the window (arrived 9:00 AM)
    for i in range(3):
        reqs.append(Request(req_ts=start - timedelta(minutes=90 + i), det_ts=None))
    # a determination 8 PM the evening before, so the day opens silent
    reqs.append(Request(req_ts=start - timedelta(hours=16), det_ts=start - timedelta(hours=14, minutes=30)))
    # greedy: whenever the rule fires, a determination lands one minute later
    t = start
    while t < end:
        d = decide(reqs, t)
        if d.kind == "alert":
            reqs.append(Request(req_ts=t - timedelta(hours=2), det_ts=t + timedelta(minutes=1)))
        t += TICK
    return reqs, start, end


def synthetic_aging_flap_day(day=datetime(2026, 7, 15)):
    """An episode ending because the waiting count fell below 3 as a request aged past 14 hours,
    restarting when a fresh request reached 30 minutes (no determination), and then the fastest
    determination cycling for the rest of the day. Without a determination the waiting count can
    only fall by aging out, and every fresh request stays in the 14-hour window past 8 PM, so the
    count can cross the 3 boundary downward at most once per day: the flap adds two messages."""
    start = _window_start(day)
    end = start + timedelta(hours=9, minutes=30)
    reqs = [Request(req_ts=start - timedelta(hours=16), det_ts=start - timedelta(hours=14, minutes=30))]
    # two requests always waiting (arrived 9:00 AM, never age out before 8 PM)
    reqs += [Request(req_ts=start - timedelta(minutes=90 + i), det_ts=None) for i in range(2)]
    # the third ages out at 10:35 (arrived 14 h before); a fresh one reaches 30 minutes at 11:00
    reqs.append(Request(req_ts=start + timedelta(minutes=5) - timedelta(hours=14), det_ts=None))
    reqs.append(Request(req_ts=start + timedelta(minutes=30) - timedelta(minutes=30), det_ts=None))
    # then greedy determination cycles
    t = start + timedelta(minutes=30)
    while t < end:
        d = decide(reqs, t)
        if d.kind == "alert":
            reqs.append(Request(req_ts=t - timedelta(hours=2), det_ts=t + timedelta(minutes=1)))
        t += TICK
    return reqs, start, end


def worst_days():
    """(name, messages in the day, kinds) for each synthetic worst day."""
    out = []
    for name, builder in (("determination cycles", synthetic_determination_cycle_day),
                          ("aging-out flap, then determination cycles", synthetic_aging_flap_day)):
        sreqs, s0, s1 = builder()
        sevents, _, _, _, _ = run(sreqs, s0, s1)
        n, _ = messages_per_central_day(sevents)
        out.append((name, n, dict(Counter(e[1] for e in sevents))))
    return out


def main(path, start, end):
    reqs = load(path)
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
    every, due_events, ticks, reads, in_window_ticks = run(reqs, t0, t1)

    print(f"ticks replayed: {ticks} ({start} -> {end}); requests: {len(reqs)}")
    print("\nRow 2 — events (Central):")
    for tick, kind, reason, waiting, silence, subject in every:
        print(f"  {ct(tick):28s} {kind:7s} {reason or '':14s} waiting={waiting:3d} silent={silence:7.0f} min  | {subject}")
    fired_every = [(a, b) for a, b, *_ in every]
    identical = fired_every == due_events
    print(f"\nRow 4 — read-when-due: reads={reads} of {ticks} ticks ({in_window_ticks} in-window ticks); events identical: {identical}")
    if not identical:
        for e in set(fired_every) ^ set(due_events):
            print("   DIFFERENCE:", ct(e[0]), e[1])
    days = (t1 - t0).days
    print(f"  reads per day: {reads / days:.1f}")

    n_hist, day_hist = messages_per_central_day(every)
    print(f"\nDaily cap ({DAILY_CAP} notifications a day for one log-based policy):")
    print(f"  history: most messages in one Central day = {n_hist} ({day_hist})")
    ok = n_hist < DAILY_CAP
    for name, n, kinds in worst_days():
        print(f"  synthetic worst day, {name}: {n} messages {kinds}")
        ok = ok and n < DAILY_CAP
    print(f"  under the cap: {ok}")
    return 0 if (identical and ok) else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
