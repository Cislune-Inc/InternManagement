from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(slots=True, frozen=True)
class AdminCommandArgument:
    name: str
    description: str
    required: bool = True
    example: str | None = None


@dataclass(slots=True, frozen=True)
class AdminCommandDefinition:
    command_id: str
    label: str
    help_text: str
    group_id: str
    read_only: bool
    advanced: bool = False
    requires_ai: bool = False
    confirm_required: bool = False
    args: tuple[AdminCommandArgument, ...] = ()
    examples: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    handler_name: str = ""
    preview_name: str | None = None


@dataclass(slots=True, frozen=True)
class AdminCommandGroupDefinition:
    group_id: str
    label: str
    description: str
    command_ids: tuple[str, ...] = ()
    advanced: bool = False


@dataclass(slots=True, frozen=True)
class AdminScenarioDefinition:
    scenario_id: str
    label: str
    description: str
    group_id: str
    command_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class AdminActionPreview:
    title: str
    summary: str
    command_id: str
    args: dict[str, str] = field(default_factory=dict)
    confirm_label: str = "Confirm"
    cancel_label: str = "Cancel"


@dataclass(slots=True)
class AdminActionRequest:
    token: str
    admin_user_id: int
    command_id: str
    args: dict[str, str]
    preview: AdminActionPreview
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_expired(self, ttl_minutes: int) -> bool:
        return datetime.now(timezone.utc) - self.created_at > timedelta(minutes=ttl_minutes)


@dataclass(slots=True)
class AdminMenuState:
    token: str
    admin_user_id: int
    screen: str
    history: list[tuple[str, str | None]] = field(default_factory=list)
    group_id: str | None = None
    command_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_expired(self, ttl_minutes: int) -> bool:
        return datetime.now(timezone.utc) - self.created_at > timedelta(minutes=ttl_minutes)


@dataclass(slots=True, frozen=True)
class ParsedAdminInput:
    kind: str
    raw: str
    group_id: str | None = None
    command_id: str | None = None
    args: dict[str, str] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True, frozen=True)
class AdminConsoleRegistry:
    groups: tuple[AdminCommandGroupDefinition, ...]
    commands: tuple[AdminCommandDefinition, ...]
    scenarios: tuple[AdminScenarioDefinition, ...]
    groups_by_id: dict[str, AdminCommandGroupDefinition]
    commands_by_id: dict[str, AdminCommandDefinition]
    scenarios_by_id: dict[str, AdminScenarioDefinition]

    def command(self, command_id: str) -> AdminCommandDefinition | None:
        return self.commands_by_id.get(command_id)

    def group(self, group_id: str) -> AdminCommandGroupDefinition | None:
        return self.groups_by_id.get(group_id)

    def group_commands(self, group_id: str) -> list[AdminCommandDefinition]:
        group = self.group(group_id)
        if not group:
            return []
        return [self.commands_by_id[command_id] for command_id in group.command_ids if command_id in self.commands_by_id]


