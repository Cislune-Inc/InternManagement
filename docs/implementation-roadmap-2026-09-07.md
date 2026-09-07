# Don Pollo implementation roadmap and live readiness findings

September 7, 2026. Internal developer/coordination packet. Erik requested live
inspection and a plan for a useful, low-friction assistant that gathers company
work evidence for Codex. Production inspection was read-only. The roadmap below
is a proposal for approval, not a claim that future features are implemented.

## Outcome and operating model

Reliable hours first, aligned work second, reusable evidence throughout.
Slack is the worker's primary interface. DP owns the durable clock and work log.
Company systems retain source artifacts; Codex helps turn authorized evidence
into next actions, tested changes and reviewable reports. No second payroll clock.

Company-account-first does not mean browser-only work. Use Cislune's ChatGPT
workspace for AI work; approved company Drive, GitHub, OnShape and server
destinations for results. CAD, machines, instruments, coding tools and physical
shop work remain legitimate. Personal accounts/private repos/local-only folders
must not become the only durable record of company work. Do not share logins.

## Newly verified live facts

- Mini: 192.168.40.177, verified saved SSH key, login pm, clean production main
  at da8aece. Live history was fetched read-only into local mini-audit/main for
  comparison; no production Git/config/roster/service mutation occurred.
- Bot launchd job is not loaded and is explicitly marked disabled. A simple
  kickstart cannot repair this. Why/by whom it was disabled is not established.
- Last bot logs are August 24 and show Slack/Discord DNS failures. DNS now
  resolves Slack/OpenAI/GitHub/Drive. Historical DNS errors do not explain the
  current disabled service flag by themselves.
- Dashboard service is running. Backup service last exited successfully;
  health reports a healthy backup. Restore verification remains a release gate.
- Slack bot auth.test succeeds. OpenAI models endpoint succeeds; configured
  gpt-5-mini and candidate gpt-6-astra are listed. These read-only checks used
  DP's TLS trust configuration. A real Responses generation/Socket Mode
  connection has NOT been verified; key presence is not model-quality proof.
- Ledger: 1,184 session rows, latest session date August 24. Latest recorded
  inbound message is August 22 00:12:48 UTC; none since August 24. This establishes
  lack of recorded DP intake, not absence of work or use of another clock.
- Roster marks 17 people active and maps all 17 to Slack. Five hourly entries
  lack Gusto mappings. The active roster is not verified against current workers;
  do not reenroll departed interns merely because an old row says active.
- Manager link still points at 192.168.4.87. Config workday rollover is 03:30;
  candidate clock uses calendar midnight/Monday. Confirm the established payroll
  workday/week and align calculations before launch, not by guessing from UI dates.
- Candidate differs from live in 34 paths, including live-only test.yml and
  existing operations/task-correction changes from the prior branch. The deploy
  reconciliation preserves the CI workflow, but every meaningful delta still
  needs review. Never replace production blindly with the candidate tree.

## Release packets, ordered by value and dependency

| Packet | Scope | Acceptance | Owner / proposed timing |
|---|---|---|---|
| R0: launch repairs | Disabled/unloaded service handling; verified host/link config; history reconciliation; company workday/week; roster selection; restore test; Slack/model smoke test | Correct candidate/config before enabling service, one instance, live Slack receipt + ledger + export agree; reboot/reconnect tested | Codex + Erik, first morning packet |
| R1: excellent Slack basics | Home shows live status/day/week; buttons; short help; one focused work question; edit previous answer; task/project suggestions; manager decision buttons + worker notification | No repeated forms or lost text, updates don't block clocks, worker can correct a mistaken project/answer without redoing the day | Codex, next small release after clock |
| R2: evidence handoff | Source links, company destination checks, retrieval status, resumable context, exact source IDs/versions, worker-confirmed summary | Worker submits one result once; Codex can retrieve it with correct permissions and cite it; private/inaccessible link never silently treated as evidence | Codex + source owners |
| R3: aligned work | Accepted project outcome, near-term demonstrable MVP, owner, target, dependencies, done evidence; approved work menus; proposed alternative queue | Erik/George can answer what matters today and spot drift without reading every message; no invented contractual deadlines | Erik/George approve baselines; Codex assists |
| R4: availability + visual manager view | Rough weekly availability, days off, focus/skills; capacity versus targets; collapsible phone UI, dependencies/Gantt | Same backend/identity; no duplicate clock; availability is not paid time | Codex after R1/R2; hosting choice needs approval |
| R5: automation and broader capture | Incremental Drive/GitHub retrieval, source-backed drafts, authorized recurring review, selected device evidence only if later approved | Bounded cost, freshness visible, recoverable queues, access revocation, minimal nagging, no automatic payroll/employment inference | Codex; staged after measured pilot |

