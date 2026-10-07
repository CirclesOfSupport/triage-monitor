"""The triage-silence rule, as pure functions of (tick time, the triage requests).

Nothing in this module talks to BigQuery, the clock, or the log. Every decision
is a function of the tick time and the set of requests, so the same code can be
replayed over history and unit-tested at the edges.

The rule (approved 2026-10-04):

  silence  = minutes since the latest determination_time
  waiting  = requests whose triage_request_time is at least 30 minutes and at
             most 14 hours old and that have no determination_time
  ALERT when silence >= 90 AND waiting >= 3 AND the Central clock
  (America/Chicago) is at or after 10:30 AM and before 8:00 PM, every day.

Episodes: an episode starts on the first 5-minute tick where the rule holds,
repeats every 2 hours while it holds, and ends on the first tick where it no
longer holds. The episode start is found by walking the tick grid back from the
current tick, so no state is stored between ticks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
WINDOW_START = time(10, 30)
WINDOW_END = time(20, 0)  # exclusive
SILENCE_MINUTES = 90
WAIT_MIN_MINUTES = 30
WAIT_MAX_HOURS = 14
WAITING_COUNT = 3
TICK = timedelta(minutes=5)
REPEAT = timedelta(hours=2)

# Plain words for the people who read the alert. The table and its columns are named in
# the README's first section, which is where a change of mechanism is carried out.
CHECKS = (
    "the time of the last triage determination recorded in our database, against "
    "requests still waiting. If the way determinations are recorded changes, this "
    "monitor must be updated."
)


@dataclass(frozen=True)
class Request:
    req_ts: datetime  # UTC, aware
    det_ts: Optional[datetime]  # UTC, aware, or None


@dataclass(frozen=True)
class Measure:
    at: datetime
    last_det: Optional[datetime]
    silence_minutes: Optional[float]  # None when no determination exists
    waiting: int
    in_window: bool

    @property
    def fires(self) -> bool:
        silent = self.silence_minutes is None or self.silence_minutes >= SILENCE_MINUTES
        return self.in_window and silent and self.waiting >= WAITING_COUNT


@dataclass(frozen=True)
class Decision:
    tick: datetime
    now: Measure
    prev: Measure
    kind: Optional[str]  # 'alert' | 'repeat' | 'ended' | None
    episode_start: Optional[datetime]
    ended_reason: Optional[str]  # 'determination' | 'window_closed' | 'waiting_fell'
    next_due: datetime


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def tick_floor(t: datetime) -> datetime:
    """Floor to the 5-minute grid (UTC)."""
    t = utc(t)
    return t.replace(second=0, microsecond=0, minute=t.minute - (t.minute % 5))


def in_window(t: datetime) -> bool:
    local = utc(t).astimezone(CENTRAL).time()
    return WINDOW_START <= local < WINDOW_END


def next_window_start(t: datetime) -> datetime:
    """The next moment the window opens at or after t (UTC)."""
    local = utc(t).astimezone(CENTRAL)
    start_today = datetime.combine(local.date(), WINDOW_START, tzinfo=CENTRAL)
    if local < start_today:
        return start_today.astimezone(timezone.utc)
    start_tomorrow = datetime.combine(local.date() + timedelta(days=1), WINDOW_START, tzinfo=CENTRAL)
    return start_tomorrow.astimezone(timezone.utc)


def window_end_today(t: datetime) -> datetime:
    local = utc(t).astimezone(CENTRAL)
    return datetime.combine(local.date(), WINDOW_END, tzinfo=CENTRAL).astimezone(timezone.utc)


def measure(requests: Iterable[Request], t: datetime) -> Measure:
    """What the table looked like at time t (as-of semantics on determination_time)."""
    t = utc(t)
    last_det: Optional[datetime] = None
    waiting = 0
    wait_newest = t - timedelta(minutes=WAIT_MIN_MINUTES)
    wait_oldest = t - timedelta(hours=WAIT_MAX_HOURS)
    for r in requests:
        if r.det_ts is not None and r.det_ts <= t and (last_det is None or r.det_ts > last_det):
            last_det = r.det_ts
        if r.req_ts <= wait_newest and r.req_ts > wait_oldest and (r.det_ts is None or r.det_ts > t):
            waiting += 1
    silence = None if last_det is None else (t - last_det).total_seconds() / 60.0
    return Measure(at=t, last_det=last_det, silence_minutes=silence, waiting=waiting, in_window=in_window(t))


def episode_start(requests: list[Request], tick: datetime) -> datetime:
    """Walk the tick grid back while the rule holds. Bounded by the window start,
    because the rule is false outside the window by definition."""
    s = tick
    while True:
        prev = s - TICK
        if not in_window(prev) or not measure(requests, prev).fires:
            return s
        s = prev


def next_due(requests: list[Request], tick: datetime, now_m: Measure) -> datetime:
    """The earliest moment the alert could possibly be true again (fifth-pass formula).

    Looking early is harmless; this must never look late. While alerting the
    next read is the next tick, because only a read can see the episode end.
    """
    if now_m.fires:
        return tick + TICK
    if not now_m.in_window:
        return next_window_start(tick)
    # A: the latest determination + 90 minutes (no determination at all: now)
    a = tick if now_m.last_det is None else now_m.last_det + timedelta(minutes=SILENCE_MINUTES)
    # B: the moment a third undetermined request reaches 30 minutes of waiting.
    # Aging past 14 hours and determinations landing can only push this later,
    # new requests can only arrive after now, so this estimate is never late.
    oldest = tick - timedelta(hours=WAIT_MAX_HOURS)
    undetermined = sorted(r.req_ts for r in requests if r.req_ts > oldest and r.req_ts <= tick and (r.det_ts is None or r.det_ts > tick))
    if len(undetermined) >= WAITING_COUNT:
        b = undetermined[WAITING_COUNT - 1] + timedelta(minutes=WAIT_MIN_MINUTES)
    else:
        b = tick + timedelta(minutes=WAIT_MIN_MINUTES)
    due = max(a, b, tick + TICK)
    if due >= window_end_today(tick):
        return next_window_start(due)
    return due


def decide(requests: list[Request], now: datetime) -> Decision:
    tick = tick_floor(now)
    now_m = measure(requests, tick)
    prev_m = measure(requests, tick - TICK)
    kind = None
    start = None
    reason = None
    if now_m.fires:
        start = episode_start(requests, tick)
        if start == tick:
            kind = "alert"
        else:
            elapsed = tick - start
            if elapsed % REPEAT == timedelta(0):
                kind = "repeat"
    elif prev_m.fires:
        kind = "ended"
        if now_m.last_det is not None and (prev_m.last_det is None or now_m.last_det > prev_m.last_det):
            reason = "determination"
        elif not now_m.in_window:
            reason = "window_closed"
        else:
            reason = "waiting_fell"
    return Decision(
        tick=tick,
        now=now_m,
        prev=prev_m,
        kind=kind,
        episode_start=start,
        ended_reason=reason,
        next_due=next_due(requests, tick, now_m),
    )


# ---------------------------------------------------------------- messages

def _ct(dt: datetime) -> str:
    local = utc(dt).astimezone(CENTRAL)
    return local.strftime("%-I:%M %p CT on %a %b %-d")


def _duration(minutes: float) -> str:
    m = int(minutes)
    h, m = divmod(m, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def compose(d: Decision, test: bool = False, email_line: Optional[str] = None) -> Optional[dict]:
    """The words of the message for a decision, or None when nothing is due.
    Keys: kind, subject, headline, last_determination, waiting, checks.
    email_line: one sentence with the hourly email check's latest result; it is added to
    an alert and to a reminder (an ended message has nothing to diagnose).
    """
    if d.kind is None:
        return None
    m = d.now
    last = "none in the last 3 days" if m.last_det is None else _ct(m.last_det)
    silent = "" if m.silence_minutes is None else _duration(m.silence_minutes)
    prefix = "TEST — " if test else ""
    if d.kind == "alert":
        subject = f"{prefix}Early Alert: triage has been silent {silent} — {m.waiting} requests waiting"
        headline = (
            f"No triage determination since {last} ({silent}); "
            f"{m.waiting} requests have waited 30 minutes or more. "
            f"Please check that triage requests are reaching the counselors."
        )
    elif d.kind == "repeat":
        subject = f"{prefix}Early Alert: triage still silent {silent} — {m.waiting} requests waiting"
        headline = (
            f"Still no triage determination since {last} ({silent}); "
            f"{m.waiting} requests have waited 30 minutes or more. "
            f"This reminder repeats every 2 hours while triage stays silent."
        )
    else:  # ended
        if d.ended_reason == "determination":
            subject = f"{prefix}Early Alert: triage resumed — determination at {last}"
            headline = (
                f"A triage determination landed at {last}. "
                f"{m.waiting} requests are still waiting 30 minutes or more."
            )
        elif d.ended_reason == "window_closed":
            subject = f"{prefix}Early Alert: triage STILL silent — monitoring hours over until 10:30 AM CT; {m.waiting} requests waiting"
            headline = (
                f"Triage is STILL silent: no determination since {last} ({silent}), and {m.waiting} requests are "
                f"still waiting. This is not resolved. The monitor's hours are over for today (10:30 AM to 8:00 PM CT); "
                f"nothing will be sent until it checks again at 10:30 AM CT."
            )
        else:
            subject = f"{prefix}Early Alert: triage NOT resolved — alert ended, fewer than 3 requests waiting"
            headline = (
                f"Fewer than 3 requests are now waiting (requests older than 14 hours are no longer counted); "
                f"no determination since {last}."
            )
    if email_line and d.kind in ("alert", "repeat"):
        headline = f"{headline} {email_line}"
    return {
        "kind": d.kind,
        "subject": subject,
        "headline": headline,
        "last_determination": last,
        "waiting": str(m.waiting),
        "checks": CHECKS,
    }
