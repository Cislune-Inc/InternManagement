# Don Pollo operating instructions

## Source of truth

- GitHub is the durable source of truth. The production checkout is `/Users/pm/InternManagement` on the office Mac mini.
- Work on an `agent/*` branch, run tests, and publish a pull request. Do not hand-edit tracked production files over SMB.
- Preserve local-only `.env`, `bootstrap.local.json`, `config/`, `data/`, `storage/`, `backups/`, and `secrets/`. Never print their secrets or add them to Git.
- The live configuration and roster are intentionally not tracked. `ops/deploy-branch.sh` applies the reviewed control values with `ops/apply_production_controls.py`, which backs up the prior config first. Make other local-config changes only as an explicit deployment step after validating their parsed form.

## Safety invariants

- Worker-facing messages should be brief, friendly and directive: state the
  action, timing and next step. Do not append repetitive legal disclaimers,
  suggestions about fictional compliance, or actual-hours reporting boilerplate
  to routine clock/break/work replies. Keep corrections easy to find in help and
  relevant error/review flows; retain all underlying time and approval controls.

- The coding assistant must not post to Slack or send email without Erik's explicit
  send instruction in the current request. Draft confirmation is not publication
  permission. Do not send general status noise. Hand off website, Drive or GitHub
  links rather than local Markdown/text files. Runtime private replies to worker
  actions and accepted clock notices are separate from assistant announcements.
- Onsite clock-in requires confirmation at the Mini's loopback-only
  kiosk, not a Slack self-attestation, GPS claim, QR link or VPN source address.
  Use name + personal PIN, not temporary Slack codes. PINs are salted/scrypt-hashed;
  accept 2–6 digits with four or more recommended and keyboard entry, no onscreen
  keypad. Preserve existing PINs and durable lockouts when changing the UI.
  never put them in chat, clock events, logs, browser storage or URLs. Setup/reset
  requires the worker's authenticated Slack identity or a verified local operator.
  Keep the owner company-management remote exception visible; staff remote work
  still requires Erik's time-bounded advance approval. Remote desktop/SSH access
  to the kiosk must remain limited to trusted administrators.
  September 8 afternoon supersedes kiosk-only break returns: Slack lunch/rest/back
  may resume an existing recorded break after minimum-time and authorization
  checks. New shifts still require the kiosk or approved remote path. Preserve
  clock-out and actual-hours reporting as safety fallbacks. Completed lunch edits
  require actor-bound, expiring previews, explicit confirmation and original-record
  audit; never infer an unpaid meal from an expected schedule or model output.
- Block early return for 10-minute paid rest and 30-minute lunch only, not early
  scheduled shift starts. Queue one private Slack readiness notice per break;
  no reply is required during rest and no timer automatically resumes work.
  Suppress delayed readiness messages after return/clock-out. Keep actual-hours
  reports and clock-out available even while the return boundary is active.

- September 8 evening: a required paid rest gets one readiness notice, not an
  automatic ten-minute clock-out. Extra short pauses have no new minimum and do
  not count as required rests. Preserve actual return windows and existing legacy
  gaps. Use gentle return-or-clock-out wording; no blanket unpaid extension rule.
  Extended completed pauses are reviewable, not automatic pay deductions. Routine
  break-pattern monitoring is private to Erik and focuses on material weekly
  patterns, not small variations or staff warnings.

- The opt-in hours-first Slack beta (`docs/slack-work-intake-beta.md`) is the sole
  beta clock on the existing SQLite session ledger. Gusto Kiosk is not a fallback;
  workers can always report actual hours to Erik on Slack. Clocking must not depend
  on ClickUp, OpenAI, photos, prose quality or work-plan approval. Keep API coaching
  bounded and advisory. Preserve original statements and versioned decisions. A project
  label is not approval, and work approval is not contract-charge, remote-work or
  overtime approval. Quality/repetition coaching must not erase worked time.

