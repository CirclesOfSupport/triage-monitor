"""The hourly email check: is an email sent by a TextIt flow still arriving?

TextIt reports nothing about a Send Email action, so the only evidence that
its email works is one of its emails arriving. Once an hour this module has
TextIt start one small flow that emails a test to a mailbox the service reads,
and then looks for that test.

Per hour (the UTC hour, so a daylight-saving change cannot skip or double one),
on the 5-minute tick:

  1. no test started yet -> start one (reference = the tick time);
  2. 10 minutes later    -> read the mailbox; the test is there -> ARRIVING;
                            not there -> start one more (the retry);
  3. 10 minutes later    -> read again; either test there -> ARRIVING;
                            neither -> NOT ARRIVING.

The guard against a false alarm: NOT ARRIVING needs, in this instance's memory,
two starts TextIt accepted in this hour, each at least 10 minutes old, and a
mailbox read that succeeded and found neither. A lost memory, a refused start
or a failed read gives no verdict for the hour, never an alert.

Nothing here talks to the network or the clock: the caller passes the time and
three functions (start a test, read the mailbox, clean the mailbox), so every
path is unit-tested with fakes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from rule import tick_floor, utc

WAIT = timedelta(minutes=10)  # start -> first look, and retry -> second look
LAST_FIRST_START_MINUTE = 35  # a first test is not started after :35 (its cycle could not finish in the hour)
LAST_RETRY_START_MINUTE = 45
MAX_REFUSALS_PER_HOUR = 3
REMIND_EVERY = timedelta(hours=2)
KEEP = timedelta(days=2)  # how long a test message stays in the mailbox before it is moved to Trash

REFERENCE_RE = re.compile(r"EA-MC-(\d{8}T\d{4})Z")


def reference(tick: datetime) -> str:
    return utc(tick).strftime("EA-MC-%Y%m%dT%H%MZ")


def reference_time(ref: str) -> datetime:
    m = REFERENCE_RE.fullmatch(ref)
    if not m:
        raise ValueError(f"not a test reference: {ref!r}")
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)


def hour_of(t: datetime) -> datetime:
    return utc(t).replace(minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Arrival:
    """One test message found in the mailbox."""
    reference: str
    arrived: datetime  # when the mailbox received it (UTC)


@dataclass
class Start:
    reference: str
    tick: datetime  # the grid tick it was started on
    at: datetime  # the moment TextIt was asked


@dataclass(frozen=True)
class Result:
    """The check's latest result, kept for the line on a triage alert."""
    state: str  # 'arriving' | 'not_arriving' | 'could_not_run'
    at: datetime
    reason: str = ""


@dataclass
class Episode:
    start_hour: datetime
    last_sent_hour: datetime
    since: Optional[datetime]  # the last test that arrived before the episode
    test: bool = False


@dataclass(frozen=True)
class Message:
    """A message that is due to people. The words are made in messages.py."""
    kind: str  # 'email_alert' | 'email_repeat' | 'email_ended'
    at: datetime
    since: Optional[datetime]  # last arrival before the trouble (None: none in the mailbox)
    sent: Tuple[datetime, ...] = ()  # when this hour's tests were started
    arrived: Optional[datetime] = None  # email_ended: when the test that arrived was received
    test: bool = False


StartFn = Callable[[str, datetime], Tuple[bool, str]]
ReadFn = Callable[[datetime], List[Arrival]]
CleanFn = Callable[[datetime], int]


