# Channel-first worker updates

Owner correction: capture ordinary human posts in every public/private channel the
bot can access. No mention, enrollment or formatting requirement. The existing
project/channel map supplies context, not an ingestion gate or charging approval.
Unmapped channels remain unmapped for review. Personal/group DMs stay separate.
`channel_updates_enabled` defaults false until Slack grants `channels:history` and
`groups:history` with `message.channels` and `message.groups` subscriptions.
Channel-list permissions support coverage auditing; bot membership still governs
delivery. Enabling the flag alone does not prove workspace-wide coverage.

Workers post normally. Capture is source-linked and quiet: no per-post receipt,
reaction or question. Short work-result text or captioned files can suppress a
redundant private progress prompt. This is a conservative heuristic, not a worker
formatting rule; ambiguous/chatter posts are also preserved but do not suppress
prompts. This is not AI quality validation, work approval, contract
charging, or attendance. Channel traffic never executes timekeeping commands or
extends the attendance inactivity timer. Files are retained as metadata references;
their contents are not downloaded, interpreted or automatically reposted.

Ordinary thread replies are included. Edits update the current view while retaining
prior versions for audit; deletions exclude the post from current views and prompt
suppression. Late retries cannot resurrect deleted content. Editing an old post
does not count as new work today: its original posting time remains the anchor.
Bot messages are ignored. No raw private content is automatically forwarded into
another channel. Reviewed useful summaries remain a separate publication action;
this is not an unattended digest publisher or historical full-workspace mirror.

DM `work channel updates` lists the user's captured reports; only the primary
owner can list across users. `work draft` offers the existing exact-version
`work share SH-id` flow, or posting directly. Channel posts are not automatically
reposted. No source-channel finding becomes an approved project plan.

Activation acceptance: verify app permission/event subscription, enable the scoped
flag, then observe one real ordinary post and check actual channel coverage. Verify
source row/permalink, no channel spam, no duplicate private progress prompt,
and unchanged attendance. Do not manufacture production hours or test progress.

Related DM repairs: greetings and clock-status questions bypass work intake;
backtick-wrapped kiosk setup works; explicit new current focus creates a new pending
work item instead of relabeling old history. CARVE CORE/EX are workstream labels,
not automatic GRASP allocation. AI receives beginning/in-progress context, not
authority to change hours. Real response quality still needs worker validation.