Do not promise all packets tomorrow. The existing 530-test beta is a candidate,
not production acceptance. R0 should precede feature expansion; R1 can ship in
small increments rather than waiting for a full website.

### R0 implementation notes

The release helper currently calls kickstart and assumes the service is loaded.
Add explicit stopped/unloaded/disabled-state handling with a deliberate enable
step only for this approved service and only after candidate/config verification.
Do not enable the old Discord-dependent bot first. Verify current process/socket,
not stale log text. Keep a single-instance lock, bounded retries and shutdown.

Review config updates that still carry hard-coded prior IPs. Propose a DHCP
reservation/stable internal DNS and one canonical base URL; do not change the
router as a side effect of deployment. Keep strict saved host-key verification.
The old admin HTTP surface is not suitable for public exposure. Add real
server-side role authorization and CSRF protection before broader web access.

Preserve actual hours through errors. Resolve workday/week and cross-midnight
classification in a deterministic time service. Verify employee versus contractor
payroll paths: the current draft Gusto row builder labels rows Employee, so it
cannot be assumed correct for a contractor merely because compensation is hourly.
Unmapped people still need time records and an explicit reconciled payment path.

## Worker experience: the minimum useful conversation

1. Clock in with one explicit action. Time is recorded before AI or project lookup.
2. DP offers up to five relevant actions: assigned/near-deadline/approved work,
   resume last block, browse allowed contracts/overhead, or propose something else.
3. Worker selects a project or types the plan. DP extracts intent, expected result
   and rough estimate, asking only for the highest-value missing detail. Existing
   useful detail means no follow-up. Labels are not permission to bill a contract.
4. DP supplies the prior artifact, accepted target and next action. Worker works
   in the right company tool; the assistant helps draft/test/research there.
5. Worker posts an artifact link plus what changed, or a brief blocker. DP verifies
   access and drafts the work summary for easy correction/confirmation.
6. End work with immediate clock-out. Offer optional one-tap next-availability
   confirmation and leave tomorrow's next step ready; never hold clock-out hostage.

Example (illustrative, not an assigned engineering task):
Worker: 'GRASP: compare today's test runs and put the plot in the project folder.'
DP: 'Recorded. What comparison will let George decide whether the change helped?'
Later: 'I found your linked plot. Draft: compared the runs; result remains mixed;
next step is to check the setup difference. Correct / Edit / Mark blocked.'
DP says 'I found' only after an authorized fetch, never from a filename alone.

Use friendly, firm coaching for repeated vague text: 'Same activity is fine—what
changed, what did you try, or what is blocking it?' Preserve originals. No word
quotas, productivity scores or disciplinary inference from lexical repetition.
Evidence can reduce progress prompts; file edits do not prove attendance, end a
break, extend permission or justify stopping/pay-deducting hours. Retain the
accepted four-hour inactivity policy independently until Erik changes it.

## Company work evidence contract

| Source of work | Durable record | Low-burden handoff |
|---|---|---|
| ChatGPT/Codex | Approved shared context plus output in company source system | Ask assistant to save result, summarize decision/remaining uncertainty, return source link; don't assume all chats are visible |
| Drive | Existing approved project folder/shared drive; native editable result | File ID/link and modified/version evidence; worker confirms summary |
| GitHub | Company organization repo; branch/commit/PR; test evidence | PR/commit link, result/tests, remaining blocker; local unpushed code is pending sync |
| OnShape | Company-accessible document/version and design intent | Version link and concise change/measurement; keep original CAD there |
| Dell C3630 / shop files | Approved project data folder, experiment/run ID, raw evidence | Stable path/manifest, timestamp/checksum and lightweight preview when permitted |
| Physical shop work / meeting | Short outcome, location/decision, optional useful photo/link | No fabricated digital artifact required; record concrete result and next action |

Each WorkBlock links worker_key, project_id, approved-plan revision (if any),
intent, expected result, estimate, status/blocker and Evidence records. Clock
events remain separate actual-time events. Evidence includes canonical source,
source ID, revision/hash, actor attribution, occurred/fetched timestamps,
classification/access scope and retrieval status. Manager decisions have actor,
time, scope, reason and version. Keep AI drafts distinctly labeled and reversible.

Do not duplicate source documents into a new generic repository. Store small
source-backed summaries/index entries, and retrieve the needed original with
current access checks. Exclude personnel, payroll, credentials and unrelated
private folders from engineering retrieval. A broad DP search already returned
personnel material; matching a keyword is not authorization to index everything.

## Identity, permissions and connection design