- A tracked worker is warned before the meal deadline, automatically paused at the configured deadline, and notified. Do not invent a flat meal deduction.
- A covered worker is warned before the overtime limit and automatically clocked out at the limit unless approval is already stored.
- Inactivity warnings must remain stateful and resilient to scheduler delays; do not rely on a one-minute scheduling window.
- New ClickUp project, overhead, blocker, and unblocker tasks require management approval when `clickup.new_task_approval_required` is enabled. Notify the configured approvers (normally Erik and George) before creation.
- Legacy public project updates require an active ClickUp task and a confident project-channel route. The new beta captures freeform work privately and disables legacy daily/weekly posts; do not let stale ClickUp routes publish unreviewed beta notes.
- September 8 accepted interaction: bounded private two-hour check-ins and explicit
  worker sharing of a versioned preview to owner-approved project channels. Private
  confirmation alone is not sharing. Keep routes explicit, suppress duplicates,
  preserve uncertain delivery for review, and never forward arbitrary DMs or time data.
  Team onboarding send is authorized only after current cohort and access readiness.
  Enrollment must name each worker; never default to all active legacy rows or
  enroll a secondary manager merely because they have an admin profile.
- Preserve vague or repeated beta statements, asking one useful question where needed; never reject time records for prose quality. Original legacy task/photo posting rules do not gate the beta clock.
- Automatic stop instructions are not proof that work stopped. Keep unconfirmed gaps reviewable and actual-hours reports visible in payroll exports, even without a recorded shift. Ordinary worker clock-outs are not manager exceptions.
- Routine operational warnings belong in the digest. Send immediate Slack alerts only for errors and critical failures.

## Verification

September 8 latest owner correction: use the currently logged-in Mac account;
do not create or require a separate macOS kiosk user. Keep the protected manager
HTTP boundary, PIN hashes and trusted-admin remote-access rules. This shared
administrator session is not OS-level isolation; do not claim otherwise.
Setup-only cohort enrollment may show verified names and permit PIN setup while
actual Gusto handover remains pending. It must not start time or silently clear
a handover hold; preserve prior work/break continuity before enabling starts.

The manager editor is loopback-only and requires its private credential even from
localhost. Access it remotely through trusted SSH, never expose Basic auth on the
LAN. `/livez` is process readiness only; detailed `/health` and `/api/health` remain
authenticated. Do not confuse a passing liveness check with bot/integration health.
Keep config/data/storage/backups/secrets private to the service account. Standard
kiosk users must not gain SSH/screen-sharing or manager access. Owner remote clock
authorization remains independent of these manager-web controls.

Run from the repository root:

```bash
.venv/bin/python -m compileall -q agent tests
.venv/bin/python -m pytest -q
```

For production verification on the Mac mini:

```bash
ops/verify-services.sh
PYTHONPATH=. .venv/bin/python ops/print-health-summary.py
```

The Mini was verified at `192.168.40.177` on September 7. Revalidate its saved SSH
host key and address before deployment. Operational links must use the verified
internal origin supplied with `--base-url`, not localhost or a hard-coded old IP.

## Deployment

- Confirm GitHub checks and review the diff before deploying.
- From the production checkout, run `ops/deploy-branch.sh agent/<branch>`; it refuses dirty worktrees and non-`agent/*` branches, creates and verifies an encrypted backup, fast-forwards when possible, and otherwise constructs a two-parent reconciliation commit only when both histories descend from GitHub `main`. The reconciliation uses the reviewed candidate tree while preserving production's existing CI workflow, then installs pinned dependencies, restarts services, and runs health checks.
- If deployment verification fails, leave evidence intact and report the exact failed check. Do not reset or discard live data.
- Disabled/unloaded jobs require explicit `--enable-disabled` after candidate and
  cohort verification. Use `--primary-admin-only --primary-admin-slack-id ID` for
  an isolated owner pilot; do not enroll old workers as a deployment side effect.
- Historical open shifts may be deferred only with the owner's explicit approval
  and `--defer-primary-legacy-before YYYY-MM-DD`. This preserves originals and
  unresolved-hours reports; it does not supply guessed ends or forgive worked time.
