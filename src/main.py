"""triage-monitor — alerts when triage determinations go silent while requests wait.

One endpoint does the work. Cloud Scheduler calls POST /tick every 5 minutes,
all day. Each tick does two things:

1. The triage rule. The service reads BigQuery only when the alert could possibly
   be true (the next-read-due moment kept in memory; an instance with no memory
   reads) and writes one structured log line per tick.
2. The hourly email check (mailcheck.py), when it is switched on: has TextIt send
   a test email, looks for it in a mailbox, and says so when two in a row go missing.

When a message is due the service first writes a `notify` log line (a Cloud
Monitoring policy carries that line as a second copy) and then sends its own plain
email. Recipients, addresses and both credentials are settings of the running
service; none is in this repository.

Log lines (jsonPayload):
  event=ran        every tick: read|skipped, the numbers, next_due
  event=notify     a message is due: kind, subject, headline, last_determination, waiting, checks
  event=own_email  the service's own email for that message: sent|failed|not_configured, attempts
  event=mailcheck  a step of the email check: started, seen (with seconds from send to arrival),
                   missing, could_not_run, read_error, not_arriving
  event=error      the BigQuery read failed (the tick answers 500)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional, Tuple

from flask import Flask, jsonify, request

import mailer
import mailread
import messages
import textit
from mailcheck import MailCheck
from rule import CENTRAL, Request, compose, decide, tick_floor, utc

app = Flask(__name__)

PROJECT = os.environ.get("BQ_PROJECT", "early-alert-responses")
TABLE = os.environ.get("TRIAGE_TABLE", "early-alert-responses.RESPONSES.triage-message-data")
LOOKBACK_DAYS = 3

QUERY = f"""
SELECT triage_request_id,
       MIN(triage_request_time) AS req_ts,
       MIN(determination_time) AS det_ts
FROM `{TABLE}`
WHERE triage_request_id IS NOT NULL
  AND triage_request_time >= TIMESTAMP_SUB(@now, INTERVAL {LOOKBACK_DAYS} DAY)
  AND triage_request_time <= @now
