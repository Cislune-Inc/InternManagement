# Don Pollo hours-first Slack beta — 2026-09-07

Erik's latest direction supersedes the earlier description-only slice: **DP is
the beta time clock; Gusto Kiosk is out.** Hours come first, understanding work
second. ClickUp is an optional reference, not a clock dependency or an accepted
program plan. This branch implements that change, but is not deployed.

## Worker flow

Slack App Home puts clock controls first, no more than five buttons per row.
The Messages tab accepts deterministic commands:

| Command | Result |
|---|---|
| `clock in onsite` | Attest onsite and record start; no task/photo/prose gate |
| `clock in onsite GRASP: compare wheel-slip runs` | Record start first, then capture work |
| `clock out` | Stop immediately without a required summary |
| `lunch` / `back` | Actual meal start/return; authorize return after 30 minutes |
| `break` / `back` | Paid rest and actual return |
| `hours` | Current state, day and week totals |
| `report hours <actual dates, times, breaks, correction>` | Preserve a claim for reconciliation, including a missing shift |

If DP fails, Slack Erik actual hours. Do not use Gusto Kiosk or a second Discord
clock. Unenrolled workers are told to contact Erik with actual time. The old
task-gated portal cannot mutate an enrolled worker's beta clock.

Ordinary text captures a first work proposal or updates the existing proposal.
Original words are saved before optional AI processing. `work <project>: ...`
starts a proposal; `work detail ...`, `work update ...`, `work status` and
`work options` provide explicit controls. Options show up to five previously
approved plans, not a live task-assignment engine. Work descriptions never grant
scope, contract charging, remote or overtime approval.

Labels: GRASP, CISORT, CITA/TRUST, Bagworm, upcoming CLASP and overhead covering
shop, meetings, proposals, sales, finance, people, operations, DP and exploration.
These are Erik's routing labels, not verified contract charging dates.

## OpenAI assistance

The backend uses the Responses API with strict Structured Outputs. Set
`OPENAI_API_KEY` in the Mini's private .env; never commit or paste it into chat.
`OPENAI_WORK_ASSISTANT_MODEL` overrides the default `gpt-6-astra`. Verify actual
project/model access during deployment; local tests mock the API.

AI summarizes supported facts, asks at most one useful follow-up and suggests up
to five next steps. It cannot mutate time, approve work or decide compensation.
Only the submitted description and bounded optional ClickUp reference text are
sent. No contract-document crawling, screenshots or audio are enabled. Absent
controlling sources, alignment stays unverified. Consult the accepted project
plan/SOW/customer direction with Erik/George before claiming alignment.

Requests use `store=False`, bounded input/output, timeouts, no automatic SDK retry,
cached responses and caps of 20 requests per worker/day and 200 globally/day.
These are request caps, not dollar caps; configure API project spend alerts too.
An absent key, timeout, refusal, invalid/incomplete response or exhausted budget
falls back to saved original text. Clocking stays independent. `store=False`
does not itself establish zero retention; use approved company API settings and
data-handling policy. A real model/key roundtrip remains unverified.

## Enforcement and truthful reconciliation

- Onsite is an explicit worker attestation, **not verified physical presence**.
  VPN/IP alone cannot establish presence. Remote work requires advance approval.
- Daily cap uses configured `labor.overtime_limit_hours`; weekly cap is 40 hours,
  with a seventh-consecutive-workday guard. Remote/overtime approvals require Erik,
  a reason, a window of at most seven days and 24 hours lead.
- Company-management mode uses the existing admin profile, not an inferred legal
  exemption or a change to worker payroll classification.
- Meal reminders precede five/ten worked hours. Automatic stops do not invent a
  meal deduction. Only actual reported meal windows are deducted; short reported
  meals are held paid pending review. Late, missed, interrupted and uncertain time
  remains reportable. Never request a compliant-looking fiction.
- Paid rest stays paid. After ten minutes without return acknowledgement the clock
  stops at the actual scheduler tick, never backdated. Actual return is required
  before restarting. Unconfirmed work after automatic stops needs reconciliation.
- Inactivity check is after four hours plus a 15-minute warning window. Scheduler
  delay never retroactively deletes hours. Worker messages count as activity;
  silence or weak prose does not establish nonwork.
- Notices have a durable retry outbox. Slack delivery can fail after a stop:
  **a queued/delivered notice is not worker confirmation**. Automatic-stop events
  require actual-time review before payroll. Undelivered notices/gaps are a live
  acceptance check, not fully automated payroll approval.