def build_admin_console_registry() -> AdminConsoleRegistry:
    groups = (
        AdminCommandGroupDefinition(
            group_id="attention",
            label="Who Needs Attention?",
            description="See who has checked in, who is quiet, and who needs follow-up right now.",
        ),
        AdminCommandGroupDefinition(
            group_id="blockers",
            label="Blockers / Intervention",
            description="Inspect blockers, see missing artifacts, and nudge people who need help.",
        ),
        AdminCommandGroupDefinition(
            group_id="task",
            label="Change Someone's Work",
            description="Inspect current assignments, switch tasks, resume work, or redirect priorities.",
        ),
        AdminCommandGroupDefinition(
            group_id="review",
            label="Review / Approve Work",
            description="Handle finished-task reviews and unblocker-task drafts that are waiting on admin decisions.",
        ),
        AdminCommandGroupDefinition(
            group_id="evidence",
            label="See Evidence / Photos",
            description="Review before/after images, visual progress, and saved media artifacts.",
        ),
        AdminCommandGroupDefinition(
            group_id="reports",
            label="Reports / Planning",
            description="Generate manager views and tomorrow-planning guidance from local state and ClickUp context.",
        ),
        AdminCommandGroupDefinition(
            group_id="system",
            label="System Checks",
            description="Validate config, verify DM reachability, and inspect ClickUp health.",
        ),
        AdminCommandGroupDefinition(
            group_id="debug",
            label="Debug / Testing",
            description="Reset or replay workflow state safely while you test the bot's behavior.",
            advanced=True,
        ),
        AdminCommandGroupDefinition(
            group_id="advanced",
            label="Advanced Analysis",
            description="Use heavier AI-backed interpretation or deeper analysis commands when you need them.",
            advanced=True,
        ),
    )

    commands = (
        AdminCommandDefinition(
            command_id="presence.clocked_in",
            label="Who clocked in today",
            help_text="List interns who have clocked in today.",
            group_id="attention",
            read_only=True,
            handler_name="_command_presence_clocked_in",
            examples=("run presence.clocked_in",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="presence.no_response",
            label="Who has not responded",
            help_text="List active roster users who have not responded yet today.",
            group_id="attention",
            read_only=True,
            handler_name="_command_presence_no_response",
            examples=("run presence.no_response",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="presence.remind_clock_in",
            label="DM everyone who has not clocked in",
            help_text="Send the configured clock-in reminder to every active roster user who has not clocked in yet.",
            group_id="attention",
            read_only=False,
            confirm_required=True,
            handler_name="_command_presence_remind_clock_in",
            preview_name="_preview_presence_remind_clock_in",
            examples=("run presence.remind_clock_in",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="presence.last_messages",
            label="Last message times",
            help_text="Show every active roster user and when their last inbound message arrived.",
            group_id="attention",
            read_only=True,
            handler_name="_command_presence_last_messages",
            examples=("run presence.last_messages",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="presence.active_count",
            label="Active count",
            help_text="Count interns who are clocked in and not yet clocked out.",
            group_id="attention",
            read_only=True,
            handler_name="_command_presence_active_count",
            examples=("run presence.active_count",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="presence.clocked_out_count",
            label="Clocked-out count",
            help_text="Count interns who have clocked out today.",
            group_id="attention",
            read_only=True,
            handler_name="_command_presence_clocked_out_count",
            examples=("run presence.clocked_out_count",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="presence.active_roster",
            label="Active roster users",
            help_text="List every active roster user the bot is managing today.",
            group_id="attention",
            read_only=True,
            handler_name="_command_presence_active_roster",
            examples=("run presence.active_roster",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="presence.attention",
            label="Who needs attention now",
            help_text="Show a consolidated attention report: no response, stuck, missing photos, and incomplete wrap-ups.",
            group_id="attention",
            read_only=True,
            handler_name="_command_presence_attention",
            examples=("run presence.attention",),
            scenarios=("attention",),
        ),
        AdminCommandDefinition(
            command_id="blockers.stuck",
            label="Who is stuck right now",
            help_text="List interns who are currently marked stuck and show their blocker context.",
            group_id="blockers",
            read_only=True,
            handler_name="_command_blockers_stuck",
            examples=("run blockers.stuck",),
            scenarios=("blockers",),
        ),
        AdminCommandDefinition(
            command_id="blockers.intervention",
            label="Which blockers need admin intervention",
            help_text="Show blockers that have crossed the intervention threshold or were already escalated.",
            group_id="blockers",
            read_only=True,
            handler_name="_command_blockers_intervention",
            examples=("run blockers.intervention",),
            scenarios=("blockers",),
        ),
        AdminCommandDefinition(
            command_id="blockers.dm_stuck",
            label="DM all stuck interns",
            help_text="Message everyone who is currently marked stuck and ask what exact help they need.",
            group_id="blockers",
            read_only=False,
            confirm_required=True,
            handler_name="_command_blockers_dm_stuck",
            preview_name="_preview_blockers_dm_stuck",
            examples=("run blockers.dm_stuck",),
            scenarios=("blockers",),
        ),
        AdminCommandDefinition(
            command_id="blockers.missing_before_photo",
            label="Missing before-start photo",
            help_text="List interns who clocked in but have not sent a before-start photo yet.",
            group_id="blockers",
            read_only=True,
            handler_name="_command_blockers_missing_before_photo",
            examples=("run blockers.missing_before_photo",),
            scenarios=("blockers",),
        ),
        AdminCommandDefinition(
            command_id="blockers.missing_end_of_day",
            label="Missing end-of-day report",
            help_text="List interns who started clock-out but still owe part of the end-of-day handoff.",
            group_id="blockers",
            read_only=True,
            handler_name="_command_blockers_missing_end_of_day",
            examples=("run blockers.missing_end_of_day",),
            scenarios=("blockers",),
        ),
        AdminCommandDefinition(
            command_id="blockers.remind_missing_photos",
            label="Remind missing photos",
            help_text="DM interns who still owe either a before-start photo or an end-of-day photo.",
            group_id="blockers",
            read_only=False,
            confirm_required=True,
            handler_name="_command_blockers_remind_missing_photos",
            preview_name="_preview_blockers_remind_missing_photos",
            examples=("run blockers.remind_missing_photos",),
            scenarios=("blockers",),
        ),
        AdminCommandDefinition(
            command_id="task.status",
            label="Show assigned and current task",
            help_text="Inspect an intern's assigned ClickUp tasks and the task the bot believes they are actively working on.",
            group_id="task",
            read_only=True,
            args=(AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),),
            handler_name="_command_task_status",
            examples=('run task.status user=Andrew',),
            scenarios=("task",),
        ),
        AdminCommandDefinition(
            command_id="task.switch",
            label="Switch an intern to another task",
            help_text="Find the best ClickUp match for a task name, reassign the intern if needed, and start task-switch onboarding.",
            group_id="task",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("task", "Task name or ClickUp task hint", example="formalize project tree"),
            ),
            handler_name="_command_task_switch",
            preview_name="_preview_task_switch",
            examples=('run task.switch user=Andrew task="formalize project tree"',),
            scenarios=("task",),
        ),
        AdminCommandDefinition(
            command_id="task.resume",
            label="Resume work after unblock",
            help_text="Clear a blocker, move the active task back to in progress, and tell the intern to resume.",
            group_id="task",
            read_only=False,
            confirm_required=True,
            args=(AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),),
            handler_name="_command_task_resume",
            preview_name="_preview_task_resume",
            examples=('run task.resume user=Andrew',),
            scenarios=("task", "blockers"),
        ),
        AdminCommandDefinition(
            command_id="task.prioritize",
            label="Redirect to highest-priority task",
            help_text="Tell an intern to focus on the highest-priority assigned ClickUp task instead of the current plan.",
            group_id="task",
            read_only=False,
            confirm_required=True,
            args=(AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),),
            handler_name="_command_task_prioritize",
            preview_name="_preview_task_prioritize",
            examples=('run task.prioritize user=Andrew',),
            scenarios=("task",),
        ),
        AdminCommandDefinition(
            command_id="review.pending_tasks",
            label="Pending task reviews",
            help_text="List interns whose finished task is waiting for admin review.",
            group_id="review",
            read_only=True,
            handler_name="_command_review_pending_tasks",
            examples=("run review.pending_tasks",),
            scenarios=("review",),
        ),
        AdminCommandDefinition(
            command_id="review.overtime_approve",
            label="Approve same-day overtime",
            help_text="Release a worker's same-day overtime restart gate, notify the worker, and notify the other configured approver.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("comments", "Reason and approved work scope", example="Approve one hour to finish the test."),
            ),
            handler_name="_command_review_overtime_approve",
            preview_name="_preview_review_overtime_approve",
            examples=('run review.overtime_approve user=Andrew comments="Approve one hour for testing."',),
            scenarios=("review", "attention"),
        ),
        AdminCommandDefinition(
            command_id="review.quality_restart",
            label="Approve quality-corrected restart",
            help_text="Release a worker's work-detail quality restart gate after reviewing a concrete correction.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="AJ"),
                AdminCommandArgument("comments", "Correction reviewed and allowed work scope", example="Reviewed the revised fixture plan and evidence target."),
            ),
            handler_name="_command_review_quality_restart",
            preview_name="_preview_review_quality_restart",
            examples=('run review.quality_restart user=AJ comments="Reviewed the corrected plan."',),
            scenarios=("review", "attention"),
        ),
        AdminCommandDefinition(
            command_id="review.close",
            label="Close reviewed task",
            help_text="Approve an intern's pending task review and close the reviewed task in ClickUp.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("task", "Optional task name when that intern has multiple pending reviews", required=False, example="formalize project tree"),
                AdminCommandArgument("task_id", "Optional ClickUp task id when that intern has multiple pending reviews", required=False, example="868jun6qg"),
                AdminCommandArgument("comments", "Optional admin note recorded with the approval", required=False, example="Looks good."),
            ),
            handler_name="_command_review_close",
            preview_name="_preview_review_close",
            examples=(
                'run review.close user=Andrew',
                'run review.close user=Andrew task="formalize project tree"',
                'run review.close user=Andrew task_id=868jun6qg comments="Looks good. Close it."',
            ),
            scenarios=("review",),
        ),
        AdminCommandDefinition(
            command_id="review.rework",
            label="Send rework comments",
            help_text="Reject closure for now, send comments back to the intern, and reactivate the task.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("task", "Optional task name when that intern has multiple pending reviews", required=False, example="formalize project tree"),
                AdminCommandArgument("task_id", "Optional ClickUp task id when that intern has multiple pending reviews", required=False, example="868jun6qg"),
                AdminCommandArgument("comments", "What the intern should change before closure", example="Fix the wiring alignment first."),
            ),
            handler_name="_command_review_rework",
            preview_name="_preview_review_rework",
            examples=(
                'run review.rework user=Andrew comments="Fix the wiring alignment first."',
                'run review.rework user=Andrew task_id=868jun6qg comments="Fix the wiring alignment first."',
            ),
            scenarios=("review",),
        ),
        AdminCommandDefinition(
            command_id="review.pending_task_proposals",
            label="Pending new-task proposals",
            help_text="List project and overhead task proposals waiting for Erik or George.",
            group_id="review",
            read_only=True,
            handler_name="_command_review_pending_task_proposals",
            examples=("run review.pending_task_proposals",),
            scenarios=("review", "task"),
        ),
        AdminCommandDefinition(
            command_id="review.task_proposal_approve",
            label="Approve new-task proposal",
            help_text="Approve a project or overhead task proposal and publish it to ClickUp.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("comments", "Optional approval note", required=False, example="Approved."),
            ),
            handler_name="_command_review_task_proposal_approve",
            preview_name="_preview_review_task_proposal_approve",
            examples=('run review.task_proposal_approve user=Andrew',),
            scenarios=("review", "task"),
        ),
        AdminCommandDefinition(
            command_id="review.task_proposal_revise",
            label="Request new-task revision",
            help_text="Send comments back before a project or overhead task is created.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("comments", "What should change", example="Tie this to the CARVE parent task."),
            ),
            handler_name="_command_review_task_proposal_revise",
            preview_name="_preview_review_task_proposal_revise",
            examples=('run review.task_proposal_revise user=Andrew comments="Tie this to CARVE."',),
            scenarios=("review", "task"),
        ),
        AdminCommandDefinition(
            command_id="review.pending_unblockers",
            label="Pending unblocker drafts",
            help_text="List unblocker tasks that are waiting for admin approval or revision.",
            group_id="review",
            read_only=True,
            handler_name="_command_review_pending_unblockers",
            examples=("run review.pending_unblockers",),
            scenarios=("review", "blockers"),
        ),
        AdminCommandDefinition(
            command_id="review.unblocker_approve",
            label="Approve unblocker draft",
            help_text="Approve an unblocker task draft and publish it to ClickUp.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("comments", "Optional admin note recorded with the approval", required=False, example="Looks good."),
            ),
            handler_name="_command_review_unblocker_approve",
            preview_name="_preview_review_unblocker_approve",
            examples=('run review.unblocker_approve user=Andrew',),
            scenarios=("review", "blockers"),
        ),
        AdminCommandDefinition(
            command_id="review.unblocker_revise",
            label="Request unblocker revision",
            help_text="Send revision comments back to the intern instead of creating the unblocker task.",
            group_id="review",
            read_only=False,
            confirm_required=True,
            args=(
                AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),
                AdminCommandArgument("comments", "What needs to change in the draft", example="Tighten the title and describe the dependency."),
            ),
            handler_name="_command_review_unblocker_revise",
            preview_name="_preview_review_unblocker_revise",
            examples=('run review.unblocker_revise user=Andrew comments="Tighten the title."',),
            scenarios=("review", "blockers"),
        ),
        AdminCommandDefinition(
            command_id="evidence.before_after",
            label="Show before and after photos",
            help_text="Send the latest before/after photo pairs the bot has for today.",
            group_id="evidence",
            read_only=True,
            handler_name="_command_evidence_before_after",
            examples=("run evidence.before_after",),
            scenarios=("evidence",),
        ),
        AdminCommandDefinition(
            command_id="evidence.visual_progress",
            label="Which projects have visual progress",
            help_text="List interns who have both a before and after photo today.",
            group_id="evidence",
            read_only=True,
            handler_name="_command_evidence_visual_progress",
            examples=("run evidence.visual_progress",),
            scenarios=("evidence",),
        ),
        AdminCommandDefinition(
            command_id="evidence.image_inventory",
            label="List all image files",
            help_text="Show all saved image files for the day grouped by user.",
            group_id="evidence",
            read_only=True,
            handler_name="_command_evidence_image_inventory",
            examples=("run evidence.image_inventory",),
            scenarios=("evidence",),
        ),
        AdminCommandDefinition(
            command_id="evidence.after_without_wrapup",
            label="After photo without wrap-up",
            help_text="List interns who sent an after photo but still owe written wrap-up text.",
            group_id="evidence",
            read_only=True,
            handler_name="_command_evidence_after_without_wrapup",
            examples=("run evidence.after_without_wrapup",),
            scenarios=("evidence",),
        ),
        AdminCommandDefinition(
            command_id="evidence.photo_delta",
            label="Summarize before/after differences",
            help_text="Compare an intern's start and end photos and summarize the visible differences.",
            group_id="evidence",
            read_only=True,
            args=(AdminCommandArgument("user", "Roster user key or display name", example="Andrew"),),
            handler_name="_command_evidence_photo_delta",
            examples=('run evidence.photo_delta user=Andrew',),
            scenarios=("evidence",),
        ),
        AdminCommandDefinition(
            command_id="report.manager",
            label="Generate today's manager report",
            help_text="Produce the main manager summary for the current day.",
            group_id="reports",
            read_only=True,
            handler_name="_command_report_manager",
            examples=("run report.manager",),
            scenarios=("reports", "attention"),
        ),
        AdminCommandDefinition(
            command_id="planning.tomorrow",
            label="What should each person work on tomorrow",
            help_text="Suggest likely next work for each intern based on ClickUp and the latest session context.",
            group_id="reports",
            read_only=True,
            handler_name="_command_planning_tomorrow",
            examples=("run planning.tomorrow",),
            scenarios=("reports",),
        ),
        AdminCommandDefinition(
            command_id="system.validate",
            label="Validate config files",
            help_text="Re-parse config and roster files, show token presence, and highlight duplicates.",
            group_id="system",
            read_only=True,
            handler_name="_command_system_validate",
            examples=("run system.validate",),
            scenarios=("system",),
        ),
        AdminCommandDefinition(
            command_id="system.dm_reachability",
            label="Check DM reachability",
            help_text="Verify whether the bot can open DM channels for all active roster users.",
            group_id="system",
            read_only=True,
            handler_name="_command_system_dm_reachability",
            examples=("run system.dm_reachability",),
            scenarios=("system",),
        ),
        AdminCommandDefinition(
            command_id="system.clickup_health",
            label="Check ClickUp health",
            help_text="Exercise the read path to ClickUp and show active-task resolution per user.",
            group_id="system",
            read_only=True,
            handler_name="_command_system_clickup_health",
            examples=("run system.clickup_health",),
            scenarios=("system",),
        ),
        AdminCommandDefinition(
            command_id="debug.resetworkday",
            label="Reset today's workday state",
            help_text="Reset local workflow state so the morning clock-in flow can be tested again. If `user=` is omitted, it resets every active roster user.",
            group_id="debug",
            read_only=False,
            advanced=True,
            confirm_required=True,
            args=(AdminCommandArgument("user", "Optional roster user key or display name", required=False, example="Andrew"),),
            handler_name="_command_debug_resetworkday",
            preview_name="_preview_debug_resetworkday",
            examples=("run debug.resetworkday", 'run debug.resetworkday user=Andrew'),
            scenarios=("debug", "system"),
        ),
        AdminCommandDefinition(
            command_id="debug.refreshroster",
            label="Refresh roster from disk",
            help_text="Force a config and roster reload from disk, then report which active users were added, removed, or changed.",
            group_id="debug",
            read_only=True,
            advanced=True,
            handler_name="_command_debug_refreshroster",
            examples=("run debug.refreshroster",),
            scenarios=("debug", "system"),
        ),
        AdminCommandDefinition(
            command_id="advanced.interpret",
            label="Interpret free-form text",
            help_text="Use AI to suggest the closest deterministic command or answer the request directly from current runtime data.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            requires_ai=True,
            args=(AdminCommandArgument("text", "The free-form admin request to interpret", example="how many people clocked in today"),),
            handler_name="_command_advanced_interpret",
            examples=('run advanced.interpret text="how many people clocked in today"',),
            scenarios=("advanced",),
        ),
        AdminCommandDefinition(
            command_id="advanced.weekly_completion",
            label="Which tasks can finish this week",
            help_text="Estimate which current tasks look realistically finishable this week.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_advanced_weekly_completion",
            examples=("run advanced.weekly_completion",),
            scenarios=("advanced", "reports"),
        ),
        AdminCommandDefinition(
            command_id="analysis.risks",
            label="Find blocked, stale, or missing-update work",
            help_text="Surface tasks that look blocked, stale, or under-documented based on today’s evidence.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_analysis_risks",
            examples=("run analysis.risks",),
            scenarios=("advanced", "blockers"),
        ),
        AdminCommandDefinition(
            command_id="advanced.bandwidth",
            label="Who has bandwidth for another task",
            help_text="Estimate which intern can reasonably absorb more work right now.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_advanced_bandwidth",
            examples=("run advanced.bandwidth",),
            scenarios=("advanced", "reports"),
        ),
        AdminCommandDefinition(
            command_id="advanced.recovery",
            label="Three-day recovery plan",
            help_text="Generate a short recovery plan for blocked or drifting work.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_advanced_recovery",
            examples=("run advanced.recovery",),
            scenarios=("advanced", "blockers"),
        ),
        AdminCommandDefinition(
            command_id="advanced.urgency_ranking",
            label="Rank tasks by urgency and impact",
            help_text="Rank current work by urgency, impact, and likely owner.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_advanced_urgency_ranking",
            examples=("run advanced.urgency_ranking",),
            scenarios=("advanced", "reports"),
        ),
        AdminCommandDefinition(
            command_id="advanced.unowned_tasks",
            label="Which tasks have no current owner",
            help_text="List mission-board tasks that are not actively owned by any intern right now.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_advanced_unowned_tasks",
            examples=("run advanced.unowned_tasks",),
            scenarios=("advanced", "reports"),
        ),
        AdminCommandDefinition(
            command_id="report.task_changes",
            label="What changed in ClickUp tasks today",
            help_text="Summarize which ClickUp tasks changed today based on intern updates.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_report_task_changes",
            examples=("run report.task_changes",),
            scenarios=("advanced", "reports"),
        ),
        AdminCommandDefinition(
            command_id="advanced.plan_vs_priority",
            label="Compare plans against ClickUp priorities",
            help_text="Compare today’s plans against the current ClickUp priority picture.",
            group_id="advanced",
            read_only=True,
            advanced=True,
            handler_name="_command_advanced_plan_vs_priority",
            examples=("run advanced.plan_vs_priority",),
            scenarios=("advanced", "reports"),
        ),
        AdminCommandDefinition(
            command_id="advanced.post_summary_comments",
            label="Post summary comment to each task",
            help_text="Write the latest intern summary back to each person’s active ClickUp task.",
            group_id="advanced",
            read_only=False,
            advanced=True,
            confirm_required=True,
            handler_name="_command_advanced_post_summary_comments",
            preview_name="_preview_advanced_post_summary_comments",
            examples=("run advanced.post_summary_comments",),
            scenarios=("advanced",),
        ),
    )

    commands_by_id = {command.command_id: command for command in commands}
    groups_by_id = {group.group_id: group for group in groups}
    scenarios = tuple(
        AdminScenarioDefinition(
            scenario_id=group.group_id,
            label=group.label,
            description=group.description,
            group_id=group.group_id,
            command_ids=tuple(command.command_id for command in commands if command.group_id == group.group_id),
        )
        for group in groups
    )
    scenarios_by_id = {scenario.scenario_id: scenario for scenario in scenarios}
    grouped_command_ids: dict[str, list[str]] = {group.group_id: [] for group in groups}
    for command in commands:
        grouped_command_ids.setdefault(command.group_id, []).append(command.command_id)
    populated_groups = tuple(
        AdminCommandGroupDefinition(
            group_id=group.group_id,
            label=group.label,
            description=group.description,
            command_ids=tuple(grouped_command_ids.get(group.group_id, [])),
            advanced=group.advanced,
        )
        for group in groups
    )
    groups_by_id = {group.group_id: group for group in populated_groups}
    return AdminConsoleRegistry(
        groups=populated_groups,
        commands=commands,
        scenarios=scenarios,
        groups_by_id=groups_by_id,
        commands_by_id=commands_by_id,
        scenarios_by_id=scenarios_by_id,
    )


