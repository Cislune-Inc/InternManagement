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
- A consolidated `storage/dashboard/time_tracking/time_tracking.csv` rollup makes archived daily hours visible in one local file, including review-status columns that help separate likely-correct days from ones that need attention.

## Directory layout

```text
InternManagement/
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
    time_tracking/
      time_tracking_dashboard.html
      time_tracking.csv
      retro_backfill/
        20260615-090000/
          audit.csv
          summary.json
  people/
    Alex Example/
      profile.json
      2026-05-27/
        hours_backfill_explanation.json
        manual_time_edits.jsonl
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

Mac/Linux quickstart:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
mkdir -p config
cp config_templates/agent.config.example.json config/agent.config.json
cp config_templates/roster.example.csv config/roster.csv
```

If you already transferred a live local config from another machine, copy those files into `config/` instead of the example files.

## Run

```bash
.venv/bin/python -m agent.main
```

## Demo

Run a local simulated workday without sending real Discord DMs:

```bash
.venv/bin/python -m agent.demo
```

Or target a specific active `user_key` from `config/roster.csv`:

```bash
.venv/bin/python -m agent.demo --user alex
```

Send a short scripted live demo to one chosen active roster user on Discord:

```bash
.venv/bin/python -m agent.demo --mode live --user alex
```

List the active `user_key` values available for the live demo:

```bash
.venv/bin/python -m agent.demo --list-users
```

## Historical Hours Backfill

Retroactively rebuild prior archived hours and write an audit bundle:

```bash
.venv/bin/python -m agent.backfill_hours --dry-run
```

Apply the reconstructed hours back into archived `session.json` files, write per-day explanation files, and rebuild the main CSV rollup:

```bash
.venv/bin/python -m agent.backfill_hours --apply
```

Open `storage/dashboard/time_tracking/time_tracking_dashboard.html` in a browser to review the read-only hours snapshot and any discovered backfill audit runs.

That exported HTML file is read-only. To manually correct past clock-in / clock-out segments, launch the local editor server instead:

```bash
.venv/bin/python -m agent.hours_editor
```

Then open `http://127.0.0.1:8765/` in a browser.

The localhost editor/dashboard is the primary live view:

- `http://127.0.0.1:8765/` and `http://127.0.0.1:8765/time` serve the editable time-tracking dashboard
- `http://127.0.0.1:8765/work` serves the work dashboard with recent per-intern work, status, blockers, active task, and available ClickUp tasks
- `http://127.0.0.1:8765/payroll` serves payroll and project-labor review
- `http://127.0.0.1:8765/exceptions` serves the manager exception queue for blockers, incomplete close-outs, admin reviews, task/timer mismatches, uncertain Slack routing, and integration failures
- `http://127.0.0.1:8765/health` serves service health, integrations, storage usage, the operational issue queue, and Slack routing review
- it reads fresh dashboard data from disk on request
- while it is open, it auto-refreshes every 10 minutes without resetting your filters
- if you are actively editing a past day, background refresh pauses until you close the editor panel
- the Hours Rollup view now includes `Likely correct`, `Needs review`, `Likely wrong`, and `In progress` review states with per-day reasons so you can compare local truth against the external hours website faster
- when the LaunchAgent is bound to `0.0.0.0`, use this Mac's LAN IP in place of `127.0.0.1` to reach the dashboards from the local network

## Slack Project Updates

Slack posting is optional and disabled unless the live local config enables it. To turn it on, set `SLACK_BOT_TOKEN` in `.env`, add `slack_user_id` values to `config/roster.csv` so updates can tag interns, and add `slack.project_routes` in `config/agent.config.json` that map ClickUp task/list/folder IDs or conservative name patterns to Slack channel IDs. Task-name matching follows the active task's ClickUp parent ancestry, so nested intern work can roll up to its contract channel.

### Slack-Only Workers

Don Pollo can run the same clock-in, task selection, ClickUp timer, lunch, status, and clock-out workflow through Slack DMs. Slack inbound messaging uses Socket Mode so the local Mac does not need a public webhook:

1. Enable Socket Mode and Event Subscriptions for the Don Pollo Slack app.
2. Subscribe the bot to the `message.im` event.
3. Add an app-level token with `connections:write` to `.env` as `SLACK_APP_TOKEN`.
4. Keep `SLACK_BOT_TOKEN` configured with DM/message and file permissions.
5. Add the worker to `config/roster.csv` with `slack_user_id` and `preferred_transport=slack`. `discord_user_id` may be blank for Slack-only workers.

The optional roster policy columns are documented in `config_templates/roster.example.csv`. Existing workers default to the current intern-style workflow. `worker_type`, compliance flags, task-aware check-in overrides, Gusto UUIDs, and local labor cost rates only change behavior when explicitly configured.

## Monday Payroll And Project Labor

Generate an approval-first bundle for the prior completed week:

```bash
.venv/bin/python -m agent.payroll_export
```

Or target a specific week-ending date:

