# Runtime channel-first integration — September 16

This runtime-side addendum complements `docs/shared-planning.md` on the planner
branch. Do not replace that branch's full host/identity/grant contract with this
file or merge the older planner tree over production.

`channel_work_updates` remains the source: actor, workspace/user bridge, root or
reply `thread_ts`, exact text, source version, files metadata and tombstones.
Replies are observations in the same channel audience, never new assignments or
attendance. The planner must ingest replies as well as top-level updates and use
current source grants. No schema changes to this table in this revision.

`dm_work_updates` now stores `reminded` receipts, not new `sent` excerpts. These
are PRIVATE and MUST NOT enter the published-excerpt adapter. Existing `sent`
history remains intact. `DMWorkUpdates.published_records` adds `preferred_source`
and `count_as_separate_progress=false` when a fuller same-author human post within
one day and the SAME channel covers at least90% of the quoted excerpt's tokens.
This is a duplicate grouping hint, not verification of the accomplishment.
Consumers reading SQLite directly need the equivalent projection; a refresh must
re-evaluate it after edits/deletes. Preserve both source identities and prefer the
full human text. Never cross channel audiences to find a replacement source.

Optional source-thread questions run only for fresh, terse unexplained blockers
in configured project channels. At most one attempt per root and two per author
per rolling day. Live complete thread/audience checks before and after bounded AI
must succeed. Existing replies, edits/deletes, explanations, explicit peer asks,
stale events and private/time language suppress the question. Uncertain delivery
is never retried. Missing Slack thread-read capability fails quietly; no new
permission is requested automatically. No per-post acknowledgement or broadcast.

Hours/lunch corrections stay in the existing worker DM handlers and daily review;
this module does not create time questions from work text, call clock APIs, change
payroll, or accept a plan. The existing current-clock activity signal is unchanged.
