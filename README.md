# Intern Management Agent

This project runs a Discord-based intern check-in agent with **local-first storage**. All operational config, chat logs, images, session snapshots, and dashboards stay on the computer running the agent.

It is designed around four responsibilities:

1. Local files hold the live config, roster, transcripts, images, and dashboard.
2. Discord handles the daily DM workflow with each intern.
3. The configured admin can command the bot through Discord DMs for status, reminders, validation, and manager reports.
4. A local SQLite state store keeps the bot reliable between restarts.

## Why this architecture is better

Compared with the original Drive-backed design, this version is simpler and more robust:

- All config and archives stay local, so the agent works without Google Drive credentials.
- The local filesystem is the source of truth for transcripts, images, and per-user history.
- ClickUp stays a reporting target instead of becoming the workflow state machine.
- Discord remains the live coordination channel with interns and admins.
- Every user gets a predictable local folder with daily subfolders for transcripts and images.
- Each daily `session.json` now includes a compact `time_summary` block with clocked-in time, task-tracked time, and per-task totals.

## Directory layout

```text
InternManagment/
  agent/
    ...
  config_templates/
    agent.config.example.json
    roster.example.csv
  bootstrap.example.json
  requirements.txt
```

## Local runtime layout

When configured, the agent uses local paths like these:

```text
config/
  agent.config.json
  roster.csv
data/
  agent_state.sqlite3
storage/
  dashboard/
    dashboard.md
  people/
    Alex Example/
      profile.json
      2026-05-27/
        session.json
        state_machine_changes.jsonl
        transcript.md
        images_manifest.json
        images/
          090501_1_wiring-harness-closeup.jpg
```

## Setup

1. Create a Python virtual environment.
2. Install `requirements.txt`.
3. Copy `.env.example` to `.env`.
4. Copy `bootstrap.example.json` to `bootstrap.local.json`.
5. Create a local `config/` folder.
6. Copy the example files from `config_templates/` into `config/` and customize them.
7. Create a Discord bot, invite it, and enable the Message Content intent in the Discord developer portal.
8. Add the Discord bot token and ClickUp token to `.env`.
9. Run the agent.

## Run

```powershell
.\.venv\Scripts\python -m agent.main
```

## Demo

Run a local simulated workday without sending real Discord DMs:

```powershell
.\.venv\Scripts\python -m agent.demo
```

Or target a specific active `user_key` from `config/roster.csv`:

```powershell
.\.venv\Scripts\python -m agent.demo --user Andrew
```

Send a short scripted live demo to one chosen active roster user on Discord:

```powershell
.\.venv\Scripts\python -m agent.demo --mode live --user Andrew
```

List the active `user_key` values available for the live demo:

```powershell
.\.venv\Scripts\python -m agent.demo --list-users
```

## What the bot does

- DMs each active user Monday through Friday at 9:00 AM asking if they have clocked in.
- Interprets that 9:00 AM check-in in each intern's own roster timezone.
- Repeats hourly until they confirm or the 1:00 PM cutoff is reached.
- After clock-in, asks for:
  - today's plan
  - a before-start picture
  - optional early blockers
- Uses ClickUp context to give plan feedback.
- Checks in every 30 minutes until the user clocks out.
- Lets a clocked-in intern mark a lunch break, pauses task timing, and checks every 30 minutes until they say they are back.
- Alerts the configured admin if someone appears stuck for four hours.
- Accepts admin-only Discord DM commands for roster status, reminder actions, photo review, config checks, and ClickUp-backed planning summaries.
- Uses the OpenAI API as an optional signal layer for intern messages and advanced admin analysis, but normal admin command routing is deterministic.
- If a 30-minute check-in reveals that an intern is stuck, the bot now offers two clearer paths: direct help from a named admin, or an unblocker task draft that goes to admin review before it is created in ClickUp.
- If the blocker clears, either the intern or an admin can tell the bot they are unblocked, and the bot will move the active task back to `in progress`, restart task tracking, and clear the stuck state.
- Requires an end-of-day picture and written wrap-up when the user clocks out.
- Saves text and image artifacts into local per-user folders.
- Appends a per-day `state_machine_changes.jsonl` file that records each real session-state transition with before/after snapshots, changed fields, and trigger context.
- Runs image recognition on supported image attachments, saving descriptive filenames plus local image metadata.
- Infers the user's active ClickUp task from their assigned work plus chat context.
- Can ask a clocked-in intern to re-confirm their active task every five minutes until task onboarding is complete if no active task/timer is confirmed.
- Can run a task-switch onboarding flow that asks for the new task plan, predicted blockers, and a fresh photo before activating the task.
- Can let an intern mark a task finished, ask for both a completion summary and a completion photo when needed, then pause the task and put it into an admin-review loop before closure.
- Hides tasks that were just closed from the next-task picker so interns do not immediately reselect finished work unless an admin explicitly reopens or reassigns it.
- Can automatically move the inferred ClickUp task into `in progress`, `hold`, or `complete` when the intern workflow strongly supports it.
- Can create Mission Board blocker tasks through a short DM prompt sequence instead of relying on people to fill ClickUp out by hand.
- Can keep per-task timer state locally and sync completed ClickUp time entries on task pause / task switch / clock-out when the workspace token has permission to write them.
- Can auto-clock out an intern after six hours with no inbound check-in, stopping the local task timer automatically, putting the active ClickUp task on `hold`, and preserving the tracked time in the daily log.
- Keeps overnight activity in the existing `YYYY-MM-DD` folder layout by using a per-user local workday rollover, so work before `03:30` stays in the previous day's folder.
- Supports multiple clock-in / clock-out segments inside one local workday bucket without re-running the full morning intake every time.
- Can suggest likely unassigned Mission Board tasks for the next day based on priority plus overlap with the intern's current work context.
- Optionally attaches newly received files to the inferred or pinned ClickUp task.
- Flushes new developments into ClickUp after 10 minutes of user inactivity.

