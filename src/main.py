"""triage-monitor — alerts when triage determinations go silent while requests wait.

One endpoint does the work. Cloud Scheduler calls POST /tick every 5 minutes,
all day. The service reads BigQuery only when the alert could possibly be true
(the next-read-due moment kept in memory; an instance with no memory reads),
writes one structured log line per tick, and writes a `notify` line whenever a
message is due. Google Cloud Monitoring turns those lines into email and SMS;
no addresses or phone numbers live here.

Log lines (jsonPayload):
  event=ran     every tick: read|skipped, the numbers, next_due
  event=notify  a message is due: kind, subject, headline, last_determination, waiting, checks
  event=error   the BigQuery read failed (the tick answers 500)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, request

from rule import CENTRAL, Request, compose, decide, utc

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


def log(payload: dict) -> None:
    """One JSON line to stdout; Cloud Run ingests it as jsonPayload."""
    payload.setdefault("severity", "INFO")
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


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

    test: None for a normal tick; "alert" or "ended" writes one notify line of that kind,
    marked TEST and carrying the live numbers, in addition to the normal tick. The two are
    sent one tick apart on purpose (a message every 5 minutes is the policy's rate limit).
    """
    now = utc(now)
    with _lock:
        due = _memory["next_due"]
    had_memory = due is not None
    if had_memory and now < due and not test:
        body = {
            "event": "ran",
            "read": False,
            "reason": "not_due",
            "tick": now.isoformat(),
            "tick_ct": _ct(now),
            "next_due": due.isoformat(),
            "next_due_ct": _ct(due),
            "alerting": False,
            "test": False,
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
    }
    log(body)
    if test:
        # Row 9a: a TEST alert line, then (one tick later, a second call) a TEST ended line.
        from dataclasses import replace

        kind, reason = ("ended", "determination") if test == "ended" else ("alert", None)
        msg = compose(replace(d, kind=kind, ended_reason=reason), test=True)
        log({"event": "notify", "severity": "WARNING", "test": True, "tick": d.tick.isoformat(), **msg})
    else:
        msg = compose(d)
        if msg:
            log({"event": "notify", "severity": "WARNING", "test": False, "tick": d.tick.isoformat(), **msg})
    return body, 200


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "triage-monitor"})


@app.post("/tick")
def tick():
    payload = request.get_json(silent=True) or {}
    test = payload.get("test")
    if test is True:
        test = "alert"
    if test not in (None, False, "alert", "ended"):
        return jsonify({"error": 'test must be "alert" or "ended"'}), 400
    body, status = run_tick(datetime.now(timezone.utc), test=test or None)
    return jsonify(body), status


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
