"""The replay runs on a synthetic fixture (no history file needed) and the daily-cap check holds."""

import csv
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/replay")
sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/src")

import replay  # noqa: E402
from rule import CENTRAL  # noqa: E402


def test_replay_on_synthetic_csv(tmp_path):
    reqs, s0, s1 = replay.synthetic_determination_cycle_day()
    path = tmp_path / "history.csv"
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["req_epoch", "det_epoch"])
        for r in reqs:
            w.writerow([int(r.req_ts.timestamp()), "" if r.det_ts is None else int(r.det_ts.timestamp())])
    loaded = replay.load(str(path))
    assert len(loaded) == len(reqs)
    every, due, ticks, reads, in_window_ticks = replay.run(loaded, s0 - timedelta(hours=2), s1 + timedelta(hours=1))
    assert [(a, b) for a, b, *_ in every] == due  # read-when-due fires on the same ticks
    assert every[0][1] == "alert" and every[0][0].astimezone(CENTRAL).strftime("%H:%M") == "10:30"
    assert reads < ticks


def test_daily_cap_never_reached_on_synthetic_worst_days():
    for name, n, kinds in replay.worst_days():
        assert n < replay.DAILY_CAP, (name, n, kinds)
    assert replay.worst_days()[0][1] == 12  # the determination-cycle ceiling
