# Channel-first work updates

Owner-authorized September14: ordinary human channel posts are valid work inputs;
useful DP DM work notes may produce attributed project-channel updates. Only Erik
accepts shared project-plan changes. A report or model interpretation is not an
approved milestone, owner, dependency, charging allocation or attendance record.

## Worker flow

Prefer posting progress, a useful next result or blocker in the project channel,
with links and photos. No DP mention or rigid format. DP retains author, source
message/revision, thread, project route, file metadata and original timestamps.
Existing check-in suppression uses useful channel activity, not time-clock activity.
Photos stay in their original channel; file content is not yet visually analyzed.

When owner-enabled, an eligible NEW DM work note can produce a short attributed
channel excerpt. OpenAI selects up to three literal excerpts (800 characters) from
that note only. The deterministic worker record supplies the route. Unknown or
conflicting routes, unverified membership, shared/external destinations, private
requests and timekeeping/personnel notes are not automatically published. `private`
in the note suppresses automatic sharing. The bot does not copy private chat history
or photo bytes, alter file access, join channels or approve work.

Budget: optional coaching and excerpt selection share existing 20 calls/person/day
and 200 company-wide/day limits, bounded input/output, caching and no automatic API
retries. Automatic sharing preparation is capped at four attempts/person and 30
company-wide per rolling24h, with 30-minute per-person/destination cooldown. Holds
consume this budget too. Work is still saved when a share is held or AI unavailable.
No history backfill or delayed automatic sends. Prefer channels to avoid these DM
mirror limits. Uncertain delivery is retained for review, not retried blindly.

## Integration seams

`ChannelUpdates.records(channels=<server-verified grants>, after=..., limit=...)`
returns source-linked observations and tombstones. `after` is inclusive; consumers
deduplicate by source_ref/source_version and retain pagination overlap. Person refs
are workspace+Slack author; user_key is resolved only from the trusted roster.
No automatic enrollment or person matching by display name. Unresolved identities
remain unresolved. Current storage is single-workspace; don't reuse it for a second
workspace without a compound-key migration.

`DMWorkUpdates.published_records(channels=<server-verified grants>)` returns only
already-published channel excerpts, not raw/held DMs. Direct DP bot posts are ignored
by normal human-channel ingestion; use this projection without pretending the bot
is the original author. DM source references and audit rows stay owner-private.

Neither method performs authorization: the caller must enforce server-side source
grants, not accept a browser-supplied list as permission. No public HTTP endpoint
or change to planner approval state is provided here.

## Activation

Install approved channels:history, groups:history, channels:read and groups:read;
subscribe to message.channels/message.groups with existing Socket Mode and DM events.
Use ops/enable_channel_work_updates.py preflight; --apply --dm-sharing explicitly
activates owner-approved future mirroring. Supply only verified project routes.
Config backups remain private. Existing token, kiosk, roster and ledger are retained.

Verify installed token scopes, real bot membership and a genuine ordinary channel
post. Test AI in isolated temporary storage, never with synthetic production shifts.
Live service health alone does not prove worker acceptance or perfect classification.
