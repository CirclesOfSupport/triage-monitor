# triage-monitor

Cloud Run service that alerts us when triage determinations stop while requests are
waiting (ITDO-517), and that checks every hour that an email sent by a TextIt flow still
arrives. It exists because on 2026-09-25 the triage emails stopped reaching the counselors
and nobody knew for about a day.

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

Every alert message ends with one plain sentence saying what it checks and that the monitor
must be updated if the way determinations are recorded changes. The message does not name
the table; this section does.

**The second thing it watches is TextIt's email itself.** The triage rule cannot tell
"counselors are away" from "the triage email never arrived", and TextIt reports nothing
about an email it sends. So once an hour, every hour of the day, the service has TextIt
start one small flow that emails a test to a mailbox the service reads, and looks for it
10 minutes later. A missing test is sent once more; when two in a row are missing the
service sends "EMAIL NOT ARRIVING", reminds every 2 hours while it lasts, and says "EMAIL ARRIVING
AGAIN" once when a test arrives. One late email never alerts, and neither does TextIt
refusing to start the test or the mailbox being unreadable: those are logged as their own
states. Every triage alert and reminder carries one line with this check's latest result.
`EMAIL_CHECK=off` turns it off without a deploy.

**If the way flow emails are sent changes** (TextIt's sender settings, the test flow, the
test address or the mailbox it lands in), re-point the settings listed under
[Settings](#settings-and-the-two-credentials) and run the test switch before trusting it.

**What it does not remember.** The service stores nothing. Whether an email outage is
already "open" is held only in the running instance's memory, and Cloud Run replaces the
instance about once a day (and on every deploy). If that happens in the middle of an email
outage, the next failed hour is announced as a new alert instead of a reminder, and if email
recovers within the hour after the replacement, the "EMAIL ARRIVING AGAIN" message is not sent.
Nothing else is lost: a false "EMAIL NOT ARRIVING" needs two accepted tests and a successful
mailbox read in the same instance, so a replaced instance can only stay quiet, never alarm.
The triage rule is not affected: it recomputes everything from the table on every tick.

## How it runs

- **Cloud Scheduler** job `triage-monitor-tick` calls `POST /tick` every 5 minutes, all day,
  with an OIDC token (the service is `--no-allow-unauthenticated`; no shared secret).
  The same tick drives the hourly email check; there is no second job.
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

## The hourly email check, step by step

The hour is the UTC hour, so a daylight-saving change can neither skip nor double one.
On each 5-minute tick (the minute is floored to the grid, so a tick a minute late changes
nothing):

1. No test started this hour: start one. A new instance first reads the mailbox, so a test
   that already arrived this hour is not sent again. No first test is started after :35.
2. Ten minutes after a start: read the mailbox. The test is there: arriving, done for the
   hour. Not there, and it was the first: start one more.
3. Ten minutes after the second: read again. Either is there: arriving. Neither: not
   arriving.

So a normal hour is one start at :00 and one read at :10; the worst hour ends at :20, and
the longest time from a break to the alert is 80 minutes.

- **The test's reference** is the start's UTC tick time, `EA-MC-<yyyymmdd>T<hhmm>Z`, passed
  to the flow as `@trigger.params.reference` (with `@trigger.params.sent_ct`, the Central
  time in words). The flow's one Send Email puts the reference in its subject. A test only
  counts for the hour it was sent in.
- **The start** is one `POST /api/v2/flow_starts.json`, 8-second limit, no retry inside the
  tick. Anything but 201 is "could not run" with the reason; after three refusals in an
  hour (or one 429) the service stops asking until the next hour.
- **The read** is IMAP over TLS, All Mail (not the inbox: Gmail keeps a message a user sends
  to their own alternate address out of the inbox), only messages addressed to the test
  address, only the To and Subject headers and the time the mailbox received it. No body is
  fetched. After a read that found the hour's test, test messages older than 2 days are
  moved to Trash, which the mail provider empties itself; nothing not addressed to the test
  address is touched.
- **Send-to-arrival seconds** are logged for every test that arrives (`action=seen`,
  `tests[].seconds`, `slowest_seconds`). The 10-minute wait is a constant
  (`WAIT` in `src/mailcheck.py`); change it only on those numbers.
- **The guard against a false alarm:** "not arriving" needs two starts TextIt accepted in
  this hour, both in this instance's memory, each at least 10 minutes old, and a mailbox read
  that succeeded and found neither.

```
resource.type="cloud_run_revision" AND resource.labels.service_name="triage-monitor" AND jsonPayload.event="mailcheck"
```

## Messages

The service composes every message and sends it twice, in this order: one log line
(`event=notify`), which a Cloud Monitoring policy carries as a second copy, and then its own
plain-text email through the Workspace SMTP relay, signed in as the mailbox user (one retry
after 5 seconds; the outcome is one `event=own_email` line: sent or failed, attempts, how
many recipients - never an address or a body). A failed email changes nothing else in the
tick, and the log line has already been written.

The email's subject opens with what it is about and its state, in capitals, so a closing
message cannot be taken for the alert it closes:

| kind | subject |
|---|---|
| `alert` | *[Early Alert] TRIAGE SILENT: 1 h 33 min, 4 requests waiting* |
| `repeat` | *[Early Alert] TRIAGE STILL SILENT: 3 h 33 min, 9 requests waiting* |
| `ended` by a determination | *[Early Alert] TRIAGE RESUMED: determination at 1:23 PM CT* |
| `ended` at 8 PM, still silent | *[Early Alert] TRIAGE STILL SILENT: monitor hours over until 10:30 AM CT, 44 waiting* |
| `ended`, fewer than 3 waiting | *[Early Alert] TRIAGE NOT RESOLVED: alert ended, fewer than 3 requests waiting* |
| `email_alert` | *[Early Alert] EMAIL NOT ARRIVING: TextIt test emails, none since 1:02 PM CT* |
| `email_repeat` | *[Early Alert] EMAIL STILL NOT ARRIVING: TextIt test emails, none since 1:02 PM CT* |
| `email_ended` | *[Early Alert] EMAIL ARRIVING AGAIN: TextIt test email at 4:02 PM CT* |

The words are in `src/messages.py`. The log line keeps its own, older wording
(`rule.compose`), below:

| kind | when | subject |
|---|---|---|
| `alert` | the first tick the rule holds | *Early Alert: triage has been silent 1 h 33 min — 4 requests waiting* |
| `repeat` | every 2 hours while it holds | *Early Alert: triage still silent 3 h 33 min — 9 requests waiting* |
| `ended` | the first tick it no longer holds | *Early Alert: triage resumed — determination at 1:23 PM CT on Sat Aug 15*; or, when the window closes with the alert still on, *Early Alert: triage STILL silent — monitoring hours over until 10:30 AM CT; 44 requests waiting* (its first words say it is not resolved); or *Early Alert: triage NOT resolved — alert ended, fewer than 3 requests waiting* |
| `test` | `POST /tick` with body `{"test": "alert"}`, then 5 minutes later `{"test": "ended"}` | the alert message and the ended message with the live numbers, prefixed `TEST —`, one tick apart on purpose (see the cap below) |

The email-check kinds (`email_alert`, `email_repeat`, `email_ended`) are written to the same
line with the same keys; `last_determination` and `waiting` read "n/a - email check".

The SMS, where a Cloud Monitoring SMS channel is attached, is Cloud Monitoring's fixed text,
which names the policy; Google does not put custom text in SMS and calls SMS best-effort.
The service's own email is the channel of record. Times are Central.

## Settings and the two credentials

All of these are set on the Cloud Run service (`gcloud run services update`), never in this
repository. With none of them set the service behaves exactly as it did before the email
check existed: the triage rule, the log lines, and no email of its own.

| Name | What |
|---|---|
| `EMAIL_CHECK` | `on` or `off` (default `off`). The one switch for the hourly check. |
| `TEXTIT_FLOW_UUID` | the test flow: one Send Email to the test address, subject containing `@trigger.params.reference` |
| `TEXTIT_CONTACT_UUID` | a contact used for nothing else (starting a flow interrupts whatever a contact is in) |
| `MAILCHECK_ADDRESS` | the test address, e.g. `<test-address>@<domain>` |
| `MAIL_USERNAME` | the mailbox user's sign-in address; it reads the mailbox and sends the service's emails |
| `ALERT_FROM` | the From line of the service's emails, e.g. `Name <address>` |
| `ALERT_RECIPIENTS` | who gets the service's emails, comma-separated. Empty: no email of its own. |
| `TEXTIT_TOKEN` | **secret** - a TextIt API token. Secret Manager, mounted with `--set-secrets`. |
| `MAIL_APP_PASSWORD` | **secret** - an app password of the mailbox user, made for this service alone. Secret Manager, mounted with `--set-secrets`. |

- `GET /health` reports `email_check` (`on`, `off`, or `not_configured` when it is switched
  on with a setting missing) and `own_email` (`configured` or `not configured`), never a value.
- Neither credential is ever written to a log line: the log writer strikes out both values
  and every configured address before a line is written (tested).
- A TextIt token is not scoped: whoever holds it can do anything the API allows in the
  workspace. It lives in Secret Manager only.
- **An app password stops working when the account's password is changed** (Google revokes
  all of them) and needs 2-Step Verification on the account. If the mailbox user's password
  is ever changed, this service's emails and its mailbox read stop together until a new app
  password is stored; the "monitor stopped" policy and the Cloud Monitoring copy do not
  depend on it.

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

