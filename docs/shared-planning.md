# Shared work and Don Pollo

This increment makes work packets, source evidence, discussions and accepted plan
revisions durable. Workers propose; only the explicitly configured acceptance
owner decides. Nothing in these modules issues clock events, changes charging,
enrolls workers, sends Slack messages or initializes production DP tables.

## Working review

```sh
python -m agent.planning_review \
  --snapshot /private/reviewed/plan.json \
  --database /private/review/planning.sqlite \
  --port 8877
```

The review executable listens only on `127.0.0.1`, supplies a clearly labeled
local sandbox identity, and does not connect Slack or timekeeping. It is not a
worker deployment or production authentication mechanism. The original imported
snapshot is preserved; imported rows do not become accepted by being loaded.
Operator imports must be reviewed for the audience: field filtering cannot decide
whether otherwise allowed free text or a source URL contains restricted content.
The existing port8876 on-site preview remains separate.

Views: next work; resource-aware dependency timeline; source updates and explicit
packet links; proposed changes with before/after values and discussion; personal
recaps and owner recap review; a source-scoped meeting draft. Recaps are visible
to their author and the acceptance owner, not automatically the project team.
The current permission question defaults to that narrower working audience.
No calendar event or publication is performed by generating the meeting draft.

## Contract for the DP integration

`PlanningStore(path, owner_ref=...)` uses a separate SQLite file. `owner_ref` is a
namespaced, verified person reference, e.g. `slack:<workspace>:<member>`. It is
stored at initialization; changing it requires an explicit migration. Never use
display names or DP's ledger `user_key` as a substitute for the Slack identity.
`roster_bridge()` provides the explicit bridge without enrolling unknown authors.

`create_planning_app(store, authenticate=..., allowed_origin=..., time_reader=...,
people_reader=...)` requires the host to authenticate every request and return:

```python
Principal(person_ref, frozenset(project_ids), frozenset(source_scopes))
```

Project grants and source grants are separate. The acceptance owner does not
automatically gain private-source access. `dp_identity_resolver()` reuses DP's
existing token validator and accepts host-established bearer/cookie sessions;
current DP enrollment, expiry and roster validation still apply. It neither
issues tokens nor makes a shared administrator Basic credential an Erik identity.
The host must establish a secure login/session handoff over TLS for remote users
and provide current server-owned project/source grants. The browser cannot supply
roles or grants. Missing or revoked grants deny access on the next request.

`people_reader(principal)` supplies only verified, audience-appropriate
`person_ref`/`name` choices. `time_reader(principal)` returns that person's actual
`clock_state`, `recorded_seconds_today`, `as_of`, `source` and `unresolved` summary.
It must be read-only; do not call helpers that normalize or save live sessions.
Neither work duration estimates nor messages are used to synthesize this summary.
No callbacks are connected by the review executable.

The DP host factory now supplies `dp_time_reader` by default. It resolves the
verified Slack identity against the current active roster and opens the existing
ledger in SQLite read-only mode. It uses DP's existing `paid_seconds` calculation,
preserving overlap union, recorded lunches, paid rest and legacy unresolved-tail
handling. It returns only that person's clock state, current-day recorded seconds,
observation time and unresolved flags. Pending report text and other workers'
records are excluded. Administrators without an explicit worker-ledger identity
receive no guessed hours. Neither StateStore nor clock constructors are invoked.

Supply `policy_reader` to the host factory to connect `DPPlanningConnections` for
current project grants and roster choices. It must read an approved private mapping
`{project_id: {"members": [slack_id], "channels": [channel_id]}}` on each request.
This mapping is not an enrollment list or a contract charging rule. The adapter
uses current DP `slack.can_share_work` to verify each source channel's membership;
absent capability, API failure or denied membership grants no source scope.
It never joins channels or posts. A pilot identity is bounded to ten source
channels. Roster choices include only active DP identities sharing an explicitly
assigned project with the viewer. No sample policy should be copied into live
configuration as an implied access approval.

September14 live smoke check: the read-only time adapter ran successfully against
the Mini ledger and retained unresolved-history flags. This was a read check,
not enrollment or payroll review.
The local sandbox on port8877 still has no live time callback. Worker deployment
still needs the private HTTPS/VPN host and authenticated browser entry point.

Routes belong to the explicitly configured origin; this app can serve a separate
authenticated listener behind the existing host's TLS/session layer. Do not mount
it under a prefix without also adjusting its absolute asset/API paths.

`planning_host.create_dp_planning_app()` now assembles that host boundary. It
returns `None` unless explicitly enabled and requires an exact HTTPS origin and
a store whose pinned acceptance owner belongs to the configured Slack workspace.
Its `grant_snapshot(slack_id)` callback returns one fresh mapping containing
`projects` and `source_scopes`, or `None` when access cannot be established. Empty
project access denies sign-in. The callback must apply current server-owned
project assignments and source membership, with no stale-success fallback.

