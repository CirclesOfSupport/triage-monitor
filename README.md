# triage-monitor

Cloud Run service that alerts us when triage determinations stop while requests are
waiting (ITDO-517). It exists because on 2026-09-25 the triage emails stopped reaching the
counselors and nobody knew for about a day.

## What it watches, and what to change if the determination mechanism changes

**The signal is one BigQuery table:** `early-alert-responses.RESPONSES.triage-message-data`,
three columns — `triage_request_id`, `triage_request_time`, `determination_time`. One row
per request (grouped by `triage_request_id`, earliest of each time, so a split row never
counts twice).

- **Silence** = minutes since the latest `determination_time`.
- **Waiting** = requests whose `triage_request_time` is at least 30 minutes and at most 14
  hours old and that have no `determination_time`.
- **Alert** when silence is 90 minutes or more AND waiting is 3 or more AND the Central
  clock (`America/Chicago`) is at or after 10:30 AM and before 8:00 PM, every day of the
  week. Nothing outside that window: a break that starts overnight is announced at
  10:30 AM. Alert thresholds come from two months of history (every rule and window was
  back-tested; the chosen one fires on seven normal days in two months and would have
  caught the 2026-09-25 outage at 5:10 PM, 95 minutes after the last determination).

**If the way determinations are recorded ever changes** — a different survey, a different
write path, a different table or column — this monitor must be re-pointed or it will alert
on silence that is not silence (or miss silence that is). The three things to change:

1. `src/main.py` — `TABLE` and the query (`QUERY`): the table and the three column names.
2. `src/rule.py` — the rule constants (`SILENCE_MINUTES`, `WAIT_MIN_MINUTES`,
   `WAIT_MAX_HOURS`, `WAITING_COUNT`, `WINDOW_START`, `WINDOW_END`) if the thresholds need
   recalibrating against the new source; and `CHECKS`, the sentence every message ends with.
3. The replay in `replay/` — re-run it over the new source's history so the thresholds are
   proven again before the change goes live.

Every alert message ends with the same statement of what it checks, so the person reading
the alert knows what to re-point.

## How it runs

- **Cloud Scheduler** job `triage-monitor-tick` calls `POST /tick` every 5 minutes, all day,
  with an OIDC token (the service is `--no-allow-unauthenticated`; no shared secret).
  The fixed tick is deliberate: a service that scheduled its own next run would die silently
  the first time one run failed.
- **The service reads BigQuery only when the alert could possibly be true.** After each read
  it computes the earliest moment worth looking again: the later of (latest determination +
  90 minutes) and (the moment a third undetermined request reaches 30 minutes of waiting;
  with fewer than three undetermined, 30 minutes from now); outside the window, 10:30 AM.
  Looking early is harmless; the formula never looks late (proven by replay — see below).
  That moment is held in memory; a tick before it answers "not due" without touching
  BigQuery; an instance with no memory reads. While an alert is on, it reads every tick,
  because only a read can see the episode end. About 9 reads a day on a normal day.
- **No stored state.** Every decision is a pure function of the tick time and the table:
  the start of an alert episode is found by walking the 5-minute grid back while the rule
  holds; reminders go out on the ticks 2 hours, 4 hours, … after the start; the episode
  ends on the first tick where the rule no longer holds. A hot instance and a cold one
  behave identically, and the deployed function is the one replayed over history.
- **Known bound:** the table is written by add-to-db, which flushes to BigQuery every
  ~2 minutes, so a determination can land in the table up to ~2 minutes after its
  `determination_time`. At an episode's start or end that can shift one message by one tick,
  or (rarely) produce a duplicate alert or a missing "ended" line. Not worth a state store.

## Messages

The service composes every message; Cloud Monitoring only carries it. One log line
(`event=notify`) is written when a message is due:

| kind | when | subject |
|---|---|---|
| `alert` | the first tick the rule holds | *Early Alert: triage has been silent 1 h 33 min — 4 requests waiting* |
| `repeat` | every 2 hours while it holds | *Early Alert: triage still silent 3 h 33 min — 9 requests waiting* |
| `ended` | the first tick it no longer holds | *Early Alert: triage resumed — determination at 1:23 PM CT on Sat Aug 15*; or, when the window closes with the alert still on, *Early Alert: triage STILL silent — monitoring hours over until 10:30 AM CT; 44 requests waiting* (its first words say it is not resolved); or *… alert ended — fewer than 3 requests waiting* |
| `test` | `POST /tick` with body `{"test": "alert"}`, then 5 minutes later `{"test": "ended"}` | the alert message and the ended message with the live numbers, prefixed `TEST —`, one tick apart on purpose (see the cap below) |

