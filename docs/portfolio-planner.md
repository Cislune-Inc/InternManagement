# Portfolio planning workbench

A manager can inspect the next work packets, change estimates in a browser-local
scenario and see how dependencies and shared resources alter a forecast. The
existing clock remains independent. This is the initial reviewable increment,
not a deployed shared worker website or a new source-ingestion service.

## Delivered

- Protected manager routes `/portfolio` and `/api/portfolio-data` reuse the
  existing loopback/Host/authentication boundary. Worker portal tokens confer no
  access. Missing or malformed snapshot errors do not modify time or work state.
- One source snapshot at `storage/portfolio/plan.json`; private data is untracked.
- Timeline, dependency graph, readiness queue and portfolio overview.
- Minimum/likely/downside remaining working-day spans; unknown estimates propagate.
- Finish-to-start dependencies, earliest-start offsets, exclusive named resources,
  idle-gap filling and a deterministic priority-ordered resource heuristic.
- Conditional gates, source links, completion evidence, scenario editing,
  browser-local persistence, JSON export/import and cycle/input rejection.

Minimum is an optimistic conditional scenario, not a proven physical lower bound.
Downside is not P80; no statistical confidence is claimed. The driving chain is
for the modeled portfolio and chosen resource heuristic, not proof of optimality.
Unestimated work does not reserve capacity and can make forecasts optimistic.
Durations are workday spans, not paid effort; split machine wait, external lead
and human effort into separate packets before assigning a credible calendar.
Weekends are skipped; holidays, individual work windows, partial allocations,
probabilistic rework and deadlines are not yet modeled.

## Local preview without production runtime

```sh
.venv/bin/python -m agent.portfolio --storage-root /path/to/private/storage --output outputs/planner.html
```

Open the rendered HTML locally or on a loopback-only local preview. Never publish
rendered private data or its input to this public source repository. The tracked
source contains no company project snapshot, source messages or credentials.
The HTML's scenario edits do not write to the authoritative source. Export a
scenario before clearing browser storage. Do not expose this owner page to workers
by merely hiding fields: worker access needs authenticated server-side projection
and approved source-scope filtering.

## Next integration packets

1. Validate the immediate bottleneck against actual physical evidence; enter the
   next few owner estimates and available work windows. Keep proposed versus
   accepted packet revisions distinct.
2. Add availability calendars and separate effort, equipment occupancy and
   external waits. Add milestone deadlines and a proper float/slip calculation.
3. Feed reviewed source changes into permission-scoped snapshots; detect source
   revision changes without silently overwriting browser scenarios or approvals.
4. Implement authenticated worker read views, versioned next-session review and
   approved-plan lookup in the existing Don Pollo work flow. Reuse its ledger and
   identity mapping; never infer payroll authority from project labels.
5. Add reviewed weekly channel drafts and disagreement resolution. Publication
   stays an explicit authorized action. Keep recurring source review checkpointed.
6. After estimate calibration, add risk distributions/correlations and probabilistic
   milestone forecasts. Do not label three-point scenarios as confidence bounds.

## Validation

```sh
node tests/test_portfolio_schedule.cjs
.venv/bin/python -m compileall -q agent tests
.venv/bin/python -m pytest -q
```

Basis: [GAO Schedule Assessment Guide](https://www.gao.gov/products/gao-16-89g).
Dependencies, resources and schedule risk matter more than manually drawn dates.

## On-site technical preview service

`python -m agent.portfolio_test_server` serves a reviewed technical snapshot from
an isolated release directory on a specific office IPv4 interface. The caller
supplies `--host`, `--network`, `--snapshot` and `--feedback-db`; default port 8876.
It is an explicitly authorized LAN test audience, not the authenticated manager
or future worker account portal. No production runtime, clock database or
configuration is loaded. Unlisted snapshot fields are excluded, but the operator
must also review the allowed-field text for its intended team audience.

The service checks peer subnet and Host, exposes only the preview/liveness and
feedback submission, and gives no directory browsing or feedback-reading API.
Feedback requires same-origin POST and a page token, is bounded and idempotent,
and goes into its own private SQLite file. Names are optional self-reports, not
verified identity. Source plan edits remain browser-local. There is no Slack send.

Use a dedicated per-user LaunchAgent with RunAtLoad/KeepAlive, referencing a
versioned release and the existing Python interpreter. Keep logs and the feedback
database outside the release. Replacing the preview never restarts Don Pollo.
This pilot requires the service account's existing login session after reboot;
it does not establish unattended boot recovery. A changed DHCP address requires
rechecking and updating the explicit listener/allowlist. Stop the preview by
booting out its single LaunchAgent; preserve its feedback database.
