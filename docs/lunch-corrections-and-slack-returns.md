# September 8: lunch corrections and Slack break returns

Accepted owner direction: keep new onsite shift starts at the local name/PIN
kiosk; permit lunch, paid rest and actual return within an existing shift on
Slack. Keep clock-out and actual-hours reports available as safety fallbacks.
Staff handover holds, advance remote authorization, hours limits and minimum
break-return gates remain. A paused paid-rest return can resume only its existing
break, with any unknown gap retained for actual-hours review. No auto-resume.

## Completed lunch correction

In the existing DP DM, send `fix lunch today 11:30am-12:15pm`, substituting actual
times. An explicit YYYY-MM-DD may replace `today` for the preceding seven days.
Both times need AM/PM and use the configured shop timezone. The preview shows
the replacement interval and before/after shift-day totals. `confirm lunch CODE`
applies that exact actor-bound preview once; `cancel lunch` cancels. Previews
expire after 30 minutes and become stale when relevant clock records change.
The confirmation does not start/stop a shift, authorize work, or submit payroll.

The narrow first release supports one completed meal inside recorded work
segments. Multiple meals, legacy/unresolved records, gaps, overlapping paid rest,
ambiguous DST wall times and uncertain/interrupted-meal reports need manager
review. Short meals remain paid pending review; correction is not a declaration
that a meal was legally compliant. Do not suggest fictional compliant timestamps.

Each applied correction preserves prior clock fields and meal records in a
durable audit. General `report hours` still records a pending claim, not an
automatic deduction. A trusted operator can prepare a preview linked to a
specific pending report with `ops/prepare_lunch_correction.py`; this writes only
the preview, sends no message, and resolves that report only on confirmation.

Lunch-shaped prose such as `On lunch since ...` is intercepted for clarification,
not converted into a project task. `hours` and App Home show the recorded lunch
interval as well as paid totals. Work check-ins are suppressed for 30 minutes
after a clock/break notice, preventing simultaneous competing prompts.

## Acceptance

Run isolated synthetic tests; never manufacture worker punches. After deployment,
use an actual owner correction and verify one deduction, original audit, resolved
source report where linked, unchanged shift start/stop and updated export totals.
Then test a genuine Slack break/return. Staff activation still requires reviewed
earlier day/week Gusto continuity. Passing tests alone is not live-user acceptance.