- Company-managed identities and separate logins; verify selected Cislune ChatGPT
  workspace and approved browser profile. Match worker_key to verified Slack,
  company Google and GitHub identities without guessing from display names.
- Confirm actual plan capabilities before relying on SSO, SCIM, custom roles or
  compliance exports. ChatGPT Business does not provide every Enterprise control.
- Workspace membership does not grant every chat/repo/file to Codex. Explicit
  sharing and authorized connectors still govern access. A connector available
  in Erik's Codex session is not automatically available to DP's OpenAI API client.
- Build DP's integrations with separately scoped company-owned credentials or
  approved delegated connections. Worker views cannot inherit an admin's broad
  read access. Manager payroll data remains separate from project context.
- Managed device/browser configuration can encourage/require company-account use
  where the specific product supports it. Validate rather than claim a prompt or
  domain allowlist alone can enforce the correct signed-in account.
- Offboarding revokes source access, worker enrollment and active credentials;
  retain required company records and prior work attribution. Do not auto-readd
  old interns from legacy deployment defaults.

Official boundary: https://learn.chatgpt.com/docs/enterprise/chatgpt-work-overview
Admin/plan controls: https://learn.chatgpt.com/docs/enterprise/work-admin-faq

## Sync and AI architecture

Keep one Mini clock authority for the first release. Use a separate durable queue
for work evidence and API enrichment so a slow/unavailable model or connector
cannot prevent saving time. Changes are append-only events; projections are
rebuildable. Back up consistent SQLite snapshots, not a live DB through a Drive
sync folder. Verify restore and retain an approved separate backup destination.

Start with explicit source links and authorized outbound polling of selected
project sources. This avoids making a new public inbound service a launch
dependency. For Drive, persist changes.list cursors only after saving each batch,
honor removals/access loss, scope to approved roots and show sync freshness.
For GitHub, track selected repositories/PRs/commits with deduplication and overlap
on incremental windows; later signed webhook delivery can reduce latency if an
approved HTTPS receiver is introduced. Delivery order is not guaranteed.

Sources: https://developers.google.com/workspace/drive/api/guides/manage-changes
https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks
https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/troubleshooting-webhooks

Expose narrow future DP tools for Codex: read project brief, list approved work,
read evidence, draft next steps/updates, propose an alignment decision. Time/pay
changes and management approvals remain separate authenticated actions. Never
let instructions embedded in source files override permissions or accepted scope.

AI should classify/summarize, ask one question, flag an evidence-backed mismatch,
and draft weekly/customer updates. Use a small bounded model for routine extraction
and a stronger model for review-worthy synthesis if justified by evaluated quality
and cost. Real model availability is now verified, generation quality is not.
Add usage/cost counters, cache by source version, latency/error telemetry without
private text, and a visible degraded mode rather than silently implying AI worked.

## Program management that accelerates MVPs

One approved Outcome card per contract: commitment/source, next demonstrable
result, acceptance evidence, owner, aggressive internal target, dependencies,
remaining uncertainty, next customer-feedback opportunity and transition/sales
follow-up. Internal targets are not customer promises. Break near-term work into
small reviewable results while allowing multi-day engineering tasks with checkpoints.

Default WIP proposal: one primary active work block per worker; switching project
requires an explicit switch/proposal, with overhead available. A timeboxed unknown
should yield a finding/decision, not run forever. Early demo feedback informs
which remaining enhancements Erik accepts. Existing ClickUp remains reference;
export/preserve history before any approved replacement.

Manager view should prioritize only: actual-hours/payment risk, blocked decision,
scope drift, threatened target, useful result ready to review. Each item includes
evidence, consequence and one recommended action. Suggested cadence is a brief
morning priorities review, batched nonurgent queue and end-of-day decision summary;
cadence and notification rules must be explicitly configured, not silently scheduled.

## Pilot acceptance and expansion decision

Measure clock reliability/corrections, interaction burden, source-link retrieval,
time to unblock work and outcome/evidence coverage. Do not reward message volume,
keystrokes or hours of AI use. Proposed pilot targets: start/stop acknowledged in
seconds, routine update under a minute, at most one unnecessary follow-up, and a
manager review that fits in ten minutes on an ordinary day. These are product
targets to test, not measured performance or disciplinary thresholds.

Test retries/outages, inaccessible/private links, wrong-account use, revoked
access, duplicate/out-of-order events, ambiguous/overhead work, repeated legitimate
work, model refusal/injection, timezone boundaries and contractor/payroll paths.
First test with Erik, then a verified currently engaged worker; selection must use
the current roster, not historical AJ/intern beta assumptions. Broaden only after
records and worker experience agree. Screenshots/audio and a Sites hosting pivot
remain separate proposed decisions, not prerequisites or silently enabled features.