class MailCheck:
    """All state is memory. It is lost when the instance is replaced; see the README for
    what that costs (one extra alert, possibly no all-clear) and why nothing is stored."""

    def __init__(self, start_fn: StartFn, read_fn: ReadFn, clean_fn: Optional[CleanFn] = None):
        self._start_fn = start_fn
        self._read_fn = read_fn
        self._clean_fn = clean_fn
        self.hour: Optional[datetime] = None
        self.starts: List[Start] = []
        self.verdict: Optional[str] = None
        self.refusals = 0
        self.no_more_starts = False
        self.cold_checked = False
        self.result: Optional[Result] = None
        self.last_arrival: Optional[datetime] = None
        self.episode: Optional[Episode] = None

    # ------------------------------------------------------------ the tick

    def step(self, now: datetime) -> Tuple[List[dict], List[Message]]:
        """One tick of the check. Returns (log payloads, messages due)."""
        now = utc(now)
        t = tick_floor(now)
        hour = hour_of(t)
        logs: List[dict] = []
        msgs: List[Message] = []
        if self.hour != hour:
            self.hour = hour
            self.starts = []
            self.verdict = None
            self.refusals = 0
            self.no_more_starts = False
            # On the hour nothing of this hour can have arrived. Later in the hour, an
            # instance with no memory looks first: the mailbox is the record of what arrived.
            self.cold_checked = t.minute == 0
        if self.verdict is not None:
            return logs, msgs

        if not self.starts:
            if not self.cold_checked:
                arrivals = self._read(now, t, logs)
                if arrivals is None:
                    return logs, msgs
                self.cold_checked = True
                mine = self._of_hour(arrivals, hour)
                if mine:
                    self._arriving(now, t, mine, logs, msgs)
                    return logs, msgs
            if t.minute <= LAST_FIRST_START_MINUTE:
                self._start(now, t, logs)
            return logs, msgs

        latest = self.starts[-1]
        if t < latest.tick + WAIT:
            return logs, msgs
        arrivals = self._read(now, t, logs)
        if arrivals is None:
            return logs, msgs
        mine = self._of_hour(arrivals, hour)
        if mine:
            self._arriving(now, t, mine, logs, msgs)
            return logs, msgs
        if len(self.starts) == 1:
            logs.append({"action": "missing", "tick": t.isoformat(), "reference": latest.reference,
                         "waited_minutes": int((t - latest.tick).total_seconds() // 60)})
            if t.minute <= LAST_RETRY_START_MINUTE:
                self._start(now, t, logs)
            return logs, msgs
        self._not_arriving(t, hour, logs, msgs)
        return logs, msgs

    # ------------------------------------------------------------ pieces

    @staticmethod
    def _of_hour(arrivals: List[Arrival], hour: datetime) -> List[Arrival]:
        return [a for a in arrivals if hour_of(reference_time(a.reference)) == hour]

    def _read(self, now: datetime, t: datetime, logs: List[dict]) -> Optional[List[Arrival]]:
        try:
            arrivals = list(self._read_fn(now))
        except Exception as exc:  # noqa: BLE001 - a failed read is a state, not a crash
            logs.append({"action": "read_error", "severity": "WARNING", "tick": t.isoformat(),
                         "reason": f"{type(exc).__name__}: {exc}"[:200]})
            return None
        if arrivals:
            newest = max(a.arrived for a in arrivals)
            if self.last_arrival is None or newest > self.last_arrival:
                self.last_arrival = newest
        return arrivals

    def _start(self, now: datetime, t: datetime, logs: List[dict]) -> None:
        if self.no_more_starts or self.refusals >= MAX_REFUSALS_PER_HOUR:
            return
        ref = reference(t)
        if any(s.reference == ref for s in self.starts):
            return
        try:
            ok, reason = self._start_fn(ref, t)
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {exc}"[:200]
        if ok:
            self.starts.append(Start(reference=ref, tick=t, at=now))
            logs.append({"action": "started", "tick": t.isoformat(), "reference": ref,
                         "attempt": len(self.starts)})
            return
        self.refusals += 1
        if "429" in reason:
            self.no_more_starts = True
        self.result = Result("could_not_run", t, reason)
        logs.append({"action": "could_not_run", "severity": "WARNING", "tick": t.isoformat(),
                     "reference": ref, "reason": reason, "refusals_this_hour": self.refusals})

    def _arriving(self, now: datetime, t: datetime, mine: List[Arrival], logs: List[dict], msgs: List[Message]) -> None:
        self.verdict = "arriving"
        self.result = Result("arriving", t)
        tests = []
        for a in sorted(mine, key=lambda x: x.arrived):
            started = next((s for s in self.starts if s.reference == a.reference), None)
            # exact: measured from the moment this instance asked TextIt. Otherwise the start
            # is known only to the minute (the reference), because the memory of it is gone.
            origin = started.at if started else reference_time(a.reference)
            tests.append({"reference": a.reference, "arrived": a.arrived.isoformat(),
                          "seconds": int((a.arrived - origin).total_seconds()), "exact": started is not None})
        logs.append({"action": "seen", "tick": t.isoformat(), "tests": tests,
                     "slowest_seconds": max(x["seconds"] for x in tests),
                     "starts_this_hour": len(self.starts)})
        if self.episode is not None:
            msgs.append(Message(kind="email_ended", at=t, since=self.episode.since,
                                arrived=max(a.arrived for a in mine), test=self.episode.test))
            self.episode = None
        if self._clean_fn is not None:
            try:
                moved = self._clean_fn(now)
                if moved:
                    logs.append({"action": "cleaned", "tick": t.isoformat(), "moved_to_trash": moved})
            except Exception as exc:  # noqa: BLE001
                logs.append({"action": "clean_error", "severity": "WARNING", "tick": t.isoformat(),
                             "reason": f"{type(exc).__name__}: {exc}"[:200]})

    def _not_arriving(self, t: datetime, hour: datetime, logs: List[dict], msgs: List[Message], test: bool = False,
                      since: Optional[datetime] = None) -> None:
        """since: the last arrival the message names; the test switch passes the last one before
        this hour (its own hour's real test may have arrived). None means the latest arrival."""
        self.verdict = "not_arriving"
        self.result = Result("not_arriving", t)
        sent = tuple(s.at for s in self.starts)
        if not test:
            since = self.last_arrival
        logs.append({"action": "not_arriving", "severity": "WARNING", "tick": t.isoformat(),
                     "references": [s.reference for s in self.starts],
                     "last_arrival": None if since is None else since.isoformat(),
                     "test": test})
        if self.episode is None:
            self.episode = Episode(start_hour=hour, last_sent_hour=hour, since=since, test=test)
            msgs.append(Message(kind="email_alert", at=t, since=since, sent=sent, test=test))
        elif hour - self.episode.last_sent_hour >= REMIND_EVERY:
            self.episode.last_sent_hour = hour
            msgs.append(Message(kind="email_repeat", at=t, since=self.episode.since, sent=sent, test=self.episode.test))

    # ------------------------------------------------------------ the test switch

    def force_misses(self, now: datetime) -> Tuple[List[dict], List[Message]]:
        """Test switch: behave as if this hour's two tests were both missing. The mailbox is
        really read (a failed read sends nothing). The message names the last arrival before
        this hour and no send times, since no test was really missed; every time in it is
        real. The hour's cycle is then reset, so the next scheduled tick looks again, finds the
        hour's real test and sends the all-clear."""
        now = utc(now)
        t = tick_floor(now)
        hour = hour_of(t)
        logs: List[dict] = []
        msgs: List[Message] = []
        if self.episode is not None:
            logs.append({"action": "test_refused", "tick": t.isoformat(), "reason": "an episode is already open"})
            return logs, msgs
        arrivals = self._read(now, t, logs)
        if arrivals is None:
            return logs, msgs
        before = [a.arrived for a in arrivals if a.arrived < hour]
        self.hour = hour
        self.starts = []
        self._not_arriving(t, hour, logs, msgs, test=True, since=max(before) if before else None)
        self.starts = []
        self.verdict = None
        self.refusals = 0
        self.no_more_starts = False
        self.cold_checked = False
        return logs, msgs

    def record_refused_test(self, now: datetime, ok: bool, reason: str) -> List[dict]:
        """Test switch: the caller asked TextIt to start a flow that does not exist. The refusal
        is recorded as the check's latest result; the hour's own cycle is not disturbed."""
        t = tick_floor(utc(now))
        if ok:
            return [{"action": "test_refused_start", "severity": "WARNING", "tick": t.isoformat(),
                     "outcome": "TextIt ACCEPTED a start that should have been refused"}]
        self.result = Result("could_not_run", t, reason)
        return [{"action": "could_not_run", "severity": "WARNING", "tick": t.isoformat(),
                 "reason": reason, "test": True}]

    # ------------------------------------------------------------ the line on a triage alert

    def latest(self, now: datetime) -> Tuple[Optional[Result], Optional[datetime], str]:
        """(result, last arrival, note) for the line a triage alert carries. With no result in
        memory (a new instance) the mailbox is read once for the last arrival."""
        if self.result is None and self.last_arrival is None:
            logs: List[dict] = []
            if self._read(utc(now), tick_floor(utc(now)), logs) is None:
                # Plain words: this sentence is read by people. The log line carries the detail.
                return None, None, "unavailable (the test mailbox could not be read)"
        return self.result, self.last_arrival, ""