```bash
.venv/bin/python -m agent.payroll_export --week-ending 2026-08-02
```

The bundle is written under `storage/dashboard/payroll/<week-ending>/` and copied to `storage/dashboard/payroll/latest/`. It includes:

- `payroll_review.csv` with shift, meal, regular, overtime, and review fields
- `project_labor.csv` with task-to-project labor allocation
- `nasa_project_labor.csv` as a source-linked reporting backup
- `project_summary.csv` with budget-hour and labor-cost columns
- `compliance_events.csv`
- `gusto_time_sheets.json`, deliberately marked approval-required and not submitted

Open `http://127.0.0.1:8765/payroll` for the human-readable review page and downloads. Review rows can be resolved there with a required reviewer name and note. Resolutions are tied to an evidence fingerprint, so changing the underlying hours automatically reopens the row. Optional learned resolution is deliberately limited to same-worker task-time variances; meal, overtime, compliance, and incomplete-segment reviews are never learned away.

Missing Gusto mappings are informational and do not block local review. Only mapped workers are included in `gusto_time_sheets.json`; everyone remains visible in the local payroll and project exports. The installed macOS LaunchAgent runs the exporter each Monday at 7:00 AM. Gusto production submission remains disabled until the company has an approved integration and an operator has reviewed the bundle.

Generate a read-only Slack/ClickUp identity proposal without changing the live roster:

```bash
.venv/bin/python -m agent.workforce_mapping
```

The result is `storage/dashboard/payroll/workforce_identity_candidates.csv` and is downloadable from the payroll dashboard. Exact identity matches are kept separate from worker-type estimates so uncertain employees and contractors can be confirmed before onboarding.

When enabled, the bot can post lightweight project-channel updates during configured work hours. It summarizes only new, interesting intern activity, attaches fresh progress images that have useful context, spaces posts out with `slack.min_post_interval_minutes`, and skips posting when there is nothing meaningful to say. If a task cannot be confidently mapped and `slack.unmapped_channel_id` is configured, the update goes there with a mapping-review note instead of guessing a project channel.

Set `slack.practice_channel_id` to force every daily update and weekly recap into one test channel while retaining the configured production route labels. Clear that value only when the production channel map is ready.

Weekly photo recaps use reactions on previously posted progress images. The recap ranks images by configured positive reactions such as `:fire:`, `:heart:`, `:rocket:`, and `:clap:`.

Admins can optionally add `slack_user_id` to their `admins` entry. Don Pollo mirrors
actionable admin notices there and sends deduplicated operational failures to that
Slack user. Uncertain Slack routes can be assigned from the System Health page;
the chosen task or session override is persisted in SQLite and used by future posts.

Operator feedback on daily updates uses lightweight Slack reactions:

- `:white_check_mark:` means useful
- `:x:` or `:twisted_rightwards_arrows:` means wrong task or channel
- `:repeat:` means duplicate
- `:memo:` means too detailed

Don Pollo checks these reactions every six hours. Negative feedback enters the
Manager Queue. Channel-only errors can be corrected there; when the active ClickUp
task/project is wrong, use the audited correction panel on the Work Dashboard.

Optional filters:

- `--user <user_key>`
- `--from YYYY-MM-DD`
- `--to YYYY-MM-DD`

## Manual Hours Editor

The local hours editor is for archived past workdays only. It lets an operator:

- choose an intern and past workday from the Hours Rollup view
- edit the full set of work segments for that day
- require both `edited by` and `reason`
- preview recalculated clocked-in and task-tracked totals before saving

Save path:

- reads the live session from SQLite first
- rewrites the archived `session.json`
- appends `manual_time_edits.jsonl` beside that day
- rebuilds `storage/dashboard/time_tracking/time_tracking.csv`
- rebuilds `storage/dashboard/time_tracking/time_tracking_dashboard.html`

## Always-On Local Dashboard On macOS

Versioned LaunchAgent templates for both always-on services live under `ops/`.
Install them into `~/Library/LaunchAgents/` once, then use the checked-in restart
and verification scripts after deployments:

```bash
cp ops/com.pm.internmanagement.bot.plist ~/Library/LaunchAgents/
cp ops/com.pm.internmanagement.time-tracking.plist ~/Library/LaunchAgents/
cp ops/com.pm.internmanagement.backup.plist ~/Library/LaunchAgents/
cp ops/com.pm.internmanagement.integration-health.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pm.internmanagement.bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pm.internmanagement.time-tracking.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pm.internmanagement.backup.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pm.internmanagement.integration-health.plist
ops/restart-services.sh
```

`ops/restart-services.sh` restarts the bot and dashboard, then verifies Discord
readiness, backup freshness, scheduled integration checks, and
`http://127.0.0.1:8765/health`. Run `ops/verify-services.sh` for a read-only health
check.

The backup LaunchAgent creates an encrypted core backup nightly at 2:30 AM.
It includes SQLite, `.env`, live config, sessions, transcripts, and audit evidence,
with 30-day backup retention. The encryption key is created at
`secrets/backup.key`; preserve a separate secure copy of that key because encrypted
backups cannot be restored without it. Progress images are deliberately excluded
from nightly backups and can be included in an explicit full backup:

