# Tuesday, September 8: Slack clock launch packet

Prepared September 7, 2026. Internal operator packet, not a rollout announcement.
Erik requested preparation now for a fast implementation tomorrow morning.
Current baseline is draft PR #3, stacked on agent/don-pollo-plan-first-quality.
No production access, deployment, roster enrollment or real API test is implied.

## Recommendation

Launch the existing Slack clock first. Do not make a new website, full ClickUp
replacement, contract decomposition or monitoring a launch dependency. Preserve
actual hours before improving descriptions. When DP fails, workers Slack Erik
actual dates/start/end/breaks; no Gusto Kiosk or duplicate Discord clock.

## Morning order

Times are planning allowances from restored access, not promised completion.

| Stage | Allowance | Owner | Exit evidence |
|---|---|---|---|
| Access and freeze | 10–15 min | Codex; Erik for physical access | Correct Mini/checkout, no concurrent George edits, live services and current shifts identified |
| Readiness and backup | 15–20 min | Codex; Erik resolves roster/policy choices | Read-only preflight, roster identities, actual workday/week, recoverable encrypted backup |
| Reviewed deployment | 15–25 min | Codex with Erik release decision | Exact reviewed tree, compatible live changes preserved, Slack-only service healthy |
| Live acceptance with Erik | 10–15 min | Erik / Codex | Actual clock record/reply, restart durability, saved description and real API answer, export matches actual time |
| Staff cutover | 10–15 min | Erik / Codex | Verified selected identities, one announcement at recorded time, each worker confirms access |

If access, migration or identity reconciliation takes longer, do not invent a
green result to meet the estimate. Keep the explicit actual-hours fallback.
If Erik starts before the team, excluded workers must know to send actual hours
to Erik during that interval; no unnoticed timekeeping gap.

## Before any live write

1. On the Mini, confirm Remote Login, user/host identity and the production path
   /Users/pm/InternManagement. Inspect branch/status and compare to the candidate;
   do not reset/stash concurrent work. This machine remains the editing owner.
2. Verify the active roster, Slack account-to-worker mappings and primary admin.
   Old intern membership is not a current roster. Preserve original hour records.
3. Inspect bootstrap locally for the actual state_db_path. Run the read-only check
   below against that path, using --user-key only for an explicitly limited cohort.
   The known historical default is data/agent_state.sqlite3; do not assume it.
4. Verify clocks already running and any external ClickUp timers. Agree the exact
   handover moment; preserve pre-cutover work. Reconcile duplicate open shifts.
5. Verify configured workday/week and worker-specific treatment. Keep hourly,
   stipend and contractor totals identifiable. Missing payroll mapping is a
   payroll issue, not a reason to lose a worker's actual-time report.
6. Review backup contents/restoration evidence and code diff. Run deployment only
   through AGENTS.md's process. PR #3 currently has no listed remote CI checks;
   absent checks are not a green CI result. Run the candidate's tests explicitly.

Read-only command, replacing the database argument with the verified path:

```sh
PYTHONPATH=. .venv/bin/python ops/slack_beta_preflight.py --state-db VERIFIED_DB_PATH
```

This reads private config/env locally but prints only credential-presence flags,
counts and readiness findings. It does not validate tokens with remote APIs, send
messages, enroll workers, create a missing database or change time. Exit 2 means
blocking findings; exit 0 still requires live acceptance. Inspect private roster
details locally; do not paste credentials into chat or PR comments.

## Small acceptance matrix

| Check | Local rehearsal | Live proof tomorrow |
|---|---|---|
| Start/stop without ClickUp or AI | Existing adapter + ledger tests | Erik records a real short work interval; displayed/exported time agrees |
| Slack button/event retry | Same event applied once | Confirm one record after reconnect/retry, not two starts |
| Restart | Ledger reload tests | Same current shift/totals after controlled service restart |
| Meals/rest/limits | Simulated time, no waiting hours | Verify real command delivery and state without inventing time or test breaks in payroll |
| Missing hours | Raw claim survives absent/conflicting shift | Submit a clearly described real correction if needed; reconcile without guessed timestamps |
| Failed notification | Durable outbox tests | Confirm retry/error visibility in isolated test configuration; review gaps before payroll |
| OpenAI | Mocked success/failure, budget and schema tests | Real bounded reply with existing private key/model access; raw description still saved |
| Payroll | DB survives missing archive; claims included | Review export, actual hours, classifications and approved Gusto entry method |
| Authorization | Worker cannot use manager tools | Confirm Erik/George roles and verified worker mapping; no public hours exposure |

