# Team check-ins and reviewed project summaries

This packet implements the accepted private-check-in and project-summary direction.
Workforce enrollment remains a separate, explicit change. Do not activate the
historical all-active roster, guess payroll classifications or announce readiness
before the restricted kiosk account and manager-editor controls are verified.

## Runtime behavior

- Optional private prompt after about two hours without a work update; at most
  three attempts per workday. Recent result, plan, detail or next-step entries
  suppress it. No prompts while clocked out, on lunch or paid rest; fresh starts
  and recorded rest returns provide a new focus window.
- `snooze` defers progress prompts for one hour. It does not grant overtime,
  waive break rules or extend the separate four-hour attendance ceiling.
- Existing OpenAI work coaching remains advisory; saved original statements and
  clock records survive model failures. Check-ins themselves do not call a model.
- `work update <result or blocker>` then `work next <next step/help needed>` and
  **Preview update** prepare an exact, versioned report.
- `work confirm SH-id` stays private. `work share SH-id` explicitly sends the
  preview to its configured project channel and confirms the report.
- No inference from legacy ClickUp routes. Only explicit project/channel pairs
  are allowed; unknown or changed destinations require another preview.
- No clock/payroll data, manager notes, arbitrary private DMs or AI-generated
  claims enter the summary. Worker-supplied reference links are labeled unverified.
- One channel attempt per worker/channel per 30 minutes. Duplicate and stale
  versions do not send; uncertain delivery requires manager verification rather
  than an automatic retry. Review destination, content and source permissions.
- This release does not read project-channel history. Bot scopes must be separately
  reviewed if source retrieval is added; a user's connectors are not bot permissions.

## Operator activation

Verify current channel IDs and app membership, then run from the repository:

```sh
PYTHONPATH=. .venv/bin/python ops/configure_work_checkins.py --enable-checkins --route project_key=CHANNEL_ID
```

Review the dry run, add `--apply`, restart and verify. The tool preserves the
current cohort and writes a config backup. Do not send synthetic staff updates
or create paid test punches. Verify one genuine worker cycle before announcement.

## Conditional onboarding draft — not sent

Recipients: only the verified, enrolled current team. Exclude separately handled
workers/vendors and future starters until their confirmed start. Avoid a channel
announcement claiming everyone is enrolled when they are not.

> Don Pollo is ready for your timekeeping and short work updates. Use the existing
> **Don Pollo Project Updates** Slack app.
>
> One-time setup at the shop: open the app's Home tab, choose **Set up / reset PIN**,
> then select your name on the Mini within ten minutes and type your new PIN twice.
> Use 2–6 digits; four or more is recommended. Never send your PIN in Slack.
>
> Start work and return from lunch/rest at the Mini. Use the Mini or Slack for
> lunch, paid rest and clock-out. DP sends a private message when you can return;
> returning still requires the Mini. **My hours** shows your recorded time.
>
> In Messages, tell DP the project and what you intend to accomplish. Short,
> concrete updates are enough: what changed, what is blocked, and what comes next.
> Link company work where useful. Expect a gentle check-in around two hours; reply
> `snooze` when you need focus time. Use **Preview update** and the displayed share
> command to put a reviewed result in the relevant project channel.
>
> For this enrolled group, DP replaces the old kiosk/Discord clock. If something
> fails or the recorded time is wrong, Slack Erik your actual times and breaks;
> all worked time must be reported. Offsite work and extra hours still need Erik's
> advance authorization. Work descriptions never determine whether time is recorded.

Replace this conditional text with verified deployment details before sending.