def parse_admin_input(text: str, registry: AdminConsoleRegistry) -> ParsedAdminInput:
    raw = (text or "").strip()
    lowered = raw.lower()
    if lowered in {"help", "menu"}:
        return ParsedAdminInput(kind="help_root", raw=raw)
    if lowered == "flow":
        return ParsedAdminInput(kind="flow", raw=raw)
    if lowered == "back":
        return ParsedAdminInput(kind="back", raw=raw)
    if lowered == "home":
        return ParsedAdminInput(kind="home", raw=raw)
    if lowered == "cancel":
        return ParsedAdminInput(kind="cancel", raw=raw)
    if lowered.startswith("help "):
        group_id = lowered[5:].strip()
        if registry.group(group_id):
            return ParsedAdminInput(kind="help_group", raw=raw, group_id=group_id)
        return ParsedAdminInput(
            kind="invalid",
            raw=raw,
            error=f"I do not know the group `{group_id}`. Send `help` to see the top-level groups.",
        )
    if not lowered.startswith("run "):
        return ParsedAdminInput(
            kind="invalid",
            raw=raw,
            error=(
                "Use the admin console grammar: `help`, `menu`, `flow`, `help <group-id>`, "
                "or `run <command-id> key=value ...`."
            ),
        )
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        return ParsedAdminInput(kind="invalid", raw=raw, error=f"Could not parse that command: {exc}")
    if len(tokens) < 2:
        return ParsedAdminInput(
            kind="invalid",
            raw=raw,
            error="Use `run <command-id> key=value ...`.",
        )
    command_id = tokens[1]
    definition = registry.command(command_id)
    if not definition:
        return ParsedAdminInput(
            kind="invalid",
            raw=raw,
            error=f"I do not know the command `{command_id}`. Send `help` to browse the available commands.",
        )
    args: dict[str, str] = {}
    expected_names = {arg.name for arg in definition.args}
    for token in tokens[2:]:
        if "=" not in token:
            return ParsedAdminInput(
                kind="invalid",
                raw=raw,
                error=f"Argument `{token}` is missing `=`. Use `key=value` pairs after the command id.",
            )
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            return ParsedAdminInput(kind="invalid", raw=raw, error="I found an argument with no key before `=`.")
        if key in args:
            return ParsedAdminInput(
                kind="invalid",
                raw=raw,
                error=f"The argument `{key}` was supplied more than once.",
            )
        if expected_names and key not in expected_names:
            valid = ", ".join(arg.name for arg in definition.args)
            return ParsedAdminInput(
                kind="invalid",
                raw=raw,
                error=f"`{key}` is not valid for `{command_id}`. Expected arguments: {valid}.",
            )
        args[key] = value
    missing = [arg.name for arg in definition.args if arg.required and not args.get(arg.name)]
    if missing:
        usage = definition.examples[0] if definition.examples else f"run {command_id}"
        missing_text = ", ".join(missing)
        return ParsedAdminInput(
            kind="invalid",
            raw=raw,
            error=f"`{command_id}` is missing required arguments: {missing_text}. Example: `{usage}`",
        )
    return ParsedAdminInput(kind="run", raw=raw, command_id=command_id, args=args)