The existing authenticated DP host can POST the existing worker portal bearer
to `/planning/session` with the exact Origin. After DP verifies enrollment and
expiry and the host supplies current grants, this sets a Secure, HttpOnly,
SameSite=Strict browser-session cookie and returns `/planning` as the next path.
No new token is issued or its lifetime extended. `/planning/logout` clears the
cookie via same-origin POST. Tokens in query parameters and shared manager Basic
credentials are not accepted for this handoff. This assembly does not add a
worker-facing login link, provision TLS, launch a listener, send Slack links or
enable access. The production host still needs to wire its existing authenticated
browser bootstrap and approved assignment policy. The default live callbacks
provide membership checks, roster projection and own-time reads.
Terminate TLS at the configured trusted host/proxy and disable access logging of
credentials; never expose the upstream listener directly on an untrusted network.

| Route | Behavior |
|---|---|
| `GET /planning` or `/` | Shared-work shell; source records arrive only from the authenticated API |
| `GET /api/planning/state` | Fresh permission projection, owner capabilities, actor-bound CSRF token |
| `POST /api/planning/proposals` | Immutable proposal based on an exact current revision; task, new task or project milestone |
| `POST /api/planning/discussion` | Note, question or disagreement; bumps reviewed discussion revision |
| `POST /api/planning/resolve` | Author or acceptance owner records a resolution without erasing the original |
| `POST /api/planning/decisions` | Owner-only accept/reject; exact discussion revision and decision reason required |
| `POST /api/planning/links` | Explicit source-to-packet link, owner-only; cannot widen original audience |
| `POST /api/planning/recaps` | Author-bound, append-only work recap referencing reviewed plan revision |

Mutations require exact Origin, actor-bound CSRF and an idempotency request ID.
Retries reuse that ID and exact payload; changed payloads conflict. Accepted
changes and their receipt commit in one immediate SQLite transaction. Concurrent
acceptance cannot silently overwrite another decision. Stale plan/discussion or
edited/deleted cited evidence requires renewed review. Open questions must have
recorded resolutions before acceptance. Rejection does not alter the plan.

The source-version table preserves provenance; normal views show only the current
source projection and tombstones. Hidden prerequisites appear as opaque unresolved
gates instead of leaking titles or becoming falsely ready. Acceptance of changed
fields does not approve unrelated imported assignments or historical estimates.
Proposals, discussion and resulting entities inherit the cited evidence audience;
acceptance does not declassify source-derived content. A reader without those
source grants receives an unresolved prerequisite instead. A separately reviewed,
sanitized audience migration is needed to widen that scope.

## Read adapters — current DP interface

The adapter targets DP's `channel_work_updates`/`work_intake_events` interfaces
and the sent-only projection in `DMWorkUpdates.published_records()` as of source
commit `9721c41`. It does not instantiate DP capture classes, because their
constructors create/migrate tables. Every database connection uses SQLite
`mode=ro` with a consistent read transaction; never `immutable=1` on a live WAL.
Prefer an operator-created consistent snapshot for analytics.

- `read_channel_batch(..., workspace=..., channels=<verified grants>, cursor=...)`
  includes edits/tombstones and returns a bounded keyset cursor. Existing optional
  workspace/roster/thread columns are respected; a contradictory workspace fails.
  No channel list means no export. It establishes captured-record coverage only.
- `ingest_channel_batch()` returns the next cursor only after successful ingestion
  of the batch. The operator persists it after success. Duplicate/reordered
  revisions do not overwrite newer evidence or resurrect a tombstone.
- `read_published_excerpts()` exports only sent destination-channel text, original
  worker attribution and actual channel message reference. No held/raw private
  input or private-DM timestamp is projected. Publication receipts do not establish
  that later edits/deletions were reconciled; the UI labels that limitation.
- `read_work_events()` is an optional private, operator-scoped adapter. Immutable
  event IDs stay external IDs. An explicit item-to-project map is needed; today's
  mutable project label must not retroactively relabel a historical event. Source
  scope is `person:slack:<workspace>:<owner>`. Do not ingest private history merely
  because a worker approved a channel excerpt.

Use operator-owned project aliases where DP keys and original planner IDs differ
(for example `dp` versus `don-pollo`, `mars_to_table` versus `mars-to-table`). Keep
the source workstream key and uncertain mappings; do not combine shared-channel
workstreams or infer their billing destination. Existing DP item IDs are never
rekeyed into planner IDs. Sources are explicitly linked to stable packets.

## Validation and remaining rollout work

