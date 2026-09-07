# September 7 release packet

Erik accepted the staged roadmap and authorized proceeding. He reaffirmed ownership
of final authorization; George supports contract/intentional IRAD/overhead alignment.
Erik explicitly approved preserving his August 7/10 open shifts as unresolved
historical records while starting fresh, without guessing end times. Other workers'
historical shifts and current roster enrollment are not authorized for migration.

## Reviewed changes

- Explicit disabled/unloaded launchd handling validates the installed job's label,
  executable and checkout before enable/bootstrap/kickstart. No broad service reset.
- Deployment preserves configured portal membership and URLs unless a verified
  base URL is supplied; primary-admin selection must match an existing manager.
- Isolated owner cohort keeps legacy worker schedulers and outbound admin alerts
  outside the cohort. Legacy portal reads/actions cannot mutate a parallel clock.
- Owner-approved historical deferral preserves original JSON and creates unresolved
  hours reports atomically. Unknown open tails are not interpreted as continuous
  work through today. Only explicit selected pre-beta records are affected.
- Slack Home shows clock state and totals; clock buttons refresh it. Reading Home
  does not create a session. The display has an update time, not a streaming timer.
- Work edits append corrections, invalidate prior plan approval, and preserve originals.
- Company-source URLs become scoped-to-owner references marked not verified. No
  connector, source-content access or automatic Drive/GitHub ingestion is claimed.
- Work handoff/evidence commands reuse saved notes and source references; IRAD is
  an explicit label. Manager decisions enqueue durable worker notices with retries.
- Original note acknowledged before optional AI; TLS uses DP's existing trust setup.
  Bounded schema output remains advisory. Never infer payroll/scope decisions from AI.
- The recovered production CI workflow is retained verbatim in the candidate.

## Pilot release command

Run only after reviewed tree, backup/restore proof and verified identity. The
parameters below are this explicitly authorized owner pilot, not deployment defaults.

```sh
ops/deploy-branch.sh agent/slack-hours-first-beta \
  --primary-admin-only --primary-admin-slack-id U01SWQKDTBM \
  --base-url http://192.168.40.177:8765 --enable-disabled \
  --defer-primary-legacy-before 2026-09-07
```

Old live deploy script lacks these options. Execute the reviewed candidate helper
from a stable staging copy with its explicit repository root; do not hand-edit
tracked production files or start the old bot before applying the new cohort.

## Acceptance and remaining work

Backup was restored in an isolated temporary location; integrity check returned
ok and 1,184 sessions. Restored plaintext was removed with its temporary directory.
Targeted tests cover changed behavior; full candidate regression passed before
the small historical-migration slice, which received focused time/export tests.
GitHub and production acceptance must be recorded separately after execution.

Only Erik joins this launch. Still confirm active worker identities, actual payroll
workday/week, Gusto employee/contractor paths and unresolved historical hours before
expanding. Midnight/Monday defaults are not a newly established payroll policy.
No payroll submission, staff announcement, Sites deployment, audio/screenshots or
ClickUp replacement is part of this release. Next source integration needs approved
project root/repository scopes and an appropriate company-owned connection.
