# Don Pollo operating instructions

## Source of truth

- GitHub is the durable source of truth. The production checkout is `/Users/pm/InternManagement` on the office Mac mini.
- Work on an `agent/*` branch, run tests, and publish a pull request. Do not hand-edit tracked production files over SMB.
- Preserve local-only `.env`, `bootstrap.local.json`, `config/`, `data/`, `storage/`, `backups/`, and `secrets/`. Never print their secrets or add them to Git.
- The live configuration and roster are intentionally not tracked. `ops/deploy-branch.sh` applies the reviewed control values with `ops/apply_production_controls.py`, which backs up the prior config first. Make other local-config changes only as an explicit deployment step after validating their parsed form.

## Safety invariants

- The coding assistant must not post to Slack or send email without Erik's explicit
  send instruction in the current request. Draft confirmation is not publication
  permission. Do not send general status noise. Hand off website, Drive or GitHub
  links rather than local Markdown/text files. Runtime private replies to worker
  actions and accepted clock notices are separate from assistant announcements.
- Onsite clock-in, meal return and paid-rest return require confirmation at the Mini's loopback-only
  kiosk, not a Slack self-attestation, GPS claim, QR link or VPN source address.
  Use name + personal PIN, not temporary Slack codes. PINs are salted/scrypt-hashed;
  never put them in chat, clock events, logs, browser storage or URLs. Setup/reset
  requires the worker's authenticated Slack identity or a verified local operator.
  Keep the owner company-management remote exception visible; staff remote work
  still requires Erik's time-bounded advance approval. Remote desktop/SSH access
  to the kiosk must remain limited to trusted administrators.
- Block early return for 10-minute paid rest and 30-minute lunch only, not early
  scheduled shift starts. Queue one private Slack readiness notice per break;
  no reply is required during rest and no timer automatically resumes work.
  Suppress delayed readiness messages after return/clock-out. Keep actual-hours
  reports and clock-out available even while the return boundary is active.

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
- Preserve vague or repeated beta statements, asking one useful question where needed; never reject time records for prose quality. Original legacy task/photo posting rules do not gate the beta clock.
- Automatic stop instructions are not proof that work stopped. Keep unconfirmed gaps reviewable and actual-hours reports visible in payroll exports, even without a recorded shift. Ordinary worker clock-outs are not manager exceptions.
- Routine operational warnings belong in the digest. Send immediate Slack alerts only for errors and critical failures.

## Verification

Run from the repository root:

```bash
.venv/bin/python -m compileall -q agent tests
.venv/bin/python -m pytest -q
```

For production verification on the Mac mini:

```bash
ops/verify-services.sh
curl --fail --silent http://127.0.0.1:8765/health
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