Recipients of the Cloud Monitoring copies live in Cloud Monitoring, not here.

**Google's cap.** A log-based alerting policy delivers at most 20 notifications a day and
one every 5 minutes (Cloud Monitoring quotas and limits). That cap now bounds only the
second copy: the service's own email has no such limit and is the channel of record. The
triage rule alone cannot reach it: an
episode needs 90 minutes of silence to start and ends on the tick after a determination
lands, so the fastest possible day is six alert/ended pairs — 12 messages; a day on which
the waiting count also fell below 3 by aging out and climbed back costs a cycle slot and
stays at 12 (both measured by `replay/replay.py` on synthetic worst days). Over the
2026-08-01 to 2026-10-02 history the most in one day was 6 (the outage, Sep 26). The
replay fails if any day reaches 20. The test switch sends its two messages 5 minutes apart
so the one-per-5-minutes limit is exercised, not skipped. An email outage lasting a whole
day adds up to 13 more (one alert, a reminder every 2 hours, one all-clear), so on a day
with both at their worst the copy would drop the last five of 25; the emails still go.

## Observability

Every tick writes one `event=ran` line with: whether it read or skipped, the latest
determination (UTC and Central), silence in minutes, waiting count, whether the rule is on,
the episode start, the message kind if any, when the next read is due, and whether the
email check is on. A failed BigQuery read writes an `event=error` line at ERROR severity and
the tick answers 500. The triage rule runs first and writes its line; the email check runs
after it, so nothing in the email check can delay that line or change the tick's answer.