def render_root_help(registry: AdminConsoleRegistry) -> str:
    lines = [
        "Admin console",
        "",
        "Top-level groups:",
    ]
    for group in registry.groups:
        suffix = " [advanced]" if group.advanced else ""
        lines.append(f"- `{group.group_id}` - {group.label}{suffix}")
        lines.append(f"  {group.description}")
    lines.extend(
        [
            "",
            "Core grammar:",
            "- `help` or `menu`",
            "- `flow`",
            "- `help <group-id>`",
            "- `run <command-id> key=value ...`",
            "- `back`, `home`, `cancel`",
            "",
            "Examples:",
            "- `run presence.clocked_in`",
            '- `run task.switch user=Andrew task=\"formalize project tree\"`',
            "- `run system.validate`",
            '- `run advanced.interpret text=\"how many people clocked in today\"`',
        ]
    )
    return "\n".join(lines)


def render_group_help(registry: AdminConsoleRegistry, group_id: str) -> str:
    group = registry.group(group_id)
    if not group:
        return f"I do not know the group `{group_id}`."
    lines = [
        f"{group.label} (`{group.group_id}`)",
        "",
        group.description,
        "",
        "Commands:",
    ]
    for command in registry.group_commands(group.group_id):
        meta: list[str] = []
        meta.append("read-only" if command.read_only else "mutating")
        if command.advanced:
            meta.append("advanced")
        if command.requires_ai:
            meta.append("requires AI")
        lines.append(f"- `{command.command_id}` - {command.label}")
        lines.append(f"  {command.help_text}")
        lines.append(f"  {'; '.join(meta)}")
        if command.examples:
            lines.append(f"  Example: `{command.examples[0]}`")
    return "\n".join(lines)