GROUP BY triage_request_id
"""

_lock = threading.Lock()
_memory: dict = {"next_due": None}  # lost on every cold start; costs one read

TRIAGE_TESTS = ("alert", "ended")
EMAIL_TESTS = ("email_misses", "email_refused")
NO_SUCH_FLOW = "00000000-0000-0000-0000-000000000000"  # the email_refused test asks TextIt to start this


@dataclass(frozen=True)
class Config:
    """Settings of the running service, read from the environment on every use.
    The two credentials arrive as secret-backed variables; nothing here has a default value."""
    email_check: str
    textit_token: str
    flow_uuid: str
    contact_uuid: str
    mailcheck_address: str
    mail_username: str
    mail_password: str
    alert_from: str
    alert_recipients: Tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        recipients = tuple(a.strip() for a in env("ALERT_RECIPIENTS", "").replace(";", ",").split(",") if a.strip())
        return cls(
            email_check=env("EMAIL_CHECK", "off").strip().lower(),
            textit_token=env("TEXTIT_TOKEN", "").strip(),
            flow_uuid=env("TEXTIT_FLOW_UUID", "").strip(),
            contact_uuid=env("TEXTIT_CONTACT_UUID", "").strip(),
            mailcheck_address=env("MAILCHECK_ADDRESS", "").strip(),
            mail_username=env("MAIL_USERNAME", "").strip(),
            mail_password=env("MAIL_APP_PASSWORD", "").replace(" ", "").strip(),
            alert_from=env("ALERT_FROM", "").strip(),
            alert_recipients=recipients,
        )

    def missing_for_check(self) -> list:
        need = {"TEXTIT_TOKEN": self.textit_token, "TEXTIT_FLOW_UUID": self.flow_uuid,
                "TEXTIT_CONTACT_UUID": self.contact_uuid, "MAILCHECK_ADDRESS": self.mailcheck_address,
                "MAIL_USERNAME": self.mail_username, "MAIL_APP_PASSWORD": self.mail_password}
        return [name for name, value in need.items() if not value]

    @property
    def check_state(self) -> str:
        """'off' | 'on' | 'not_configured' (switched on with a setting missing)."""
        if self.email_check != "on":
            return "off"
        return "not_configured" if self.missing_for_check() else "on"

    @property
    def own_email_ready(self) -> bool:
        return bool(self.mail_username and self.mail_password and self.alert_from and self.alert_recipients)

    def never_log(self) -> list:
        """Values that must never reach a log line: both credentials and every address."""
        values = [self.textit_token, self.mail_password, self.mailcheck_address, self.mail_username,
                  parseaddr(self.alert_from)[1], *self.alert_recipients]
        return sorted({v for v in values if v}, key=len, reverse=True)


def log(payload: dict) -> None:
    """One JSON line to stdout; Cloud Run ingests it as jsonPayload.
    A credential or an address that found its way into a line is struck out before it is written."""
    payload.setdefault("severity", "INFO")
    line = json.dumps(payload, default=str)
    for value in Config.from_env().never_log():
        line = line.replace(value, "[withheld]")
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- the email check's three hands

def _start_test(reference: str, tick: datetime):
    cfg = Config.from_env()
    sent_ct = utc(tick).astimezone(CENTRAL).strftime("%-I:%M %p CT on %a %b %-d")
    return textit.start_flow(cfg.textit_token, cfg.flow_uuid, cfg.contact_uuid,
                             {"reference": reference, "sent_ct": sent_ct})


def _read_tests(now: datetime):
    cfg = Config.from_env()
    return mailread.read_tests(cfg.mail_username, cfg.mail_password, cfg.mailcheck_address, now)


def _clean_tests(now: datetime):
    cfg = Config.from_env()
    return mailread.clean(cfg.mail_username, cfg.mail_password, cfg.mailcheck_address, now)


_mailcheck = MailCheck(_start_test, _read_tests, _clean_tests)


def email_line(now: datetime) -> str:
    """The sentence a triage alert carries about the email check. Never raises: the triage
    message must not wait on, or fail because of, the email check."""
    try:
        state = Config.from_env().check_state
        if state != "on":
            return messages.email_line(state, None, None, "", now)
        result, last_arrival, note = _mailcheck.latest(now)
        return messages.email_line("on", result, last_arrival, note, now)
    except Exception:  # noqa: BLE001
        return "Email check: result unavailable (the check could not be completed)."


def notify(tick: datetime, carried: dict, own: Optional[Tuple[str, str]], test: bool) -> None:
    """A message is due. The log line goes first, so the copy Cloud Monitoring carries never
    depends on the email; then the service's own email, once, with one retry inside send()."""
    log({"event": "notify", "severity": "WARNING", "test": test, "tick": tick.isoformat(), **carried})
    kind = carried.get("kind")
    try:
        cfg = Config.from_env()
        if own is None or not cfg.own_email_ready:
            log({"event": "own_email", "kind": kind, "test": test, "outcome": "not_configured",
                 "attempts": 0, "recipients": len(cfg.alert_recipients)})
            return
        r = mailer.send(cfg.mail_username, cfg.mail_password, cfg.alert_from, cfg.alert_recipients, own[0], own[1])
        log({"event": "own_email", "severity": "INFO" if r["outcome"] == "sent" else "ERROR", "kind": kind,
             "test": test, "outcome": r["outcome"], "attempts": r["attempts"], "reason": r["reason"],
             "recipients": len(cfg.alert_recipients)})
    except Exception as exc:  # noqa: BLE001
        log({"event": "own_email", "severity": "ERROR", "kind": kind, "test": test, "outcome": "failed",
             "attempts": 0, "reason": type(exc).__name__})


def email_tick(now: datetime, test: Optional[str] = None) -> None:
    """The hourly email check's share of a tick. Never raises and never changes the tick's answer."""
    try:
        cfg = Config.from_env()
        state = cfg.check_state
        tick = tick_floor(now)
        msgs = []
        if state != "on":
            if test in EMAIL_TESTS or (state == "not_configured" and tick.minute == 0):
                log({"event": "mailcheck", "severity": "WARNING", "action": state, "tick": tick.isoformat(),
                     "missing_settings": cfg.missing_for_check()})
            return
        if test == "email_refused":
            ok, reason = textit.start_flow(cfg.textit_token, NO_SUCH_FLOW, cfg.contact_uuid,
                                           {"reference": "TEST-REFUSED"})
            logs = _mailcheck.record_refused_test(now, ok, reason)
        elif test == "email_misses":
            logs, msgs = _mailcheck.force_misses(now)
        else:
            logs, msgs = _mailcheck.step(now)
        for entry in logs:
            log({"event": "mailcheck", **entry})
        for m in msgs:
            subject, body = messages.email_check(m, now)
            notify(m.at, messages.email_check_google(m, subject, body), (subject, body), m.test)
    except Exception as exc:  # noqa: BLE001
        log({"event": "mailcheck", "severity": "ERROR", "action": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]})


def rows_to_requests(rows) -> list[Request]:
    """Rows -> one Request per triage_request_id, MIN of each time.
    The query already groups; this merges again so a split row can never count twice."""
    by_id: dict = {}
    for row in rows:
        rid = row["triage_request_id"]
        req = utc(row["req_ts"])
        det = row["det_ts"]
        det = utc(det) if det is not None else None
        if rid in by_id:
            old = by_id[rid]
            req = min(req, old.req_ts)
            if det is None:
                det = old.det_ts
            elif old.det_ts is not None:
                det = min(det, old.det_ts)
        by_id[rid] = Request(req_ts=req, det_ts=det)
    return list(by_id.values())