```
resource.type="cloud_run_revision" AND resource.labels.service_name="triage-monitor" AND jsonPayload.event="ran"
```

## Replay and tests

```
python3 -m pytest -q tests
python3 replay/replay.py <history.csv> 2026-08-01T05:00:00Z 2026-10-03T04:55:00Z
```

The tests need nothing but this repository; TextIt, the mailbox and SMTP are replaced by
fakes. The replay needs a history file, which is not
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
Invoke-RestMethod -Uri "https://<service url>/tick" -Method POST -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body '{"test": "email_misses"}'
Invoke-RestMethod -Uri "https://<service url>/tick" -Method POST -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body '{"test": "email_refused"}'
```

To prove the mail route itself from a workstation - one test email sent as the mailbox user
to the test address and read back, with the service's own code - run
`python tools/mail_proof.py --username <mailbox sign-in> --sender "Name <address>" --to <test address>`
(it asks for the app password at a hidden prompt; on Windows install `tzdata` first). Do this
after the app password or the test address changes.

The caller needs Cloud Run Invoker on the service. The four test bodies:

- `alert`, then `ended` 5 minutes later: a TEST triage alert and its TEST "resumed", by log
  line and by email, with the live numbers.
- `email_misses`: the email check behaves as if this hour's two tests were missing. The
  mailbox is really read; a TEST "EMAIL NOT ARRIVING" goes out; the next scheduled tick looks
  again, finds the hour's real test and sends the TEST "EMAIL ARRIVING AGAIN". Run it a few
  minutes after the hour's test has arrived.
- `email_refused`: asks TextIt to start a flow that does not exist. The refusal is logged
  (`action=could_not_run`) and shows in the next triage alert's line; no message goes out.

## Cost

Estimate, to be replaced by the bill: one Scheduler job (~$0.10/month), about 300 BigQuery
reads a month at the 10 MB minimum (cents), Cloud Run request time for 8,640 short ticks a
month (under $1), two alert-policy conditions (~$0.23/month on Google's list price), two
stored secrets (~$0.12/month on Google's list price). The hourly check adds 24 to 48 TextIt
flow starts and test emails a day, a few seconds of request time each hour, and no new job
or policy.