`planning_sync.sync_sources()` provides a bounded, operator-driven refresh over
channel capture or sent-only excerpts. It requires the host to supply current
channel grants each time; it does not discover grants, read raw DMs, or start a
background job. A separate private checkpoint database serializes refreshers and
pins each stream to its source/destination paths, workspace, channel list, source
kind and project aliases. Changed configuration requires a reviewed new stream.
Removing ingestion grants does not erase historical records: fresh viewer grants
must still control every planning request.

Each call reads at most ten batches of 200 records. Checkpoints commit only after
all ingestions succeed; interrupted calls replay safely using source versions.
Returned metadata includes coverage, checked time, counts and whether more captured
records remain. Missing capture tables remain explicit coverage gaps. This does
not establish complete Slack history or a successful live Slack connection.

Call with `reconcile=True` to start a bounded sweep from the beginning; if
`has_more` is true, resume with normal calls until exhausted. Reconciliation is
necessary for backdated capture records, restored source snapshots and late
publication receipts that sort before the incremental cursor. Do not restart the
sweep on every page. Source backups must preserve versions; deleted rows without
tombstones cannot be reconciled by this adapter. Published-excerpt receipts still
do not establish current channel edit/deletion state. No automatic refresh cadence
or source retention policy is enabled by this helper.

Tests exercise authorization/CSRF, projection, hidden dependencies, source
versions/tombstones, cursor coverage, sent-only excerpts, immutable work references,
concurrent/stale acceptance, discussion resolution, idempotency, and recap privacy.
The UI supports a complete proposal/discussion/decision cycle in a disposable
sandbox. There are no new dependencies beyond the repository's existing aiohttp.

Before a worker pilot: connect the existing verified identity/session transport;
supply current project and source grants; connect read-only DP records and actual
time callbacks; reconcile a current Bagworm snapshot and owner-reviewed estimates;
verify one real worker cycle. Publishing recaps and automated agendas remains the
existing DP sharing workflow with its own authorization. Resource calendars,
probabilistic forecasts, image interpretation, automated source polling, and
automatic reallocation are not implemented by this increment.

## Trusted owner-machine pilot

`python -m agent.planning_owner_pilot --host VERIFIED_MINI_ADDRESS --owner OWNER_SLACK_ID
--workspace WORKSPACE_ID --channel BAGWORM_CHANNEL --snapshot PRIVATE_SEED
--database PRIVATE_PLANNER_DB --port 8879` runs only on loopback on the owner's
trusted Mac. Supply these CLI flags on one line. This is a distinct owner-only
transport, not the worker HTTPS service. Every process/user with access to this
Mac's loopback can access the pilot; never run it on a shared kiosk. The operator
explicitly selects the owner, and each data/mutation request verifies the saved
SSH host key, the current DP primary-owner identity and Slack workspace. SSH uses
the existing trusted administrator account; it is not worker authentication.

Source access is rechecked using current Slack membership on each request. The
pilot reads captured Bagworm sources and a bounded recent-20-message history
snapshot. The latter excludes bot posts and does not cover older history, replies
or deletion reconciliation. These limits are displayed on source cards. Failure
uses no cached permission fallback. Source code is evaluated through SSH stdin;
no production checkout files, credentials, enrollment, clocks or service are
modified. Secrets remain on the Mini. Sources and owner review revisions remain
in the private local planner database; they are not automatically shared plans.

The owner's existing ledger mapping is used if present. A primary administrator
without a worker-ledger mapping receives unavailable time, never synthetic hours.
Worker testing still requires a selected worker and the separate authenticated
HTTPS entry point. Keep proposed source-backed changes pending until Erik accepts
specific fields. Test acceptance on a disposable database copy rather than
altering real project proposals for QA.

### Channel-first source grouping (September 16)

The published-excerpt adapter remains sent-only; private `reminded` records are
not planner evidence. On each authorized store view, historical bot excerpts are
grouped with fuller human posts only within the same source scope, author and
project, within one day, and with at least five distinct quoted words matching
contiguously after normalization. Both source IDs and original texts remain.
`preferred_source` is a planner source reference; `count_as_separate_progress=false`
marks the excerpt, and `progress_group` identifies the fuller source. These are
view-only hints, recomputed from visible current revisions, not persisted receipt
versions or proof of completed work. Edits, tombstones and lost grants remove the
match. Missing source coverage means grouping may be incomplete. The UI links to
the fuller post. Plan approval and time records are unaffected.

The owner pilot checkout is now `/Users/erikfranks/Developer/cislune-pm`, outside
iCloud Documents. Its private state remains under ignored `storage/portfolio`;
the original Documents checkout is retained as migration recovery, not the active
service checkout. Do not put private runtime files into Git.