Run accelerated tests only against temporary test databases. Never simulate an
eight-hour workday, edit system time or insert fictional worker breaks into the
production ledger. A short real clock test is sufficient for the live transport;
the boundary scenarios are tested with simulated time offline.

Offline rehearsal:

```sh
.venv/bin/python -m compileall -q agent tests ops/slack_beta_preflight.py
.venv/bin/python -m pytest -q
zsh -n ops/deploy-branch.sh ops/verify-services.sh ops/restart-services.sh
```

## Release and aftercare

- Apply backed-up cohort config only after identity review. Verify actual Slack
  responses before announcing. Do not claim a new Site/link exists.
- Record the cutover timestamp and complete active-roster list locally. Verify
  every person can find DP in Slack and request hours. Resolve gaps immediately.
- Review automatic-stop gaps and pending claims at the end of the first day.
  Do not silently mark unconfirmed time as unpaid. Routine successful clock-outs
  do not need an admin notification; consequential failures do.
- Before the next payroll, reconcile DP claims plus direct messages to Erik;
  confirm employee versus contractor import/entry and actual payroll deadline.
  No automated Gusto submission is delivered. Never lock the active day's hours.
- If service fails, preserve database/backups and use actual-hours messages to
  Erik. Do not restore an old DB over new hours or silently reactivate Discord.

## Worker announcement — UNSENT, send only after verified cutover

Don Pollo in Slack is now our time clock. Open the app's Home tab for buttons,
or message it “clock in onsite,” “clock out,” or “hours.” No ClickUp task search
is needed to record time. Then tell DP the project and what you plan to accomplish;
it may ask one useful follow-up.

Use “lunch” when your meal actually starts and “back” when you return. Use “break”
for paid rest and “back” on return. Follow the reminders and stop-work notices;
remote work and extra hours need my advance approval.

If the clock or a record is wrong, use “report hours” with the actual times, or
message me directly. Record all work actually performed—don't change times to
make them look compliant. Gusto Kiosk and Discord are not our time clocks.

Please open DP and confirm you can see your hours. If you can't, message me now.

## After launch: proposed small web surface, not accepted replacement architecture

Recommendation: keep Slack for clock controls and reminders. A mobile-friendly
web surface could add (1) My day with the same authoritative clock state,
(2) rough weekly availability, and (3) manager decisions/project outcomes.
Use the same backend authority, not a second independent attendance database.

Sites is a candidate for an internal manager/project view. Its documented sign-in
and sharing may help, but a hosted Site is not automatically connected to the
Mini's private VPN/network. A real connection needs an approved authenticated
service boundary or an explicitly read-only, timestamped data snapshot. Do not
expose the Mini's raw dashboard to the public internet to shortcut integration.
The Python process/SQLite clock is not a drop-in Sites deployment. Sites currently
has no data-residency support; do not move controlled project material or sensitive
payroll data there without checking the company's requirements. External visitor
access must be tested against actual account capabilities.

Start with a private synthetic-data preview only if Erik wants to evaluate this
interface. No employee data, live time mutations or automatic approval buttons
until identity, access and backend behavior are verified. This packet does not
authorize publishing a Site or moving the payroll ledger to a cloud database.

For program management, propose one compact card per contract: accepted outcome,
next demonstrable MVP, owner, short internal target, dependencies, evidence link
and next customer-feedback opportunity. Read the registered controlling project
context before populating real milestones. ClickUp remains a reference while
Erik decides whether this captures its useful functions. Do not invent deadlines
or assign engineering work as a side effect of the time-clock launch.

Official Sites capabilities and limitations checked September 7:
https://learn.chatgpt.com/docs/sites