```bash
.venv/bin/python -m agent.backup
.venv/bin/python -m agent.backup --include-images
.venv/bin/python -m agent.backup --verify backups/<backup-file>.tar.gz.enc
.venv/bin/python -m agent.backup --restore backups/<backup-file>.tar.gz.enc --restore-to /tmp/don-pollo-restore
```

Restores always go to a separate staging directory and never overwrite live files.
The integration-health LaunchAgent checks the bot, database, backup age, dashboard,
Discord, Slack, ClickUp, and OpenAI every 30 minutes. Failures are deduplicated in
SQLite and sent to configured Slack admins such as Erik.

## Development And Storage

Install test-only tools separately from production dependencies:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Create a non-destructive inventory of local storage:

```bash
.venv/bin/python -m agent.storage_maintenance
```

The report is written to
`storage/dashboard/system/storage_inventory.json`. It identifies large files and
storage categories but deliberately never deletes session evidence, transcripts,
or progress images.

Preview old daily folders for encrypted archival:

```bash
.venv/bin/python -m agent.storage_maintenance --archive-before 2026-07-01
```

Creating the verified archive requires `--apply`. Source folders are retained unless
the operator separately adds `--delete-source`; this prevents routine maintenance
from silently deleting historical evidence.

## What the bot does

- DMs each active user Monday through Friday at 9:00 AM asking if they have clocked in.
- Interprets that 9:00 AM check-in in each intern's own roster timezone.
- Repeats hourly until they confirm or the 1:00 PM cutoff is reached.
- After clock-in, asks for:
  - today's plan
  - a before-start picture
  - optional early blockers
- Uses ClickUp context to give plan feedback.
- Uses a 90-minute average check-in cadence, adapting between 45 and 120 minutes from the task duration supplied during onboarding.
- Lets a clocked-in intern mark a lunch break, pauses task timing, and checks every 30 minutes until they say they are back.
- Lets an intern ask for their hours for the current week in DM and replies with both clocked-in time and task-tracked time plus a daily breakdown.
- Can retroactively rebuild prior archived hours from local artifacts, writing a durable `hours_backfill_explanation.json` beside each applied day plus a per-run audit bundle under `storage/dashboard/time_tracking/retro_backfill/`.
- Generates a local `storage/dashboard/time_tracking/time_tracking_dashboard.html` file with filterable archived hours plus expandable backfill-audit details.
- Can post task-linked Slack project updates and weekly progress-photo recaps when routing is configured. Uncertain routes are quarantined, vague/workflow chatter is rejected, and later updates and photos stay in the worker/task thread.
- Supports the same workflow in Slack DMs for roster users who are not on Discord.
- Warns a tracked worker near 4.5 recorded hours and automatically pauses work time at the configured meal deadline if lunch has not started.
- Keeps clocked-in totals equal to recorded work segments; lunch never triggers a flat automatic time deduction.
- Uses practical intern wording for lunch and end-of-day coaching, while configured employees and contractors receive explicit approval language.
- Notifies an admin after an automatic meal pause. Covered hourly workers receive an overtime warning before the configured limit and are automatically clocked out at the limit unless approval is stored; salaried/exempt and external workers can opt out in the roster.
- Never deducts, revokes, or changes recorded time merely because a compliance reminder was sent.
- Generates Monday payroll, project-budget, overhead-review, compliance, NASA-reporting, and Gusto-ready export artifacts.
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
- Can draft project, overhead, blocker, and unblocker tasks through DM, but requires configured management approval before creating them in ClickUp.
- Can keep per-task timer state locally and sync completed ClickUp time entries on task pause / task switch / clock-out when the workspace token has permission to write them.
- Can warn and auto-clock out an inactive worker after the configured interval, stopping the local task timer, putting the active ClickUp task on `hold`, and preserving tracked time in the daily log.
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
- `slack.enabled` defaults to `false`. When enabling it, configure `SLACK_BOT_TOKEN`, roster `slack_user_id` values, and `slack.project_routes` so updates go to the correct project channels. Use `slack.unmapped_channel_id` for mapping-review posts when a ClickUp task is not confidently routed.
- ClickUp's assignee timer APIs are permission-sensitive. The bot will query/start assignee-linked timers when allowed by the token and workspace, and otherwise it falls back to local running-timer state plus synced closed time entries.
- `schedule.auto_clock_out_after_hours` controls when a clocked-in but silent worker is treated as clocked out automatically. `schedule.auto_clock_out_warning_minutes` controls the stateful warning lead time. The production recommendation is `1` hour with a `15` minute warning.
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

The full grouped command catalog and scenario flowchart live in [docs/admin_console.md](docs/admin_console.md).
The current equal-peer multi-admin test matrix and conflict guide live in [docs/multi_admin_scenarios.md](docs/multi_admin_scenarios.md).
