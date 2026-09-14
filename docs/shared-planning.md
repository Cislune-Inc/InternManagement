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

Routes belong to the explicitly configured origin; this app can serve a separate
authenticated listener behind the existing host's TLS/session layer. Do not mount
it under a prefix without also adjusting its absolute asset/API paths.

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