## Notes

- The operational config is read from the local `agent.config.json` file.
- Unknown Discord users are ignored unless they exist in the roster.
- OpenAI-backed advice is optional. If `OPENAI_API_KEY` is absent, the bot falls back to a heuristic advisor.
- If your preferred OpenAI model is not available to the org tied to your API key, set `BACKUP_OPENAI_MODEL` in `.env` and the agent will retry that model before disabling the OpenAI-assisted feature.
- Only one `agent.main` process should run at a time. The app now enforces a lock file in `data/agent.lock`.
- The roster identifies a person, not a pinned task. Use `clickup_user_id` or `clickup_user_email` so the bot can inspect that person's assigned tasks and choose the most relevant active task for the day.
- The roster can now include an optional `timezone` column per user. If it is blank, the bot falls back to the main `timezone` in `agent.config.json`.
- `clickup_task_id` and `clickup_list_id` are no longer supported roster columns. Per-user task/list overrides were removed so task selection stays runtime-driven.
- `admins` in `agent.config.json` is the preferred way to define named admins for DM escalation and admin-targeted stuck-help prompts. The legacy `admin_discord_user_id` value is still used as the primary fallback.
- If you want the bot to assign ClickUp tasks directly to an admin, include that admin's `clickup_user_id` or `clickup_user_email` in `admins`. The runtime can also attempt a workspace-member lookup by name, but explicit IDs are more reliable.
- `clickup.mission_board_list_id` should point at the List where new blocker tasks and next-task suggestions should come from.
- ClickUp's assignee timer APIs are permission-sensitive. The bot will query/start assignee-linked timers when allowed by the token and workspace, and otherwise it falls back to local running-timer state plus synced closed time entries.
- `schedule.auto_clock_out_after_hours` controls when a clocked-in but silent intern is treated as clocked out automatically. The default is `6`.
- `schedule.workday_rollover_time` controls when a user's local workday rolls into the next date folder. The default is `03:30`.
- Lunch breaks suspend the inactivity auto-clock-out timer and keep the current ClickUp task in `in progress` while local task timing is paused.
- Manual clock-out also parks the current active ClickUp task on `hold`. Task closure should happen through the finish-task admin review flow instead of ordinary clock-out.
- Daily image metadata is written to `images_manifest.json`, and transcripts include any generated descriptions/tags for saved images.
- The admin console is grouped and deterministic by default. Normal admin control uses `help`, `menu`, `flow`, and `run <command-id> ...`.
- `admin_console.enable_ai_fallback` defaults to `true`. When the admin sends text that does not parse as a deterministic command, the bot first tries to suggest the closest existing command and then falls back to a direct AI answer from current runtime data.
- You can optionally set `OPENAI_INTERFACE_MODEL` in `.env` to control the model used for intern signal interpretation and command-suggestion routing. It defaults to `gpt-4.1-mini`, and `BACKUP_OPENAI_MODEL` is used as a retry target if the primary model errors.

## Admin Console

The admin Discord user ID from `agent.config.json` can DM the bot directly with:

- `help` or `menu`
- `flow`
- `help <group-id>`
- `run <command-id> key=value ...`
- `back`, `home`, `cancel`

Examples:

- `run presence.clocked_in`
- `run presence.remind_clock_in`
- `run task.status user=Andrew`
- `run task.switch user=Andrew task="formalize project tree"`
- `run review.close user=Andrew`
- `run review.rework user=Andrew comments="Fix the wiring alignment first."`
- `run system.validate`
- `run advanced.interpret text="how many people clocked in today"`

The full grouped command catalog and scenario flowchart live in [docs/admin_console.md](</C:/Users/George Ore/Documents/InternManagment/docs/admin_console.md>).
The current equal-peer multi-admin test matrix and conflict guide live in [docs/multi_admin_scenarios.md](</C:/Users/George Ore/Documents/InternManagment/docs/multi_admin_scenarios.md>).
