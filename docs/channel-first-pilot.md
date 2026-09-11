# Channel-first worker updates

The first release accepts explicit app mentions only in `work_summary_channels`,
from active enrolled workers. `channel_updates_enabled` defaults false and must
remain false until the Slack installation has `app_mentions:read` and subscribes
to `app_mention`. No channel-history or blanket DM-forwarding scope is required.

Workers may post their result, next step or blocker in a configured project channel
and mention Don Pollo. Capture is source-linked, with a short in-thread receipt.
Five or more words count as a provisional substantive update for two-hour progress
prompt suppression. This is not AI quality validation, work approval, contract
charging, or attendance. Channel traffic never executes timekeeping commands or
extends the attendance inactivity timer. Files are retained as metadata references;
their contents are not downloaded, interpreted or automatically reposted.

Only explicit mentions are captured. Ordinary thread replies and later edits or
deletions are not synchronized in this first increment. To correct an update,
post a new clearly labeled correction and mention DP again. Originals remain.
Reusing the same channel/message timestamp cannot create a second receipt. A
transport failure is logged without blind retries. This is not a full channel
history mirror or an unattended digest publisher.

DM `work channel updates` lists the user's captured reports; only the primary
owner can list across users. `work draft` offers the existing exact-version
`work share SH-id` flow, or posting directly. Channel posts are not automatically
reposted. No source-channel finding becomes an approved project plan.

Activation acceptance: verify app permission/event subscription, enable the scoped
flag, then observe one real worker mention in an approved project channel. Verify
source row/permalink, one threaded receipt, no duplicate private progress prompt,
and unchanged attendance. Do not manufacture production hours or test progress.

Related DM repairs: greetings and clock-status questions bypass work intake;
backtick-wrapped kiosk setup works; explicit new current focus creates a new pending
work item instead of relabeling old history. CARVE CORE/EX are workstream labels,
not automatic GRASP allocation. AI receives beginning/in-progress context, not
authority to change hours. Real response quality still needs worker validation.