def render_command_help(command: AdminCommandDefinition) -> str:
    lines = [
        f"{command.label} (`{command.command_id}`)",
        "",
        command.help_text,
    ]
    metadata: list[str] = []
    metadata.append("read-only" if command.read_only else "mutating")
    if command.advanced:
        metadata.append("advanced")
    if command.requires_ai:
        metadata.append("requires AI")
    lines.extend(["", f"Mode: {', '.join(metadata)}"])
    if command.args:
        lines.append("")
        lines.append("Arguments:")
        for arg in command.args:
            requirement = "required" if arg.required else "optional"
            example = f" Example: `{arg.example}`." if arg.example else ""
            lines.append(f"- `{arg.name}` ({requirement}) - {arg.description}.{example}")
    if command.examples:
        lines.append("")
        lines.append("Examples:")
        lines.extend(f"- `{example}`" for example in command.examples)
    return "\n".join(lines)


def render_flow(registry: AdminConsoleRegistry) -> str:
    return "\n".join(
        [
            "```mermaid",
            build_mermaid_flowchart(registry),
            "```",
        ]
    )


def build_mermaid_flowchart(registry: AdminConsoleRegistry) -> str:
    lines = ["flowchart TD", '  start["What situation are you in?"]']
    for scenario in registry.scenarios:
        scenario_node = _mermaid_node_id(f"scenario_{scenario.scenario_id}")
        lines.append(f'  start --> {scenario_node}["{_escape_mermaid_text(scenario.label)}"]')
        for command_id in scenario.command_ids:
            command = registry.command(command_id)
            if not command:
                continue
            command_node = _mermaid_node_id(command.command_id)
            lines.append(
                f'  {scenario_node} --> {command_node}["{_escape_mermaid_text(command.command_id)}"]'
            )
    return "\n".join(lines)


