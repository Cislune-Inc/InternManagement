# Stage kiosk setup without starting a second clock

Use the already logged-in Mac account, per the owner's September 8 correction.
Manager HTTP authentication remains; sharing an administrator desktop is not
OS-level isolation. Do not add another macOS account as a prerequisite.

The verified current worker can be enrolled for PIN setup and private work intake
while the actual Gusto-to-DP handover is still being reconciled:

```sh
PYTHONPATH=. .venv/bin/python ops/enable_slack_clock_beta.py \
  --user-key VERIFIED_WORKER_KEY --setup-only \
  --state-db data/agent_state.sqlite3
```

Review the explicit cohort; repeat `--user-key` for each worker to retain, then
use `--apply` and restart/verify services. This operation preserves configuration
backups and refuses open shifts or breaks for the workers being staged. Existing
PINs and attendance are not changed. Names appear with a handover-pending label;
PIN setup is permitted, but clock starts and returns are held server-side, even
for approved remote work. Hours reports and clock-out remain available. The owner
is not staged merely because the owner is automatically retained in the cohort.

Workers open a ten-minute setup window using `kiosk setup` in their own existing
Don Pollo Project Updates Slack conversation, then privately choose their PIN on
the Mini. A trusted operator may instead use `ops/kiosk_pin_setup.py` after
identity verification. Never generate, read, photograph or send their PINs.
No announcement or synthetic clock cycle is part of this operation.

## Before releasing the hold

- Verify actual handover, source timezone, earlier work and breaks today, and
  work already recorded in the established workweek. Approximate arrival time is
  not permission to invent an exact timestamp or a compliant break.
- Preserve original Gusto records and source identifiers. Flag disagreements for
  review; do not silently replace records or close another person's shift.
- Reconcile earlier time into the existing DP ledger/limit checks with a reviewed
  auditable import. A new clock must not reset daily/weekly or meal thresholds.
  This patch stages enrollment; it does **not** implement that import.
- Remove only the reviewed worker's pending-handover ID as a backed-up private
  configuration change. Ordinary re-enrollment deliberately does not clear holds.
- Have the person make their actual first kiosk punch; verify persisted time and
  their private Slack hours. Do not manufacture test attendance.
- Reconcile Gusto and DP before payroll, counting each worked interval once.
  No payroll upload, approval, source deletion or automatic Gusto cutoff is done.

The hold is a migration safeguard, not a work-description or AI quality gate.
