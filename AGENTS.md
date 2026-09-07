# Don Pollo operating instructions

## Source of truth

- GitHub is the durable source of truth. The production checkout is `/Users/pm/InternManagement` on the office Mac mini.
- Work on an `agent/*` branch, run tests, and publish a pull request. Do not hand-edit tracked production files over SMB.
- Preserve local-only `.env`, `bootstrap.local.json`, `config/`, `data/`, `storage/`, `backups/`, and `secrets/`. Never print their secrets or add them to Git.
- The live configuration and roster are intentionally not tracked. `ops/deploy-branch.sh` applies the reviewed control values with `ops/apply_production_controls.py`, which backs up the prior config first. Make other local-config changes only as an explicit deployment step after validating their parsed form.

## Safety invariants

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

The dashboard should load at `http://192.168.4.87:8765/` over the office VPN. Operational links sent to admins must use the VPN-reachable address, not localhost.

## Deployment

- Confirm GitHub checks and review the diff before deploying.
- From the production checkout, run `ops/deploy-branch.sh agent/<branch>`; it refuses dirty worktrees and non-`agent/*` branches, creates and verifies an encrypted backup, fast-forwards when possible, and otherwise constructs a two-parent reconciliation commit only when both histories descend from GitHub `main`. The reconciliation uses the reviewed candidate tree while preserving production's existing CI workflow, then installs pinned dependencies, restarts services, and runs health checks.
- If deployment verification fails, leave evidence intact and report the exact failed check. Do not reset or discard live data.
