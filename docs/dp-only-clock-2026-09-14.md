# DP-only clock simplification

Owner-approved replacement for the September 7–13 dual-clock migration workflow.
Production Slack/kiosk ledger enables `simplified_flow`; legacy callers retain
the prior behavior for compatibility. No historical payroll import or pay approval.

## Worker flow

- Start/finish at the existing PIN kiosk (approved remote/owner paths retained).
- Paid rest stays on the clock. Optional `break` records an intention, not a
  fabricated completed ten-minute interval. No mandatory return action or lockout.
- Lunch starts with `lunch` and ends with `back`; 30-minute return boundary remains.
  Correct forgotten/completed lunch using the existing actor-bound preview/confirm.
- Remind at 30 and 10 minutes before the meal threshold and once overdue. Do not
  stop the clock or require a fictional meal to restart when lunch is missing.
- Hours/remote approval gates remain. Automatic stops are explicitly unconfirmed;
  a source-bound worker confirmation verifies the displayed finish without changing
  hours. Different actual times go to the existing correction queue.
- Kiosk checkout queues one private daily review, deduplicated by the punch event.
  Confirm hours/lunch with a source-bound button; report rest exceptions without
  reconstructing timestamps. Missing meals/automatic gaps cannot be marked clean.
- Pending daily reviews appear in the private manager queue; no admin broadcast.
- Fresh useful channel posts suppress inactivity warnings for their own enrolled
  author while a clock is already running. No start/resume, old-post or edit replay.

## Boundaries and verification

Gusto is the payroll destination, not an ongoing clock or fallback. The historical
reconciliation page remains available for last week's overlap cleanup. No new
automatic Gusto writes, compensation inference, channel permissions or AI payroll
decisions. Existing bounded work-coaching API use remains unchanged.

Legacy open rest markers are preserved as unconfirmed reports when interacted
with; no invented end, deduction or retrospective completed rest. Obsolete queued
rest/meal-lockout prompts and resolved reminder/confirmation messages are filtered.
Daily worker review does not approve payroll or clear a manager hours report.

Test in isolated storage; no synthetic production punches or Slack test posts.
After deployment, genuine worker kiosk checkout, private review and actual lunch
start/return are the acceptance check. Process health alone is not that check.