The email subject and body are the service's words (subject, headline, last determination,
waiting count, what it checks). The SMS is Cloud Monitoring's fixed text, which names the
policy; Google does not put custom text in SMS, and Google itself calls SMS best-effort, so
the email is the channel of record. Times are Central.

## Alerting (Cloud Monitoring, `monitoring/`)

- `policy_triage_silent.json` — a log-based alerting policy on the `notify` line. Each
  distinct message is delivered (Cloud Monitoring treats each combination of extracted
  labels as its own rule), so the cadence is the service's: once at the start, every 2 hours,
  once at the end.
- `log_metric_triage_monitor_ran.txt` — the filter for the log-based counter metric
  `triage_monitor_ran`, one count per tick (read or skipped).
- `policy_triage_monitor_stopped.json` — a metric-absence policy: no tick counted for
  45 minutes means the monitor itself is dead (Scheduler stopped, service failing). It
  notifies when it opens and when it closes. Series are summed across revisions so a deploy
  does not look like a stop. It sets no custom subject: a metric policy cannot put the
  incident state into a custom subject, so the "stopped" and "recovered" emails would share
  it; with Google's default subject the two are told apart in the inbox.
- The policy files are plain ASCII on purpose: `gcloud` on Windows reads them in the
  system code page, and an em dash in a display name arrived in Google as `?`.

Recipients (one email channel, four SMS channels) live in Cloud Monitoring, not here.

**Google's cap.** A log-based alerting policy delivers at most 20 notifications a day and
one every 5 minutes (Cloud Monitoring quotas and limits). The rule cannot reach it: an
episode needs 90 minutes of silence to start and ends on the tick after a determination
lands, so the fastest possible day is six alert/ended pairs — 12 messages; a day on which
the waiting count also fell below 3 by aging out and climbed back costs a cycle slot and
stays at 12 (both measured by `replay/replay.py` on synthetic worst days). Over the
2026-08-01 to 2026-10-02 history the most in one day was 6 (the outage, Sep 26). The
replay fails if any day reaches 20. The test switch sends its two messages 5 minutes apart
so the one-per-5-minutes limit is exercised, not skipped.

## Observability

Every tick writes one `event=ran` line with: whether it read or skipped, the latest
determination (UTC and Central), silence in minutes, waiting count, whether the rule is on,
the episode start, the message kind if any, and when the next read is due. A failed
BigQuery read writes an `event=error` line at ERROR severity and the tick answers 500.

```
resource.type="cloud_run_revision" AND resource.labels.service_name="triage-monitor" AND jsonPayload.event="ran"
```

## Replay and tests

```
python3 -m pytest -q tests
python3 replay/replay.py <history.csv> 2026-08-01T05:00:00Z 2026-10-03T04:55:00Z
```

The tests need nothing but this repository. The replay needs a history file, which is not
in this repository: export the service's query (`QUERY` in `src/main.py`, over the date
range you want) to a CSV with two integer columns, `req_epoch` and `det_epoch` (epoch
seconds; `det_epoch` blank when undetermined), and pass its path. The replay feeds the
deployed `decide()` function the history at every 5-minute tick (each tick sees the last 3
days, exactly as the service's query returns them), prints every alert, reminder and ended
message it would have sent, repeats the run with the read-only-when-due schedule and checks
the two fire on identical ticks, then checks the daily cap.

Result on that history (63 days): seven normal episodes — Sun Aug 9 (11:35 AM and 5:55 PM),
Sat Aug 15 (11:05 AM and 7:25 PM), Sun Aug 16 (10:30 AM), Sun Aug 23 (2:25 PM), Thu Aug 27
(5:40 PM) — and the outage: Fri Sep 25 at 5:10 PM and Sat Sep 26 at 10:30 AM. No others.
Read-when-due: 812 reads over 18,144 ticks, identical events.

## Running it by hand

```powershell
$token = gcloud auth print-identity-token
Invoke-RestMethod -Uri "https://<service url>/health" -Headers @{ Authorization = "Bearer $token" }
Invoke-RestMethod -Uri "https://<service url>/tick" -Method POST -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body '{}'
Invoke-RestMethod -Uri "https://<service url>/tick" -Method POST -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body '{"test": "alert"}'
Invoke-RestMethod -Uri "https://<service url>/tick" -Method POST -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body '{"test": "ended"}'
```

The caller needs Cloud Run Invoker on the service.

## Cost

Estimate, to be replaced by the bill: one Scheduler job (~$0.10/month), about 300 BigQuery
reads a month at the 10 MB minimum (cents), Cloud Run request time for 8,640 short ticks a
month (under $1), two alert-policy conditions (~$0.23/month on Google's list price).
