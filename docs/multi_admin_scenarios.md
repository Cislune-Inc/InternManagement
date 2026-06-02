# Multi-Admin Scenario Guide

This document maps the bot's **current** multi-admin behavior using an **equal-peer** model.

It is intentionally grounded in today's implementation:

- Any configured admin can use the admin console.
- Many admin notices fan out to all configured admins.
- Any admin can currently resolve reviews, unblocker drafts, task redirects, and resume actions.
- There is no explicit claim, owner, or lock on a pending admin item.

Use this guide to test the current behavior before deciding what hardening features to add.

## Current Model

### Strengths

- Shared admin coverage works well for read-only visibility and fast response.
- Named-admin help requests are a natural fit when an intern wants a specific person.
- Either admin can step in on reviews, blockers, and task control without extra setup.

### Weak spots

- A single admin item can attract duplicate effort because both admins may receive it.
- There is no built-in "George is handling this" signal.
- Mutating actions depend on current state at execution time, not on a prior claim.
- Sequential stale actions are usually rejected cleanly, but near-simultaneous human decisions can still conflict.

## Scenario Matrix

### Helpful scenarios

| Scenario | How to run it | Expected current behavior | Classification |
|---|---|---|---|
| Shared blocker coverage | Intern asks for admin help without naming one person | Both admins can be notified and either one can respond | Safe/useful now |
| Review backlog relief | Two interns finish tasks close together and each admin handles one | Reviews should resolve independently per intern/session | Safe/useful now |
| Expertise routing by name | Intern asks for help from `George` or `Erik` by name | Only the named admin should be targeted | Safe/useful now |
| Availability handoff | Admin A reads an alert, Admin B performs the action later | Admin B can act from current state without hidden ownership | Safe/useful now |
| Parallel visibility without action | Both admins run read-only commands like `review.pending_tasks` or `task.status` | Both should see the same current state picture | Safe/useful now |

### Conflict scenarios

| Scenario | How to run it | Expected current behavior | Classification |
|---|---|---|---|
| Double review resolution | Both admins act on the same pending task review | First resolution should win; a later sequential action should be rejected as stale | Safe but noisy |
| Double unblocker resolution | Both admins act on the same unblocker draft | First resolution should win; later sequential action should be rejected as stale | Safe but noisy |
| Conflicting task direction | One admin switches a task while another prioritizes or resumes the same intern | Intern may receive mixed work directions | Conflict-prone |
| Noise from all-admin fanout | A single review or blocker notice reaches both admins | Both may assume ownership and duplicate work | Conflict-prone |
| Stale-read then write | Admin A reads state, Admin B changes it, Admin A acts later | Outcome depends on whether state has already been cleared or changed | Conflict-prone |
| Recovery versus approval race | Intern becomes unblocked while an unblocker draft is pending and an admin still tries to approve it | Sequential stale approval should be rejected, but timing is human-dependent | Safe but noisy |
| Review versus next-task race | One admin closes a task while another redirects the same intern | The intern can be pushed into overlapping onboarding and direction changes | Conflict-prone |
| Manual admin intervention during active prompts | Admin changes task during onboarding, lunch, or closeout prompts | Prompt state can compete with admin intent | Requires future coordination feature |

## Manual Run Template

For each scenario, capture:

- initiating admin
- second admin
- intern
- starting session stage
- starting active task
- commands sent
- outbound DMs sent to intern/admins
- final session stage
- final active task
- final pending review / pending unblocker state
- final timer / `time_summary`
- ClickUp outcome
- result label: correct, duplicated, contradictory, or noisy

## Evidence to inspect

After each run, inspect:

- per-user `session.json`
- per-user `state_machine_changes.jsonl`
- per-user `transcript.md`
- ClickUp task status and comments
- `time_summary` and active timer state

## Prioritized failure modes to harden first

1. **No ownership/claim semantics on pending review items**
2. **All-admin fanout creates duplicate human effort**
3. **No explicit visibility for "another admin is already handling this"**
4. **Conflicting task-control actions can target the same intern concurrently**
5. **Prompt-driven intern flows can overlap with manual admin interventions**

## Automated coverage in repo

Current automated coverage for the current-state model lives mainly in:

- [tests/test_runtime.py](</C:/Users/George Ore/Documents/InternManagment/tests/test_runtime.py>)
- [tests/test_admin_commands.py](</C:/Users/George Ore/Documents/InternManagment/tests/test_admin_commands.py>)

Those tests currently validate:

- multiple admins are accepted by the admin console
- read-only review visibility is identical across admins
- named admin help requests route to the named admin only
- default admin notices fan out to all admins
- a second sequential review or unblocker resolution is rejected after the first one clears the pending state