def render_reference_markdown(registry: AdminConsoleRegistry) -> str:
    lines = [
        "# Admin Console Reference",
        "",
        "This console is deterministic by default. Use grouped help, browse the situation menu, or run commands with stable command IDs.",
        "",
        "## Grammar",
        "",
        "- `help`",
        "- `menu`",
        "- `flow`",
        "- `help <group-id>`",
        "- `run <command-id> key=value ...`",
        "- `back`",
        "- `home`",
        "- `cancel`",
        "",
        "## Situation Groups",
        "",
    ]
    for group in registry.groups:
        suffix = " (advanced)" if group.advanced else ""
        lines.extend([f"### `{group.group_id}` - {group.label}{suffix}", "", group.description, ""])
        for command in registry.group_commands(group.group_id):
            meta = ["read-only" if command.read_only else "mutating"]
            if command.requires_ai:
                meta.append("requires AI")
            if command.advanced:
                meta.append("advanced")
            lines.append(f"- `{command.command_id}` - {command.label} ({', '.join(meta)})")
            lines.append(f"  - {command.help_text}")
            if command.examples:
                lines.append(f"  - Example: `{command.examples[0]}`")
        lines.append("")
    lines.extend(
        [
            "## Scenario Flow",
            "",
            "```mermaid",
            build_mermaid_flowchart(registry),
            "```",
            "",
            "## Common Paths",
            "",
            "- Who needs attention: `help attention` or `run presence.attention`",
            "- Someone is stuck: `help blockers`, then `run blockers.intervention` or `run blockers.dm_stuck`",
            '- Change someone\'s task: `help task`, then `run task.switch user=Andrew task="formalize project tree"`',
            "- Review finished work: `help review`, then `run review.pending_tasks` followed by `run review.close ...` or `run review.rework ...`",
            "- Review evidence: `help evidence`, then `run evidence.before_after` or `run evidence.photo_delta user=Andrew`",
            "- Generate planning/report output: `help reports` or `help advanced`",
            "- Verify the system: `help system`, then `run system.validate` or `run system.clickup_health`",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _escape_mermaid_text(text: str) -> str:
    return text.replace('"', "'")


def _mermaid_node_id(text: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in text)