The beta uses the configured company timezone, midnight workday boundaries and
a Monday-start week. Verify these match the established payroll workday/week
before activation. Manager corrections require UTC offsets. Cross-midnight shifts
are flagged for splitting. Daily classifications are drafts; weekly/seventh-day
overtime, premiums and worker-specific rules require review. This is not a claim
of comprehensive labor-law automation.

## Manager flow

Erik/George can use:

- `work queue`, `work review DP-id`, `work approve DP-id REVISION REASON`,
  `work redirect DP-id REVISION NEXT_STEP`. Changed plans invalidate old approval.
- `hours reports` lists pending actual-hours claims, five at a time.
- `hours add USER_KEY START_ISO END_ISO REASON` records actual past work.
  Overlap counts once, originals remain; it does not invent break times.
- `hours resolve REPORT_ID OUTCOME` records a reconciliation outcome, not hours.
  Correct actual time before resolving.
- Erik only: `hours authorize USER_KEY remote|overtime START_ISO END_ISO REASON`.

The manager exception page includes proposals. Workers see decisions with
`work status`; push notifications and friendlier approval buttons remain follow-up
UX. Removal/splitting or meal amendments use the existing audited manager hours
editor after checking original evidence.

Payroll export prefers SQLite beta sessions even if file archival failed. All
unresolved raw claims are included, even without a shift or with receipt dates
outside the requested week. Reconcile these with reports sent directly to Erik.
Review classifications/Gusto mappings explicitly; deployment no longer guesses
them. Contractor/stipend treatment is not inferred from a name. Gusto bundles are
**review-only, never automatically submitted**. Confirm Monday payroll processing
deadlines and the applicable entry method before the first bundle.

## Deployment and hard cutover

1. Restore Mini SSH; verify /Users/pm/InternManagement, live Git/config/roster,
   services and open local/ClickUp timers. Preserve George's concurrent changes.
   Never overwrite live .env, roster or state.
2. Review PR/CI. Follow AGENTS.md for encrypted backup, deployment and verification.
   This branch is stacked on `agent/don-pollo-plan-first-quality`, not old main.
3. Verify Slack bot/app tokens, app Home/interactivity, OpenAI key/model access
   without exposing secrets. No Discord token/login is required with a nonempty
   verified Slack cohort.
4. Dry-run `PYTHONPATH=. .venv/bin/python ops/enable_slack_clock_beta.py`.
   Default selects all active roster workers plus mapped admins. Repeat
   `--user-key` to limit testers. Missing/duplicate mappings fail. Verify identities,
   classifications and every excluded worker's actual-hours fallback.
5. At the agreed switch, rerun with `--apply` and restart. It backs up config,
   enables the cohort and disables old daily/weekly posts. A nonempty
   `slack.work_intake_beta_slack_user_ids` selects the Slack-only runtime.
   Old Discord and enrolled-beta web clock writes are not accepted.
6. With Erik, verify actual Slack start/stop, restart/retry idempotency, lunch,
   paid rest, totals, a correction, manager review, a real OpenAI response and an
   export matching actual hours. Test a failed notice and recovery. Local mocked
   tests are not this live acceptance test.
7. Announce one hard cutover only after the clock works. No Kiosk coexistence.
   If DP fails, workers Slack Erik actual hours while it is repaired. Preserve all
   new ledger/additive tables. Code rollback requires time reconciliation and an
   explicit replacement-clock decision, not silently restarting Discord.

## Next program-management work

Once the clock is stable, assess ClickUp against actual use rather than cloning
its features. A lean baseline is: accepted outcome, next demonstrable MVP, owner,
short target date, dependencies, next evidence/customer-feedback step and approved
alternatives. Codex can research/decompose plans; DP captures evidence and asks
questions. A replacement/pivot needs Erik's decision and retention of task history.
Weekly availability UI, authoritative project-source retrieval, Gantt/dependencies
and device capture are not delivered here.

No screenshots, cameras or audio were enabled. Before capture, define purpose,
coverage, retention, access, notice/consent and protection of credentials/incidental
sensitive content. Company ownership/signage is not blanket permission for
confidential audio. Never infer wage deductions from monitoring.

Primary references checked September 7, 2026:

- https://developers.openai.com/api/docs/guides/structured-outputs
- https://www.dir.ca.gov/dlse/FAQ_RestPeriods.htm
- https://www.dir.ca.gov/dlse/faq_mealperiods.htm
- https://www.dir.ca.gov/dlse/faq_overtime.htm
- https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=PEN&sectionNum=632.