def read_requests(now: datetime) -> list[Request]:
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT)
    job = client.query(
        QUERY,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("now", "TIMESTAMP", now)]
        ),
    )
    rows = [dict(r) for r in job.result()]
    return rows


def _ct(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else utc(dt).astimezone(CENTRAL).isoformat()


def run_tick(now: datetime, reader=read_requests, test: Optional[str] = None) -> tuple[dict, int]:
    """One tick. Returns (response body, http status).

    test: None for a normal tick.
      "alert" / "ended"   one triage message of that kind, marked TEST, with the live numbers,
                          in addition to the normal tick. Sent one tick apart on purpose.
      "email_misses"      the email check behaves as if this hour's two tests were missing
                          (TEST "not arriving"); the next scheduled tick sends the TEST all-clear.
      "email_refused"     asks TextIt to start a flow that does not exist; the refusal is logged
                          and no message goes out.
    The triage rule runs first and writes its line; the email check runs after it and can
    neither delay that line nor change the tick's answer.
    """
    now = utc(now)
    body, status = triage_tick(now, reader, test if test in TRIAGE_TESTS else None)
    if test is None or test in EMAIL_TESTS:
        email_tick(now, test)
    return body, status


def triage_tick(now: datetime, reader=read_requests, test: Optional[str] = None) -> tuple[dict, int]:
    check_state = Config.from_env().check_state
    with _lock:
        due = _memory["next_due"]
    had_memory = due is not None
    if had_memory and now < due and not test:
        body = {
            "event": "ran",
            "read": False,
            "reason": "not_due",
            "tick": tick_floor(now).isoformat(),
            "tick_ct": _ct(tick_floor(now)),
            "next_due": due.isoformat(),
            "next_due_ct": _ct(due),
            "alerting": False,
            "test": False,
            "email_check": check_state,
        }
        log(body)
        return body, 200
    try:
        rows = reader(now)
        requests_ = rows_to_requests(rows)
    except Exception as exc:  # noqa: BLE001 — the whole point is to log it
        body = {
            "event": "error",
            "severity": "ERROR",
            "tick": now.isoformat(),
            "tick_ct": _ct(now),
            "error": f"{type(exc).__name__}: {exc}",
            "had_memory": had_memory,
        }
        log(body)
        return body, 500
    d = decide(requests_, now)
    with _lock:
        _memory["next_due"] = d.next_due
    body = {
        "event": "ran",
        "read": True,
        "reason": f"test_{test}" if test else ("no_memory" if not had_memory else "due"),
        "tick": d.tick.isoformat(),
        "tick_ct": _ct(d.tick),
        "requests_in_lookback": len(requests_),
        "last_determination": d.now.last_det.isoformat() if d.now.last_det else None,
        "last_determination_ct": _ct(d.now.last_det),
        "silence_minutes": None if d.now.silence_minutes is None else round(d.now.silence_minutes, 1),
        "waiting": d.now.waiting,
        "in_window": d.now.in_window,
        "alerting": d.now.fires,
        "episode_start_ct": _ct(d.episode_start),
        "kind": d.kind,
        "ended_reason": d.ended_reason,
        "next_due": d.next_due.isoformat(),
        "next_due_ct": _ct(d.next_due),
        "test": test or False,
        "email_check": check_state,
    }
    log(body)
    if test:
        # A TEST alert, then (one tick later, a second call) a TEST ended.
        kind, reason = ("ended", "determination") if test == "ended" else ("alert", None)
        d = replace(d, kind=kind, ended_reason=reason)
    if d.kind:
        line = email_line(now) if d.kind in ("alert", "repeat") else None
        notify(d.tick, compose(d, test=bool(test), email_line=line), messages.triage(d, line, now, test=bool(test)), bool(test))
    return body, 200


@app.get("/health")
def health():
    cfg = Config.from_env()
    return jsonify({
        "status": "ok",
        "service": "triage-monitor",
        "email_check": cfg.check_state,
        "own_email": "configured" if cfg.own_email_ready else "not configured",
    })


@app.post("/tick")
def tick():
    payload = request.get_json(silent=True) or {}
    test = payload.get("test")
    if test is True:
        test = "alert"
    if test not in (None, False) + TRIAGE_TESTS + EMAIL_TESTS:
        return jsonify({"error": "test must be one of: " + ", ".join(TRIAGE_TESTS + EMAIL_TESTS)}), 400
    body, status = run_tick(datetime.now(timezone.utc), test=test or None)
    return jsonify(body), status


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
