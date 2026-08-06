from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from openai import AsyncOpenAI

from .admin_console import (
    AdminActionPreview,
    AdminActionRequest,
    AdminCommandDefinition,
    AdminConsoleRegistry,
    AdminMenuState,
    build_admin_console_registry,
    parse_admin_input,
    render_command_help,
    render_flow,
    render_group_help,
    render_root_help,
)
from .clickup_client import ClickUpClient
from .models import MessageRecord, SessionState, UserProfile
from .openai_models import ModelFallbackChain
from .time_utils import ADMIN_DISPLAY_TIMEZONE, format_admin_datetime, localize_datetime, resolve_timezone

if TYPE_CHECKING:
    from .runtime import InternManagementRuntime


_MAX_DISCORD_MESSAGE = 1900


@dataclass(slots=True)
class UserDailySnapshot:
    user: UserProfile
    session: SessionState
    messages: list[MessageRecord]
    before_start_photos: list[Path]
    progress_photos: list[Path]
    after_photos: list[Path]
    last_inbound: MessageRecord | None
    last_outbound: MessageRecord | None

    @property
    def responded(self) -> bool:
        return bool(self.session.first_sign_of_life_at or self.last_inbound)

    @property
    def clocked_in(self) -> bool:
        return bool(self.session.clocked_in_at)

    @property
    def clocked_out(self) -> bool:
        return bool(self.session.clocked_out_at)

    @property
    def active_now(self) -> bool:
        return self.clocked_in and not self.clocked_out and self.session.stage in {
            "awaiting_task_selection",
            "awaiting_plan",
            "awaiting_start_photo",
            "awaiting_risk",
            "active",
            "awaiting_clock_out_artifacts",
        }

    @property
    def on_lunch_break(self) -> bool:
        return self.session.stage == "on_lunch_break" and self.clocked_in and not self.clocked_out

    @property
    def stuck_now(self) -> bool:
        return bool(self.session.stuck_since) and not self.clocked_out

    @property
    def last_user_message_at(self) -> str | None:
        return self.session.last_user_message_at

    @property
    def missing_before_start_photo(self) -> bool:
        return self.clocked_in and not self.before_start_photos

    @property
    def missing_end_of_day_report(self) -> bool:
        return self.session.stage == "awaiting_clock_out_artifacts"

    @property
    def after_photo_without_summary(self) -> bool:
        return bool(self.after_photos) and bool(self.session.awaiting_clock_out_summary)

    @property
    def active_clickup_task_name(self) -> str | None:
        value = self.session.metadata.get("active_clickup_task_name")
        return str(value) if isinstance(value, str) and value else None

    @property
    def active_clickup_task_id(self) -> str | None:
        value = self.session.metadata.get("active_clickup_task_id")
        return str(value) if isinstance(value, str) and value else None

    @property
    def blocker_state(self) -> str | None:
        value = self.session.metadata.get("blocker_state")
        return str(value) if isinstance(value, str) and value else None

    @property
    def pending_admin_review(self) -> dict[str, Any] | None:
        reviews = self.pending_admin_reviews
        return reviews[0] if reviews else None

    @property
    def pending_admin_reviews(self) -> list[dict[str, Any]]:
        value = self.session.metadata.get("pending_admin_reviews")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        legacy = self.session.metadata.get("pending_admin_review")
        if isinstance(legacy, dict):
            return [legacy]
        return []

    @property
    def pending_admin_unblocker_task(self) -> dict[str, Any] | None:
        value = self.session.metadata.get("pending_admin_unblocker_task")
        return value if isinstance(value, dict) else None

    @property
    def pending_admin_task_proposal(self) -> dict[str, Any] | None:
        value = self.session.metadata.get("pending_admin_task_proposal")
        return value if isinstance(value, dict) else None


class AdminAnalyst:
    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        self.models = ModelFallbackChain(
            "admin analyst",
            os.environ.get("OPENAI_MODEL") or "gpt-5-mini",
            os.environ.get("BACKUP_OPENAI_MODEL"),
            "gpt-4.1-mini",
        )
        self.model = self.models.active_model or "gpt-4.1-mini"
        self.client = AsyncOpenAI(api_key=api_key) if api_key else None
        self.enabled = self.client is not None

    async def _create_response(self, *, input: Any):
        if not self.client:
            raise RuntimeError("OpenAI client is not configured.")
        last_exc: Exception | None = None
        for model in self.models.candidate_models():
            try:
                response = await self.client.responses.create(model=model, input=input)
            except Exception as exc:
                last_exc = exc
                continue
            self.models.record_success(model)
            self.model = model
            return response
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No admin analyst model is configured.")

    async def answer(self, question: str, context: str, fallback: str) -> str:
        if not self.enabled or not self.client:
            return fallback
        prompt = (
            "You are an operations assistant for a small intern management system.\n"
            "Answer the admin's question using only the provided data. Be concrete, concise, "
            "and honest about uncertainty. If a recommendation depends on incomplete data, say so.\n\n"
            f"Admin question:\n{question}\n\n"
            f"Available data:\n{context}"
        )
        try:
            response = await self._create_response(input=prompt)
            text = response.output_text.strip()
            return text or fallback
        except Exception:
            self.enabled = False
            return fallback

    async def compare_images(self, question: str, before_path: Path, after_path: Path, fallback: str) -> str:
        if not self.enabled or not self.client:
            return fallback
        try:
            response = await self._create_response(
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Compare these two progress photos from the same project.\n"
                                    "Describe the visible differences, what appears more complete, "
                                    "and whether progress seems meaningful. Keep it short.\n\n"
                                    f"Admin question: {question}"
                                ),
                            },
                            {"type": "input_image", "image_url": _as_data_url(before_path)},
                            {"type": "input_image", "image_url": _as_data_url(after_path)},
                        ],
                    }
                ],
            )
            text = response.output_text.strip()
            return text or fallback
        except Exception:
            self.enabled = False
            return fallback


class _AdminMenuView(discord.ui.View):
    def __init__(self, router: AdminCommandRouter, state: AdminMenuState) -> None:
        timeout_seconds = router._menu_timeout_minutes() * 60
        super().__init__(timeout=timeout_seconds)
        self.router = router
        self.state = state
        if state.screen == "root":
            for group in router.registry.groups:
                self.add_item(_AdminGroupButton(router, state, group.group_id, group.label))
        elif state.screen == "group" and state.group_id:
            self.add_item(_AdminCommandSelect(router, state, state.group_id))
        elif state.screen == "command" and state.command_id:
            command = router.registry.command(state.command_id)
            if command and not _required_arg_names(command):
                label = "Run" if command.read_only else "Preview"
                self.add_item(_AdminRunButton(router, state, command.command_id, label))
        if state.screen != "root":
            self.add_item(_AdminNavButton(router, state, "back", "Back", discord.ButtonStyle.secondary))
        self.add_item(_AdminNavButton(router, state, "home", "Home", discord.ButtonStyle.secondary))
        self.add_item(_AdminNavButton(router, state, "cancel", "Cancel", discord.ButtonStyle.danger))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.admin_user_id:
            await _safe_interaction_message(
                interaction,
                "This menu belongs to another admin session. Send `help` to open your own menu.",
                replace=False,
            )
            return False
        if not self.router._is_menu_state_valid(self.state.admin_user_id, self.state.token):
            await _safe_interaction_message(
                interaction,
                "That menu expired. Send `help` to open a fresh admin console menu.",
                replace=False,
            )
            return False
        return True


class _AdminGroupButton(discord.ui.Button[_AdminMenuView]):
    def __init__(self, router: AdminCommandRouter, state: AdminMenuState, group_id: str, label: str) -> None:
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"admin-menu:{state.token}:group:{group_id}",
        )
        self.router = router
        self.state = state
        self.group_id = group_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.router._open_group_from_interaction(interaction, self.state, self.group_id)


class _AdminCommandSelect(discord.ui.Select[_AdminMenuView]):
    def __init__(self, router: AdminCommandRouter, state: AdminMenuState, group_id: str) -> None:
        self.router = router
        self.state = state
        self.group_id = group_id
        options = []
        for command in router.registry.group_commands(group_id)[:25]:
            description = command.help_text[:100]
            options.append(
                discord.SelectOption(
                    label=command.label[:100],
                    description=description,
                    value=command.command_id,
                )
            )
        super().__init__(
            placeholder="Choose a command to inspect",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"admin-menu:{state.token}:command-select:{group_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.router._open_command_from_interaction(interaction, self.state, self.values[0])


class _AdminRunButton(discord.ui.Button[_AdminMenuView]):
    def __init__(self, router: AdminCommandRouter, state: AdminMenuState, command_id: str, label: str) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.success,
            custom_id=f"admin-menu:{state.token}:run:{command_id}",
        )
        self.router = router
        self.state = state
        self.command_id = command_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.router._run_command_from_interaction(interaction, self.command_id)


class _AdminNavButton(discord.ui.Button[_AdminMenuView]):
    def __init__(
        self,
        router: AdminCommandRouter,
        state: AdminMenuState,
        action: str,
        label: str,
        style: discord.ButtonStyle,
    ) -> None:
        super().__init__(
            label=label,
            style=style,
            custom_id=f"admin-menu:{state.token}:nav:{action}",
        )
        self.router = router
        self.state = state
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.router._handle_navigation_interaction(interaction, self.state, self.action)


class _AdminConfirmView(discord.ui.View):
    def __init__(self, router: AdminCommandRouter, request: AdminActionRequest) -> None:
        timeout_seconds = router._menu_timeout_minutes() * 60
        super().__init__(timeout=timeout_seconds)
        self.router = router
        self.request = request
        self.add_item(_AdminConfirmButton(router, request, True))
        self.add_item(_AdminConfirmButton(router, request, False))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.request.admin_user_id:
            await _safe_interaction_message(
                interaction,
                "This confirmation belongs to another admin session. Send `help` to continue in your own console.",
                replace=False,
            )
            return False
        if not self.router._is_pending_action_valid(self.request.admin_user_id, self.request.token):
            await _safe_interaction_message(
                interaction,
                "That confirmation expired. Run the command again if you still want to do it.",
                replace=False,
            )
            return False
        return True


class _AdminConfirmButton(discord.ui.Button[_AdminConfirmView]):
    def __init__(self, router: AdminCommandRouter, request: AdminActionRequest, confirm: bool) -> None:
        label = request.preview.confirm_label if confirm else request.preview.cancel_label
        style = discord.ButtonStyle.success if confirm else discord.ButtonStyle.danger
        action = "confirm" if confirm else "cancel"
        super().__init__(
            label=label,
            style=style,
            custom_id=f"admin-confirm:{request.token}:{action}",
        )
        self.router = router
        self.request = request
        self.confirm = confirm

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.confirm:
            await self.router._confirm_action_from_interaction(interaction, self.request)
            return
        await self.router._cancel_action_from_interaction(interaction, self.request)


class AdminCommandRouter:
    def __init__(self, runtime: InternManagementRuntime) -> None:
        self.runtime = runtime
        self.analyst = AdminAnalyst()
        self.registry: AdminConsoleRegistry = build_admin_console_registry()
        self._active_admin_user_id: int | None = None
        self._menu_states: dict[int, AdminMenuState] = {}
        self._pending_actions: dict[int, AdminActionRequest] = {}

    async def handle_message(self, client: discord.Client, message: discord.Message) -> bool:
        config = self.runtime.config
        is_admin = False
        if config:
            if hasattr(self.runtime, "is_admin_user"):
                is_admin = bool(self.runtime.is_admin_user(message.author.id))
            else:
                is_admin = message.author.id == config.admin_discord_user_id
        if not config or not is_admin:
            return False

        self._active_admin_user_id = message.author.id
        try:
            parsed = parse_admin_input(message.content or "", self.registry)
            if parsed.kind == "help_root":
                await self._send_root_menu(client, message.author.id)
                return True
            if parsed.kind == "help_group" and parsed.group_id:
                await self._send_group_menu(client, message.author.id, parsed.group_id)
                return True
            if parsed.kind == "flow":
                await self._send_admin_text(
                    client,
                    "Admin console flow:\n\n"
                    + render_flow(self.registry)
                    + "\n\nFor the full reference, open `docs/admin_console.md` in the workspace.",
                )
                return True
            if parsed.kind == "home":
                await self._clear_pending_action(message.author.id)
                await self._send_root_menu(client, message.author.id)
                return True
            if parsed.kind == "back":
                await self._navigate_back(client, message.author.id)
                return True
            if parsed.kind == "cancel":
                await self._clear_pending_action(message.author.id)
                self._menu_states.pop(message.author.id, None)
                await self._send_admin_text(
                    client,
                    "Cancelled the current admin action. Send `help` to reopen the admin console.",
                )
                return True
            if parsed.kind == "invalid":
                if await self._maybe_handle_ai_fallback(client, message.author.id, parsed.raw):
                    return True
                await self._send_admin_text(client, (parsed.error or "I could not parse that command.") + "\n\n" + self._grammar_hint())
                return True
            if parsed.kind == "run" and parsed.command_id:
                await self._handle_run_command(client, message.author.id, parsed.command_id, parsed.args)
                return True
            await self._send_admin_text(client, self._grammar_hint())
            return True
        finally:
            self._active_admin_user_id = None

    async def _send_root_menu(self, client: discord.Client, admin_user_id: int) -> None:
        state = self._new_menu_state(admin_user_id, "root")
        await self._send_menu_state(client, state)

    async def _send_group_menu(self, client: discord.Client, admin_user_id: int, group_id: str) -> None:
        state = self._new_menu_state(
            admin_user_id,
            "group",
            group_id=group_id,
            history=[("root", None)],
        )
        await self._send_menu_state(client, state)

    async def _send_command_menu(self, client: discord.Client, admin_user_id: int, command_id: str, history: list[tuple[str, str | None]]) -> None:
        group_id = self.registry.command(command_id).group_id if self.registry.command(command_id) else None
        state = self._new_menu_state(
            admin_user_id,
            "command",
            group_id=group_id,
            command_id=command_id,
            history=history,
        )
        await self._send_menu_state(client, state)

    async def _send_menu_state(self, client: discord.Client, state: AdminMenuState) -> None:
        self._menu_states[state.admin_user_id] = state
        content = self._render_menu_state(state)
        view = _AdminMenuView(self, state)
        await self._send_admin_text(client, content, view=view)

    async def _open_group_from_interaction(
        self,
        interaction: discord.Interaction,
        current_state: AdminMenuState,
        group_id: str,
    ) -> None:
        history = current_state.history + [(current_state.screen, current_state.group_id)]
        state = self._new_menu_state(
            interaction.user.id,
            "group",
            group_id=group_id,
            history=history,
        )
        await self._replace_menu_interaction(interaction, state)

    async def _open_command_from_interaction(
        self,
        interaction: discord.Interaction,
        current_state: AdminMenuState,
        command_id: str,
    ) -> None:
        history = current_state.history + [(current_state.screen, current_state.group_id)]
        group_id = self.registry.command(command_id).group_id if self.registry.command(command_id) else current_state.group_id
        state = self._new_menu_state(
            interaction.user.id,
            "command",
            group_id=group_id,
            command_id=command_id,
            history=history,
        )
        await self._replace_menu_interaction(interaction, state)

    async def _handle_navigation_interaction(
        self,
        interaction: discord.Interaction,
        current_state: AdminMenuState,
        action: str,
    ) -> None:
        if action == "home":
            await self._clear_pending_action(interaction.user.id)
            state = self._new_menu_state(interaction.user.id, "root")
            await self._replace_menu_interaction(interaction, state)
            return
        if action == "cancel":
            await self._clear_pending_action(interaction.user.id)
            self._menu_states.pop(interaction.user.id, None)
            await _safe_interaction_message(
                interaction,
                "Cancelled the current admin action. Send `help` to reopen the menu.",
                replace=True,
            )
            return
        previous = self._previous_state_from_history(interaction.user.id, current_state)
        if previous is None:
            state = self._new_menu_state(interaction.user.id, "root")
        else:
            state = previous
        await self._replace_menu_interaction(interaction, state)

    async def _run_command_from_interaction(self, interaction: discord.Interaction, command_id: str) -> None:
        await self._handle_run_command(
            None,
            interaction.user.id,
            command_id,
            {},
            interaction=interaction,
        )

    async def _confirm_action_from_interaction(
        self,
        interaction: discord.Interaction,
        request: AdminActionRequest,
    ) -> None:
        stored = self._pending_actions.get(interaction.user.id)
        if not stored or stored.token != request.token:
            await _safe_interaction_message(
                interaction,
                "That confirmation is no longer active. Run the command again if you still want it.",
                replace=False,
            )
            return
        await interaction.response.edit_message(content="Running command...", view=None)
        await self._clear_pending_action(interaction.user.id)
        self._active_admin_user_id = interaction.user.id
        try:
            result = await self._execute_command(interaction.client, stored.command_id, stored.args)
        finally:
            self._active_admin_user_id = None
        await interaction.followup.send(result[:_MAX_DISCORD_MESSAGE])

    async def _cancel_action_from_interaction(
        self,
        interaction: discord.Interaction,
        request: AdminActionRequest,
    ) -> None:
        await self._clear_pending_action(interaction.user.id)
        await interaction.response.edit_message(
            content=f"Cancelled `{request.command_id}`.",
            view=None,
        )

    async def _navigate_back(self, client: discord.Client, admin_user_id: int) -> None:
        current_state = self._menu_states.get(admin_user_id)
        previous = self._previous_state_from_history(admin_user_id, current_state)
        if previous is None:
            await self._send_root_menu(client, admin_user_id)
            return
        await self._send_menu_state(client, previous)

    async def _handle_run_command(
        self,
        client: discord.Client | None,
        admin_user_id: int,
        command_id: str,
        args: dict[str, str],
        *,
        interaction: discord.Interaction | None = None,
    ) -> None:
        command = self.registry.command(command_id)
        if not command:
            target = interaction.client if interaction else client
            if target:
                await self._send_or_reply(
                    target,
                    admin_user_id,
                    f"I do not know the command `{command_id}`.",
                    interaction=interaction,
                )
            return
        if command.confirm_required and not command.read_only:
            preview = await self._build_preview(command, args)
            request = AdminActionRequest(
                token=self._new_token(),
                admin_user_id=admin_user_id,
                command_id=command_id,
                args=args,
                preview=preview,
            )
            self._pending_actions[admin_user_id] = request
            content = self._format_preview(preview)
            view = _AdminConfirmView(self, request)
            if interaction:
                await _safe_interaction_message(interaction, content, view=view, replace=False)
                return
            if client:
                await self._send_admin_text(client, content, view=view)
            return
        self._active_admin_user_id = admin_user_id
        try:
            result = await self._execute_command(interaction.client if interaction else client, command_id, args)
        finally:
            self._active_admin_user_id = None
        target = interaction.client if interaction else client
        if target:
            await self._send_or_reply(target, admin_user_id, result, interaction=interaction)

    async def _execute_command(
        self,
        client: discord.Client | None,
        command_id: str,
        args: dict[str, str],
    ) -> str:
        command = self.registry.command(command_id)
        if not command:
            return f"I do not know the command `{command_id}`."
        snapshots = await self._collect_snapshots()
        handler = getattr(self, command.handler_name)
        return await handler(client, snapshots, args)

    async def _build_preview(
        self,
        command: AdminCommandDefinition,
        args: dict[str, str],
    ) -> AdminActionPreview:
        snapshots = await self._collect_snapshots()
        if command.preview_name:
            preview_builder = getattr(self, command.preview_name)
            preview = await preview_builder(snapshots, args)
            if preview:
                return preview
        title = f"Preview `{command.command_id}`"
        lines = [command.help_text]
        if args:
            lines.append("")
            lines.append("Arguments:")
            for key, value in args.items():
                lines.append(f"- `{key}` = `{value}`")
        lines.append("")
        lines.append("This command changes state or sends messages and will not run until you confirm it.")
        return AdminActionPreview(
            title=title,
            summary="\n".join(lines),
            command_id=command.command_id,
            args=dict(args),
        )

    async def _maybe_handle_ai_fallback(
        self,
        client: discord.Client,
        admin_user_id: int,
        text: str,
    ) -> bool:
        config = getattr(self.runtime, "config", None)
        if not config or not getattr(config, "admin_console", None):
            return False
        if not config.admin_console.enable_ai_fallback:
            return False
        response = await self._admin_ai_fallback_response(text)
        if not response:
            return False
        self._active_admin_user_id = admin_user_id
        try:
            await self._send_admin_text(client, response)
        finally:
            self._active_admin_user_id = None
        return True

    def _menu_timeout_minutes(self) -> int:
        config = getattr(self.runtime, "config", None)
        if not config or not getattr(config, "admin_console", None):
            return 10
        return max(1, int(config.admin_console.menu_timeout_minutes))

    def _new_menu_state(
        self,
        admin_user_id: int,
        screen: str,
        *,
        group_id: str | None = None,
        command_id: str | None = None,
        history: list[tuple[str, str | None]] | None = None,
    ) -> AdminMenuState:
        return AdminMenuState(
            token=self._new_token(),
            admin_user_id=admin_user_id,
            screen=screen,
            history=list(history or []),
            group_id=group_id,
            command_id=command_id,
        )

    def _previous_state_from_history(
        self,
        admin_user_id: int,
        current_state: AdminMenuState | None,
    ) -> AdminMenuState | None:
        if not current_state or not current_state.history:
            return None
        history = list(current_state.history)
        screen, payload = history.pop()
        if screen == "root":
            return self._new_menu_state(admin_user_id, "root", history=history)
        if screen == "group":
            return self._new_menu_state(admin_user_id, "group", group_id=payload, history=history)
        if screen == "command":
            return self._new_menu_state(admin_user_id, "command", command_id=payload, history=history)
        return None

    def _render_menu_state(self, state: AdminMenuState) -> str:
        if state.screen == "root":
            return render_root_help(self.registry)
        if state.screen == "group" and state.group_id:
            return render_group_help(self.registry, state.group_id)
        if state.screen == "command" and state.command_id:
            command = self.registry.command(state.command_id)
            if command:
                return render_command_help(command)
        return render_root_help(self.registry)

    async def _replace_menu_interaction(self, interaction: discord.Interaction, state: AdminMenuState) -> None:
        self._menu_states[state.admin_user_id] = state
        await _safe_interaction_message(
            interaction,
            self._render_menu_state(state),
            view=_AdminMenuView(self, state),
            replace=True,
        )

    async def _clear_pending_action(self, admin_user_id: int) -> None:
        self._pending_actions.pop(admin_user_id, None)

    def _is_menu_state_valid(self, admin_user_id: int, token: str) -> bool:
        state = self._menu_states.get(admin_user_id)
        return bool(state and state.token == token and not state.is_expired(self._menu_timeout_minutes()))

    def _is_pending_action_valid(self, admin_user_id: int, token: str) -> bool:
        request = self._pending_actions.get(admin_user_id)
        return bool(request and request.token == token and not request.is_expired(self._menu_timeout_minutes()))

    def _new_token(self) -> str:
        return secrets.token_hex(6)

    def _grammar_hint(self) -> str:
        return (
            "Use `help`, `menu`, `flow`, `help <group-id>`, or `run <command-id> key=value ...`.\n"
            "Example: `run task.switch user=Andrew task=\"formalize project tree\"`"
        )

    def _format_preview(self, preview: AdminActionPreview) -> str:
        return f"{preview.title}\n\n{preview.summary}"

    async def _send_or_reply(
        self,
        client: discord.Client,
        admin_user_id: int,
        content: str,
        *,
        interaction: discord.Interaction | None = None,
    ) -> None:
        if interaction:
            await _safe_interaction_message(interaction, content, replace=False)
            return
        self._active_admin_user_id = admin_user_id
        try:
            await self._send_admin_text(client, content)
        finally:
            self._active_admin_user_id = None

    async def _collect_snapshots(self) -> list[UserDailySnapshot]:
        config = self.runtime.config
        if not config:
            return []
        base_now = datetime.now(tz=resolve_timezone(config.timezone))
        snapshots: list[UserDailySnapshot] = []
        for user in self.runtime.roster_by_key.values():
            session, _local_now = self.runtime.get_user_session_for_moment(user, base_now)
            messages = self.runtime.list_session_messages(user.user_key, session)
            last_inbound = next((message for message in reversed(messages) if message.direction == "inbound"), None)
            last_outbound = next((message for message in reversed(messages) if message.direction == "outbound"), None)
            before_start = self._resolve_paths(session.metadata.get("before_start_photo_paths"))
            progress = self._resolve_paths(session.metadata.get("progress_photo_paths"))
            after = self._resolve_paths(session.metadata.get("clock_out_photo_paths"))
            if not before_start or not after:
                fallback_before, fallback_after, fallback_progress = self._infer_photo_roles(messages)
                before_start = before_start or fallback_before
                after = after or fallback_after
                progress = progress or fallback_progress
            snapshots.append(
                UserDailySnapshot(
                    user=user,
                    session=session,
                    messages=messages,
                    before_start_photos=before_start,
                    progress_photos=progress,
                    after_photos=after,
                    last_inbound=last_inbound,
                    last_outbound=last_outbound,
                )
            )
        return snapshots

    async def _send_admin_text(
        self,
        client: discord.Client,
        content: str,
        *,
        view: discord.ui.View | None = None,
    ) -> None:
        if not self.runtime.config:
            return
        target_user_id = self._active_admin_user_id or self.runtime.config.admin_discord_user_id
        admin_user = await client.fetch_user(target_user_id)
        dm = await admin_user.create_dm()
        chunks = _split_message(content)
        if view is None:
            for chunk in chunks:
                await dm.send(chunk)
            return
        await dm.send(chunks[0][: _MAX_DISCORD_MESSAGE], view=view)
        for chunk in chunks[1:]:
            await dm.send(chunk)

    async def _send_admin_files(self, client: discord.Client, content: str, files: list[Path]) -> None:
        if not self.runtime.config:
            return
        target_user_id = self._active_admin_user_id or self.runtime.config.admin_discord_user_id
        admin_user = await client.fetch_user(target_user_id)
        dm = await admin_user.create_dm()
        discord_files = [discord.File(str(path)) for path in files if path.exists()]
        if not discord_files:
            await dm.send(content[:_MAX_DISCORD_MESSAGE])
            return
        await dm.send(content=content[:_MAX_DISCORD_MESSAGE], files=discord_files[:10])

    def _format_named_list(self, title: str, items: list[str]) -> str:
        lines = [f"{title}:"]
        if not items:
            lines.append("- none")
        else:
            lines.extend(f"- {item}" for item in items)
        return "\n".join(lines)

    async def _command_presence_clocked_in(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._format_named_list(
            "Clocked in today",
            [snapshot.user.display_name for snapshot in snapshots if snapshot.clocked_in],
        )

    async def _command_presence_no_response(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._format_named_list(
            "No response yet",
            [snapshot.user.display_name for snapshot in snapshots if not snapshot.responded],
        )

    async def _command_presence_remind_clock_in(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        return await self._dm_missing_clock_in(client, snapshots)

    async def _command_presence_last_messages(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._last_message_report(snapshots)

    async def _command_presence_active_count(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        active = [snapshot.user.display_name for snapshot in snapshots if snapshot.active_now]
        return f"{len(active)} active right now: {', '.join(active) or 'none'}"

    async def _command_presence_clocked_out_count(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        clocked_out = [snapshot.user.display_name for snapshot in snapshots if snapshot.clocked_out]
        return f"{len(clocked_out)} have clocked out today: {', '.join(clocked_out) or 'none'}"

    async def _command_presence_active_roster(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._format_named_list("Active roster users", [snapshot.user.display_name for snapshot in snapshots])

    async def _command_presence_attention(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        lines = ["Who needs attention now:"]
        no_response = [snapshot.user.display_name for snapshot in snapshots if not snapshot.responded]
        stuck = [snapshot.user.display_name for snapshot in snapshots if snapshot.stuck_now]
        on_lunch = [snapshot.user.display_name for snapshot in snapshots if snapshot.on_lunch_break]
        missing_before = [snapshot.user.display_name for snapshot in snapshots if snapshot.missing_before_start_photo]
        missing_end = [snapshot.user.display_name for snapshot in snapshots if snapshot.missing_end_of_day_report]
        if no_response:
            lines.append("- No response yet: " + ", ".join(no_response))
        if stuck:
            lines.append("- Currently stuck: " + ", ".join(stuck))
        if on_lunch:
            lines.append("- On lunch break: " + ", ".join(on_lunch))
        if missing_before:
            lines.append("- Missing before-start photo: " + ", ".join(missing_before))
        if missing_end:
            lines.append("- Incomplete end-of-day handoff: " + ", ".join(missing_end))
        if len(lines) == 1:
            lines.append("- nobody is currently flagged")
        return "\n".join(lines)

    async def _command_blockers_stuck(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._stuck_report(snapshots)

    async def _command_blockers_intervention(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._admin_intervention_report(snapshots)

    async def _command_blockers_dm_stuck(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        return await self._dm_stuck_users(client, snapshots)

    async def _command_blockers_missing_before_photo(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._format_named_list(
            "Missing before-start photo",
            [snapshot.user.display_name for snapshot in snapshots if snapshot.missing_before_start_photo],
        )

    async def _command_blockers_missing_end_of_day(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._format_named_list(
            "End-of-day report still incomplete",
            [snapshot.user.display_name for snapshot in snapshots if snapshot.missing_end_of_day_report],
        )

    async def _command_blockers_remind_missing_photos(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        return await self._remind_missing_photos(client, snapshots)

    async def _command_task_status(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        return await self.runtime.describe_user_task_state(snapshot.user, snapshot.session)

    async def _command_task_switch(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        task_hint = (args.get("task") or "").strip()
        if not task_hint:
            return "I still need the target task name. Example: `run task.switch user=Andrew task=\"formalize project tree\"`"
        return await self.runtime.initiate_admin_task_switch(client, snapshot.user, snapshot.session, task_hint)

    async def _command_task_resume(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        return await self.runtime.resume_user_after_unblock(
            client,
            snapshot.user,
            snapshot.session,
            source="admin",
            actor_note=f"Admin console resume command for {snapshot.user.display_name}.",
            notify_user=True,
        )

    async def _command_task_prioritize(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client or not self.runtime.config or not self.runtime.clickup:
            return "ClickUp is not configured."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        tasks = await self.runtime.clickup.list_assigned_tasks(snapshot.user)
        highest = _pick_highest_priority_task(tasks) or (tasks[0] if tasks else None)
        if not highest:
            return f"I could not find any assigned ClickUp tasks for {snapshot.user.display_name}."
        now = self.runtime.resolve_user_local_now(snapshot.user)
        await self.runtime._send_dm(
            client,
            snapshot.user,
            snapshot.session,
            f"Priority change from admin: focus first on `{highest.get('name')}`. "
            "Treat it as the top task before branching back to your earlier plan unless you hit a blocker.",
            now,
        )
        return f"Told {snapshot.user.display_name} to prioritize `{highest.get('name')}`."

    async def _command_review_pending_tasks(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        pending_lines: list[str] = []
        for snapshot in snapshots:
            for review in snapshot.pending_admin_reviews:
                task_name = str(review.get("task_name") or snapshot.active_clickup_task_name or "current task")
                task_id = str(review.get("task_id") or "").strip()
                line = f"- {snapshot.user.display_name}: {task_name}"
                if task_id:
                    line += f" | id={task_id}"
                pending_lines.append(line)
        if not pending_lines:
            return "No intern tasks are waiting on admin review right now."
        return "\n".join(["Pending task reviews:", *pending_lines])

    async def _command_review_close(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        admin_message = (args.get("comments") or "Approved from the admin console.").strip()
        return await self.runtime.resolve_admin_review(
            client,
            snapshot.user,
            snapshot.session,
            approve_close=True,
            admin_message=admin_message,
            task_hint=(args.get("task") or "").strip() or None,
            task_id=(args.get("task_id") or "").strip() or None,
        )

    async def _command_review_rework(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        comments = (args.get("comments") or "").strip()
        if not comments:
            return "I need `comments=` for `review.rework`."
        return await self.runtime.resolve_admin_review(
            client,
            snapshot.user,
            snapshot.session,
            approve_close=False,
            admin_message=comments,
            task_hint=(args.get("task") or "").strip() or None,
            task_id=(args.get("task_id") or "").strip() or None,
        )

    async def _command_review_pending_unblockers(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        pending = [snapshot for snapshot in snapshots if snapshot.pending_admin_unblocker_task]
        if not pending:
            return "No unblocker-task drafts are waiting on admin review right now."
        lines = ["Pending unblocker-task drafts:"]
        for snapshot in pending:
            proposal = snapshot.pending_admin_unblocker_task or {}
            draft = proposal.get("draft") if isinstance(proposal, dict) else {}
            title = ""
            if isinstance(draft, dict):
                title = str(draft.get("title") or "").strip()
            lines.append(f"- {snapshot.user.display_name}: {title or 'untitled draft'}")
        return "\n".join(lines)

    async def _command_review_pending_task_proposals(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        pending = [snapshot for snapshot in snapshots if snapshot.pending_admin_task_proposal]
        if not pending:
            return "No new-task proposals are waiting for Erik or George right now."
        lines = ["Pending new-task proposals:"]
        for snapshot in pending:
            proposal = snapshot.pending_admin_task_proposal or {}
            draft = proposal.get("draft") if isinstance(proposal, dict) else {}
            title = str(draft.get("title") or "untitled proposal") if isinstance(draft, dict) else "untitled proposal"
            category = str(proposal.get("category") or "project")
            lines.append(f"- {snapshot.user.display_name}: [{category}] {title}")
        return "\n".join(lines)

    async def _command_review_task_proposal_approve(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        admin_message = (args.get("comments") or "Approved from the admin console.").strip()
        return await self.runtime.resolve_admin_task_proposal(
            client,
            snapshot.user,
            snapshot.session,
            approve_create=True,
            admin_message=admin_message,
        )

    async def _command_review_task_proposal_revise(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        comments = (args.get("comments") or "").strip()
        if not comments:
            return "I need `comments=` for `review.task_proposal_revise`."
        return await self.runtime.resolve_admin_task_proposal(
            client,
            snapshot.user,
            snapshot.session,
            approve_create=False,
            admin_message=comments,
        )

    async def _command_review_unblocker_approve(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        admin_message = (args.get("comments") or "Approved from the admin console.").strip()
        return await self.runtime.resolve_admin_unblocker_task(
            client,
            snapshot.user,
            snapshot.session,
            approve_create=True,
            admin_message=admin_message,
        )

    async def _command_review_unblocker_revise(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        comments = (args.get("comments") or "").strip()
        if not comments:
            return "I need `comments=` for `review.unblocker_revise`."
        return await self.runtime.resolve_admin_unblocker_task(
            client,
            snapshot.user,
            snapshot.session,
            approve_create=False,
            admin_message=comments,
        )

    async def _command_evidence_before_after(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        sent = 0
        for snapshot in snapshots:
            files: list[Path] = []
            if snapshot.before_start_photos:
                files.append(snapshot.before_start_photos[0])
            if snapshot.after_photos:
                files.append(snapshot.after_photos[-1])
            if not files:
                continue
            await self._send_admin_files(client, f"{snapshot.user.display_name} - before/after photos", files)
            sent += 1
        return f"Sent photo sets for {sent} user(s)."

    async def _command_evidence_visual_progress(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._visual_progress_report(snapshots)

    async def _command_evidence_image_inventory(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._image_inventory_report(snapshots)

    async def _command_evidence_after_without_wrapup(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return self._format_named_list(
            "After photo without wrap-up",
            [snapshot.user.display_name for snapshot in snapshots if snapshot.after_photo_without_summary],
        )

    async def _command_evidence_photo_delta(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        snapshot = self._snapshot_or_error(args.get("user"), snapshots)
        if isinstance(snapshot, str):
            return snapshot
        if not snapshot.before_start_photos or not snapshot.after_photos:
            return f"I do not have both a before and after photo for {snapshot.user.display_name} today."
        fallback = (
            f"I have both photos for {snapshot.user.display_name}, but image comparison is unavailable right now. "
            "You can still inspect the two files directly."
        )
        return await self.analyst.compare_images(
            f"Summarize visual progress for {snapshot.user.display_name}.",
            snapshot.before_start_photos[0],
            snapshot.after_photos[-1],
            fallback,
        )

    async def _command_report_manager(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._generate_manager_report(snapshots)

    async def _command_planning_tomorrow(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._tomorrow_plan_report("What should each person work on tomorrow?", snapshots)

    async def _command_system_validate(
        self,
        _client: discord.Client | None,
        _snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._validate_config_files()

    async def _command_system_dm_reachability(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        return await self._check_dm_reachability(client, snapshots)

    async def _command_system_clickup_health(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._check_clickup_health(snapshots)

    async def _command_debug_resetworkday(
        self,
        client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        if not client:
            return "Discord client is unavailable."
        target = (args.get("user") or "").strip()
        target_snapshots: list[UserDailySnapshot]
        if target:
            snapshot = self._snapshot_or_error(target, snapshots)
            if isinstance(snapshot, str):
                return snapshot
            target_snapshots = [snapshot]
        else:
            target_snapshots = snapshots
        if not target_snapshots:
            return "There are no active roster users to reset."
        results: list[str] = []
        for snapshot in target_snapshots:
            results.append(
                await self.runtime.debug_reset_workday(
                    client,
                    snapshot.user,
                    snapshot.session,
                    notify_user=True,
                )
            )
        return "\n".join(f"- {line}" for line in results)

    async def _command_debug_refreshroster(
        self,
        _client: discord.Client | None,
        _snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        before = {
            user_key: (
                user.display_name,
                user.discord_user_id,
                user.discord_username,
                user.clickup_user_id,
                user.clickup_user_email,
            )
            for user_key, user in self.runtime.roster_by_key.items()
        }
        try:
            await self.runtime.refresh_configuration(force=True)
        except Exception as exc:
            return f"Roster refresh failed: {type(exc).__name__}: {exc}"
        after = {
            user_key: (
                user.display_name,
                user.discord_user_id,
                user.discord_username,
                user.clickup_user_id,
                user.clickup_user_email,
            )
            for user_key, user in self.runtime.roster_by_key.items()
        }
        added_keys = sorted(set(after) - set(before))
        removed_keys = sorted(set(before) - set(after))
        changed_keys = sorted(
            user_key for user_key in (set(before) & set(after))
            if before[user_key] != after[user_key]
        )
        lines = [
            "Roster refresh complete.",
            f"- active roster users now loaded: {len(after)}",
        ]
        if added_keys:
            lines.append(
                "- added: "
                + ", ".join(
                    f"{self.runtime.roster_by_key[user_key].display_name} ({user_key})"
                    for user_key in added_keys
                )
            )
        if removed_keys:
            lines.append("- removed: " + ", ".join(removed_keys))
        if changed_keys:
            lines.append(
                "- changed: "
                + ", ".join(
                    f"{self.runtime.roster_by_key[user_key].display_name} ({user_key})"
                    for user_key in changed_keys
                )
            )
        if not added_keys and not removed_keys and not changed_keys:
            lines.append("- no active roster changes detected")
        lines.append(
            "- current active roster: "
            + (", ".join(user.display_name for user in self.runtime.roster_by_key.values()) or "none")
        )
        return "\n".join(lines)

    async def _command_advanced_interpret(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> str:
        text = (args.get("text") or "").strip()
        if not text:
            return "I need `text=` for `advanced.interpret`."
        response = await self._admin_ai_fallback_response(text, snapshots=snapshots)
        if response:
            return response
        return "AI interpretation is unavailable right now, so I could not suggest a command or answer directly."

    async def _command_advanced_weekly_completion(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._analysis_report(
            "Which tasks can realistically be completed this week?",
            snapshots,
            "I do not have enough ClickUp data yet to estimate this week confidently.",
        )

    async def _command_analysis_risks(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._analysis_report(
            "Find tasks that are blocked, stale, or missing updates.",
            snapshots,
            self._blocked_stale_fallback(snapshots),
        )

    async def _command_advanced_bandwidth(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._analysis_report(
            "Which intern has bandwidth for another task?",
            snapshots,
            self._bandwidth_fallback(snapshots),
        )

    async def _command_advanced_recovery(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._analysis_report(
            "Give me a three-day recovery plan for all blocked work.",
            snapshots,
            self._recovery_fallback(snapshots),
        )

    async def _command_advanced_urgency_ranking(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._analysis_report(
            "Rank current tasks by urgency, impact, and who should own them.",
            snapshots,
            "I can rank them once I have stronger ClickUp context for each person.",
        )

    async def _command_advanced_unowned_tasks(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._unowned_task_report(snapshots)

    async def _command_report_task_changes(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._analysis_report(
            "What ClickUp tasks changed today based on intern updates?",
            snapshots,
            self._tasks_changed_fallback(snapshots),
        )

    async def _command_advanced_plan_vs_priority(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._analysis_report(
            "Compare today's plans against ClickUp priorities.",
            snapshots,
            self._plan_priority_fallback(snapshots),
        )

    async def _command_advanced_post_summary_comments(
        self,
        _client: discord.Client | None,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> str:
        return await self._post_summary_comments(snapshots)

    async def _preview_blockers_dm_stuck(
        self,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> AdminActionPreview:
        targets = [snapshot.user.display_name for snapshot in snapshots if snapshot.stuck_now]
        return AdminActionPreview(
            title="Preview `blockers.dm_stuck`",
            summary=self._format_named_list("These interns will receive a help-needed DM", targets),
            command_id="blockers.dm_stuck",
        )

    async def _preview_presence_remind_clock_in(
        self,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> AdminActionPreview:
        targets = [snapshot.user.display_name for snapshot in snapshots if not snapshot.clocked_in]
        return AdminActionPreview(
            title="Preview `presence.remind_clock_in`",
            summary=self._format_named_list("These interns will receive a clock-in reminder", targets),
            command_id="presence.remind_clock_in",
        )

    async def _preview_blockers_remind_missing_photos(
        self,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> AdminActionPreview:
        targets: list[str] = []
        for snapshot in snapshots:
            if snapshot.session.awaiting_start_photo or snapshot.session.awaiting_clock_out_photo:
                targets.append(snapshot.user.display_name)
        return AdminActionPreview(
            title="Preview `blockers.remind_missing_photos`",
            summary=self._format_named_list("These interns will receive a photo reminder", targets),
            command_id="blockers.remind_missing_photos",
        )

    async def _preview_task_switch(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        summary = f"Switch `{args.get('user')}` to `{args.get('task')}`."
        if isinstance(target, UserDailySnapshot):
            summary = (
                f"{target.user.display_name} will be switched to `{args.get('task')}`.\n\n"
                "The bot will pause the current task, put it on hold, and start task-switch onboarding."
            )
        return AdminActionPreview(title="Preview `task.switch`", summary=summary, command_id="task.switch", args=dict(args))

    async def _preview_task_resume(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        return AdminActionPreview(
            title="Preview `task.resume`",
            summary=f"{label} will be marked unblocked, moved back to `in progress`, and nudged to resume work.",
            command_id="task.resume",
            args=dict(args),
        )

    async def _preview_task_prioritize(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        return AdminActionPreview(
            title="Preview `task.prioritize`",
            summary=f"{label} will receive a DM telling them to switch attention to the highest-priority assigned ClickUp task.",
            command_id="task.prioritize",
            args=dict(args),
        )

    async def _preview_review_close(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        task_label = args.get("task_id") or args.get("task")
        task_suffix = f" for `{task_label}`" if task_label else ""
        return AdminActionPreview(
            title="Preview `review.close`",
            summary=f"{label}'s pending task review{task_suffix} will be approved and the reviewed task will be closed in ClickUp.",
            command_id="review.close",
            args=dict(args),
        )

    async def _preview_review_rework(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        comments = args.get("comments") or "No comments provided."
        task_label = args.get("task_id") or args.get("task")
        task_suffix = f" for `{task_label}`" if task_label else ""
        return AdminActionPreview(
            title="Preview `review.rework`",
            summary=f"{label} will receive rework comments{task_suffix} and that task will be moved back to `in progress`.\n\nComments:\n{comments}",
            command_id="review.rework",
            args=dict(args),
        )

    async def _preview_review_unblocker_approve(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        return AdminActionPreview(
            title="Preview `review.unblocker_approve`",
            summary=f"The pending unblocker-task draft for {label} will be created in ClickUp.",
            command_id="review.unblocker_approve",
            args=dict(args),
        )

    async def _preview_review_task_proposal_approve(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        return AdminActionPreview(
            title="Preview `review.task_proposal_approve`",
            summary=f"The pending project/overhead task proposal for {label} will be created in ClickUp.",
            command_id="review.task_proposal_approve",
            args=dict(args),
        )

    async def _preview_review_task_proposal_revise(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        comments = args.get("comments") or "No comments provided."
        return AdminActionPreview(
            title="Preview `review.task_proposal_revise`",
            summary=f"{label} will receive revision comments for the proposed task.\n\nComments:\n{comments}",
            command_id="review.task_proposal_revise",
            args=dict(args),
        )

    async def _preview_review_unblocker_revise(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = self._snapshot_or_error(args.get("user"), snapshots)
        label = args.get("user") or "that user"
        if isinstance(target, UserDailySnapshot):
            label = target.user.display_name
        comments = args.get("comments") or "No comments provided."
        return AdminActionPreview(
            title="Preview `review.unblocker_revise`",
            summary=f"{label} will receive unblocker-task revision comments.\n\nComments:\n{comments}",
            command_id="review.unblocker_revise",
            args=dict(args),
        )

    async def _preview_advanced_post_summary_comments(
        self,
        snapshots: list[UserDailySnapshot],
        _args: dict[str, str],
    ) -> AdminActionPreview:
        return AdminActionPreview(
            title="Preview `advanced.post_summary_comments`",
            summary=self._format_named_list(
                "These interns will get a ClickUp summary-comment sync",
                [snapshot.user.display_name for snapshot in snapshots],
            ),
            command_id="advanced.post_summary_comments",
        )

    async def _preview_debug_resetworkday(
        self,
        snapshots: list[UserDailySnapshot],
        args: dict[str, str],
    ) -> AdminActionPreview:
        target = (args.get("user") or "").strip()
        if target:
            resolved = self._snapshot_or_error(target, snapshots)
            labels = [resolved.user.display_name] if isinstance(resolved, UserDailySnapshot) else [target]
        else:
            labels = [snapshot.user.display_name for snapshot in snapshots]
        summary_lines = [
            "This resets local workflow state so today's clock-in flow can be tested again.",
            "It preserves stored messages and files, but new workflow logic will ignore messages from before the reset.",
            "",
            "Targets:",
        ]
        summary_lines.extend(f"- {label}" for label in labels or ["none"])
        return AdminActionPreview(
            title="Preview `debug.resetworkday`",
            summary="\n".join(summary_lines),
            command_id="debug.resetworkday",
            args=dict(args),
        )

    def _last_message_report(self, snapshots: list[UserDailySnapshot]) -> str:
        now = datetime.now(tz=resolve_timezone(ADMIN_DISPLAY_TIMEZONE))
        lines = ["Roster status:"]
        for snapshot in snapshots:
            last_user = (
                format_admin_datetime(snapshot.last_inbound.created_at, reference=now)
                if snapshot.last_inbound
                else "no user message yet"
            )
            lunch_note = ""
            if snapshot.on_lunch_break:
                lunch_started_at = str(snapshot.session.metadata.get("lunch_started_at") or "").strip()
                lunch_note = (
                    f"; on lunch since {format_admin_datetime(lunch_started_at, reference=now)}"
                    if lunch_started_at
                    else "; on lunch since unknown"
                )
            lines.append(f"- {snapshot.user.display_name}: last user message {last_user}{lunch_note}")
        return "\n".join(lines)

    def _stuck_report(self, snapshots: list[UserDailySnapshot]) -> str:
        stuck = [snapshot for snapshot in snapshots if snapshot.stuck_now]
        if not stuck:
            return "Nobody is currently marked stuck."
        now = datetime.now(tz=resolve_timezone(ADMIN_DISPLAY_TIMEZONE))
        lines = ["Currently stuck:"]
        for snapshot in stuck:
            suffix = " (no help requested)" if snapshot.blocker_state == "blocked_no_help" else ""
            stuck_since = self._coerce_snapshot_datetime(snapshot, snapshot.session.stuck_since)
            lines.append(
                f"- {snapshot.user.display_name}: stuck since {format_admin_datetime(stuck_since, reference=now)}; "
                f"blocker: {snapshot.session.latest_blocker or 'no blocker text'}{suffix}"
            )
        return "\n".join(lines)

    def _admin_intervention_report(self, snapshots: list[UserDailySnapshot]) -> str:
        if not self.runtime.config:
            return "Configuration is not loaded."
        now = datetime.now(tz=resolve_timezone(ADMIN_DISPLAY_TIMEZONE))
        threshold_hours = self.runtime.config.schedule.stuck_alert_after_hours
        lines = ["Admin intervention candidates:"]
        found = False
        for snapshot in snapshots:
            if not snapshot.session.stuck_since:
                continue
            stuck_since = self._coerce_snapshot_datetime(snapshot, snapshot.session.stuck_since)
            if not stuck_since:
                continue
            hours = (now - stuck_since).total_seconds() / 3600
            if hours >= threshold_hours or snapshot.session.stuck_alerted_at:
                found = True
                suffix = " (no help requested)" if snapshot.blocker_state == "blocked_no_help" else ""
                lines.append(
                    f"- {snapshot.user.display_name}: {snapshot.session.latest_blocker or 'no blocker text'} "
                    f"(stuck {hours:.1f}h; since {format_admin_datetime(stuck_since, reference=now)}){suffix}"
                )
        if not found:
            lines.append("- none beyond the current intervention threshold")
        return "\n".join(lines)

    def _coerce_snapshot_datetime(
        self,
        snapshot: UserDailySnapshot,
        raw_value: str | None,
    ) -> datetime | None:
        if not raw_value:
            return None
        timezone_name = self.runtime.resolve_user_timezone_name(snapshot.user)
        parser = getattr(self.runtime, "_coerce_datetime", None)
        if callable(parser):
            return parser(raw_value, timezone_name=timezone_name)
        try:
            parsed = datetime.fromisoformat(raw_value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return localize_datetime(parsed, timezone_name)
        return parsed

    async def _dm_stuck_users(self, client: discord.Client, snapshots: list[UserDailySnapshot]) -> str:
        if not self.runtime.config:
            return "Configuration is not loaded."
        sent_to: list[str] = []
        for snapshot in snapshots:
            if not snapshot.stuck_now:
                continue
            now = self.runtime.resolve_user_local_now(snapshot.user)
            await self.runtime._send_dm(
                client,
                snapshot.user,
                snapshot.session,
                "What exact help do you need right now? Tell me the blocker, what you already tried, and what decision or resource would unblock you fastest.",
                now,
            )
            sent_to.append(snapshot.user.display_name)
        return self._format_named_list("Asked these stuck users for exact help needs", sent_to)

    async def _dm_missing_clock_in(self, client: discord.Client, snapshots: list[UserDailySnapshot]) -> str:
        if not self.runtime.config:
            return "Configuration is not loaded."
        sent_to: list[str] = []
        for snapshot in snapshots:
            if snapshot.clocked_in:
                continue
            now = self.runtime.resolve_user_local_now(snapshot.user)
            previous_session = self.runtime._clone_session_state(snapshot.session)
            await self.runtime._send_dm(
                client,
                snapshot.user,
                snapshot.session,
                self.runtime.config.prompts.clock_in_reminder,
                now,
            )
            snapshot.session.last_clock_in_prompt_at = now.isoformat()
            await self.runtime._persist_session_state(
                snapshot.user,
                snapshot.session,
                now=now,
                previous_session=previous_session,
                trigger="admin_clock_in_reminder",
                details={"source": "admin_console"},
            )
            sent_to.append(snapshot.user.display_name)
        if sent_to and hasattr(self.runtime, "write_dashboard"):
            await self.runtime.write_dashboard()
        return self._format_named_list("Sent clock-in reminders", sent_to)

    async def _generate_manager_report(self, snapshots: list[UserDailySnapshot]) -> str:
        fallback = self._manager_report_fallback(snapshots)
        context = await self._build_analysis_context(snapshots)
        return await self.analyst.answer("Generate today's manager report.", context, fallback)

    async def _validate_config_files(self) -> str:
        lines = ["Config validation:"]
        try:
            await self.runtime.refresh_configuration(force=True)
            lines.append("- agent.config.json parsed successfully")
            lines.append("- roster.csv parsed successfully")
        except Exception as exc:
            return f"Config validation failed: {exc}"
        config = self.runtime.config
        if not config:
            return "Configuration is not loaded."
        if not self.runtime.roster_by_key:
            lines.append("- no active roster users found")
        else:
            lines.append(f"- active roster users: {len(self.runtime.roster_by_key)}")
        duplicates = self._find_duplicates()
        if duplicates:
            lines.extend(f"- {item}" for item in duplicates)
        else:
            lines.append("- no duplicate roster keys or Discord IDs found")
        for env_name in ("DISCORD_BOT_TOKEN", "CLICKUP_API_TOKEN", "OPENAI_API_KEY"):
            status = "present" if os.environ.get(env_name) else "missing"
            lines.append(f"- {env_name}: {status}")
        lines.append(f"- admin console AI fallback: {'enabled' if config.admin_console.enable_ai_fallback else 'disabled'}")
        return "\n".join(lines)

    async def _check_dm_reachability(self, client: discord.Client, snapshots: list[UserDailySnapshot]) -> str:
        lines = ["Discord DM reachability check:"]
        for snapshot in snapshots:
            try:
                user = await client.fetch_user(snapshot.user.discord_user_id)
                await user.create_dm()
                lines.append(f"- {snapshot.user.display_name}: DM channel opens")
            except Exception as exc:
                lines.append(f"- {snapshot.user.display_name}: DM channel failed ({type(exc).__name__}: {exc})")
        lines.append("Note: opening a DM channel is a good sign, but Discord can still block a real send in some cases.")
        return "\n".join(lines)

    async def _check_clickup_health(self, snapshots: list[UserDailySnapshot]) -> str:
        if not self.runtime.clickup:
            return "ClickUp is not configured."
        ok, detail = await self.runtime.clickup.check_connection()
        lines = [f"ClickUp connection: {'ok' if ok else 'problem'}", f"- {detail}"]
        for snapshot in snapshots:
            resolved = await self.runtime.clickup.resolve_clickup_user_id(snapshot.user)
            bundle = await self.runtime.clickup.get_context_bundle(snapshot.user, snapshot.session, snapshot.messages)
            lines.append(
                f"- {snapshot.user.display_name}: clickup_user_id={resolved or 'unresolved'}; "
                f"active_task={bundle.active_task_name or 'none selected'}"
            )
        lines.append("This is a read-path health check. Use `run advanced.post_summary_comments` to test the write path explicitly.")
        return "\n".join(lines)

    async def _tomorrow_plan_report(self, question: str, snapshots: list[UserDailySnapshot]) -> str:
        fallback_lines = ["Tomorrow recommendations:"]
        for snapshot in snapshots:
            recommendation = snapshot.active_clickup_task_name or snapshot.session.latest_plan or "No obvious task yet."
            if self.runtime.clickup:
                suggestions = await self.runtime.clickup.suggest_next_tasks(
                    snapshot.user,
                    snapshot.session,
                    snapshot.messages,
                    exclude_task_ids={task_id for task_id in [snapshot.active_clickup_task_id] if task_id},
                    limit=2,
                )
                if suggestions:
                    recommendation = suggestions[0].get("name") or recommendation
            fallback_lines.append(f"- {snapshot.user.display_name}: {recommendation}")
        context = await self._build_analysis_context(snapshots)
        return await self.analyst.answer(question, context, "\n".join(fallback_lines))

    async def _analysis_report(self, question: str, snapshots: list[UserDailySnapshot], fallback: str) -> str:
        context = await self._build_analysis_context(snapshots)
        return await self.analyst.answer(question, context, fallback)

    async def _remind_missing_photos(self, client: discord.Client, snapshots: list[UserDailySnapshot]) -> str:
        if not self.runtime.config:
            return "Configuration is not loaded."
        sent_to: list[str] = []
        for snapshot in snapshots:
            now = self.runtime.resolve_user_local_now(snapshot.user)
            if snapshot.session.awaiting_start_photo:
                await self.runtime._send_dm(
                    client,
                    snapshot.user,
                    snapshot.session,
                    "I still need the before-start picture. Send it when you can.",
                    now,
                )
                sent_to.append(snapshot.user.display_name)
                continue
            if snapshot.session.awaiting_clock_out_photo:
                await self.runtime._send_dm(
                    client,
                    snapshot.user,
                    snapshot.session,
                    "I still need the end-of-day photo before I can close out your update.",
                    now,
                )
                sent_to.append(snapshot.user.display_name)
        return self._format_named_list("Sent photo reminders", sent_to)

    def _visual_progress_report(self, snapshots: list[UserDailySnapshot]) -> str:
        lines = ["Visual progress today:"]
        found = False
        for snapshot in snapshots:
            if snapshot.before_start_photos and snapshot.after_photos:
                found = True
                project = snapshot.active_clickup_task_name or snapshot.session.latest_plan or "unspecified project"
                lines.append(f"- {snapshot.user.display_name}: {project}")
        if not found:
            lines.append("- no users have both a before and after photo yet")
        return "\n".join(lines)

    def _image_inventory_report(self, snapshots: list[UserDailySnapshot]) -> str:
        lines = ["Image files saved today:"]
        found = False
        for snapshot in snapshots:
            files = snapshot.before_start_photos + snapshot.progress_photos + snapshot.after_photos
            if not files:
                continue
            found = True
            lines.append(f"- {snapshot.user.display_name}:")
            lines.extend(f"  - {path}" for path in files)
        if not found:
            lines.append("- none")
        return "\n".join(lines)

    async def _unowned_task_report(self, snapshots: list[UserDailySnapshot]) -> str:
        if not self.runtime.clickup:
            return "ClickUp is not configured."
        mission_board_id = self.runtime.config.clickup.mission_board_list_id if self.runtime.config else None
        if not mission_board_id:
            return "Mission Board is not configured in ClickUp."
        task_lines: list[str] = []
        tasks = await self.runtime.clickup.list_list_tasks(mission_board_id, limit=100, include_closed=False)
        active_task_ids = {snapshot.active_clickup_task_id for snapshot in snapshots if snapshot.active_clickup_task_id}
        for task in tasks:
            task_id = str(task.get("id") or "")
            if not task_id or task_id in active_task_ids:
                continue
            if task.get("assignees"):
                continue
            task_lines.append(
                f"- {task.get('name')} | status={(task.get('status') or {}).get('status') or 'unknown'} | "
                f"priority={(task.get('priority') or {}).get('priority') or 'none'}"
            )
        if not task_lines:
            return "I did not find any unassigned Mission Board tasks outside the active intern focus set."
        return "Mission Board tasks with no intern actively focused on them right now:\n" + "\n".join(task_lines[:20])

    async def _post_summary_comments(self, snapshots: list[UserDailySnapshot]) -> str:
        if not self.runtime.config:
            return "Configuration is not loaded."
        lines = ["ClickUp comment posting:"]
        for snapshot in snapshots:
            now = self.runtime.resolve_user_local_now(snapshot.user)
            ok, detail = await self.runtime.force_clickup_sync(snapshot.user, snapshot.session, now=now)
            lines.append(f"- {snapshot.user.display_name}: {detail if ok else 'skipped - ' + detail}")
        return "\n".join(lines)

    async def _build_analysis_context(self, snapshots: list[UserDailySnapshot]) -> str:
        payload: dict[str, Any] = {
            "date": datetime.now(tz=resolve_timezone(self.runtime.config.timezone)).date().isoformat() if self.runtime.config else None,
            "users": [],
        }
        if self.runtime.clickup:
            ok, detail = await self.runtime.clickup.check_connection()
            payload["clickup_health"] = {"ok": ok, "detail": detail}
        for snapshot in snapshots:
            tracking_state = await self.runtime._get_task_tracking_state(snapshot.user, snapshot.session)
            user_payload: dict[str, Any] = {
                "user_key": snapshot.user.user_key,
                "display_name": snapshot.user.display_name,
                "timezone": self.runtime.resolve_user_timezone_name(snapshot.user),
                "workday_date": snapshot.session.session_date,
                "clocked_in_at": snapshot.session.clocked_in_at,
                "clocked_out_at": snapshot.session.clocked_out_at,
                "stage": snapshot.session.stage,
                "stuck_since": snapshot.session.stuck_since,
                "latest_plan": snapshot.session.latest_plan,
                "latest_status": snapshot.session.latest_status,
                "latest_blocker": snapshot.session.latest_blocker,
                "active_clickup_task_name": snapshot.active_clickup_task_name,
                "active_clickup_task_id": snapshot.active_clickup_task_id,
                "timer_running": tracking_state["timer_running"],
                "timer_source": tracking_state["timer_source"],
                "timer_task_id": tracking_state["timer_task_id"],
                "timer_task_name": tracking_state["timer_task_name"],
                "timer_note": tracking_state["timer_note"],
                "before_start_photo_count": len(snapshot.before_start_photos),
                "after_photo_count": len(snapshot.after_photos),
                "last_user_message_at": snapshot.last_user_message_at,
                "recent_user_messages": [
                    message.content
                    for message in snapshot.messages
                    if message.direction == "inbound" and message.content.strip()
                ][-3:],
                "task_onboarding": {
                    "plan": str(snapshot.session.metadata.get("task_onboarding_plan") or "") or None,
                    "tangible_result": str(snapshot.session.metadata.get("task_onboarding_tangible_result") or "") or None,
                    "necessity": str(snapshot.session.metadata.get("task_onboarding_necessity") or "") or None,
                    "effectiveness": str(snapshot.session.metadata.get("task_onboarding_effectiveness") or "") or None,
                    "estimated_duration": str(snapshot.session.metadata.get("task_onboarding_estimated_duration") or "") or None,
                    "reconsider_threshold": str(snapshot.session.metadata.get("task_onboarding_reconsider_threshold") or "") or None,
                    "fallback_plan": str(snapshot.session.metadata.get("task_onboarding_fallback_plan") or "") or None,
                    "summary": str(snapshot.session.metadata.get("last_task_onboarding_summary") or "") or None,
                },
            }
            if self.runtime.clickup:
                bundle = await self.runtime.clickup.get_context_bundle(snapshot.user, snapshot.session, snapshot.messages)
                tasks = await self.runtime.clickup.list_assigned_tasks(snapshot.user, limit=8)
                user_payload["clickup_context"] = bundle.context
                user_payload["assigned_tasks"] = [
                    {
                        "id": str(task.get("id")),
                        "name": task.get("name"),
                        "status": (task.get("status") or {}).get("status"),
                        "priority": (task.get("priority") or {}).get("priority"),
                    }
                    for task in tasks
                ]
                suggestions = await self.runtime.clickup.suggest_next_tasks(
                    snapshot.user,
                    snapshot.session,
                    snapshot.messages,
                    exclude_task_ids={task_id for task_id in [snapshot.active_clickup_task_id] if task_id},
                    limit=3,
                )
                user_payload["next_task_candidates"] = [
                    {
                        "id": str(task.get("id")),
                        "name": task.get("name"),
                        "status": (task.get("status") or {}).get("status"),
                        "priority": (task.get("priority") or {}).get("priority"),
                    }
                    for task in suggestions
                ]
            payload["users"].append(user_payload)
        return json.dumps(payload, indent=2)

    async def _admin_ai_fallback_response(
        self,
        text: str,
        *,
        snapshots: list[UserDailySnapshot] | None = None,
    ) -> str | None:
        suggestion = await self._suggest_interpretation(text)
        if suggestion:
            return suggestion
        return await self._answer_freeform_admin_request(text, snapshots=snapshots)

    async def _answer_freeform_admin_request(
        self,
        text: str,
        *,
        snapshots: list[UserDailySnapshot] | None = None,
    ) -> str | None:
        if not self.analyst.enabled or not self.analyst.client:
            return None
        resolved_snapshots = snapshots if snapshots is not None else await self._collect_snapshots()
        context = await self._build_analysis_context(resolved_snapshots)
        fallback = (
            "I could not map that to a strong existing command, and I could not produce a reliable direct answer.\n\n"
            + self._grammar_hint()
        )
        answer = await self.analyst.answer(
            (
                "An admin sent a free-form request after no strong deterministic command match was found.\n"
                "Answer directly using the provided operational data.\n"
                "If the data is incomplete, say so clearly.\n"
                "Do not claim that you ran a command or changed state.\n\n"
                f"Admin request:\n{text}"
            ),
            context,
            fallback,
        )
        if not answer.strip():
            return None
        if answer == fallback:
            return fallback
        return "Best-effort answer:\n" + answer

    async def _suggest_interpretation(self, text: str) -> str | None:
        interpreter = getattr(self.runtime, "interface_intelligence", None)
        if not interpreter or not hasattr(interpreter, "resolve_admin_command"):
            return None
        templates: list[str] = []
        for command in self.registry.commands:
            if command.command_id == "advanced.interpret":
                continue
            if command.examples:
                templates.append(command.examples[0])
            else:
                templates.append(f"run {command.command_id}")
        match = await interpreter.resolve_admin_command(text, templates)
        if not match or not match.canonical_command.strip():
            return None
        reason = f"\nReason: {match.reason}" if match.reason else ""
        return (
            "Suggested deterministic command:\n"
            f"- `{match.canonical_command.strip()}`\n"
            f"- confidence: {match.confidence:.2f}{reason}\n\n"
            "Run that exact command if it matches what you meant."
        )

    def _resolve_paths(self, raw_value: Any) -> list[Path]:
        if not isinstance(raw_value, list):
            return []
        paths: list[Path] = []
        for item in raw_value:
            if isinstance(item, str):
                path = Path(item)
                if path.exists():
                    paths.append(path)
        return paths

    def _infer_photo_roles(self, messages: list[MessageRecord]) -> tuple[list[Path], list[Path], list[Path]]:
        image_messages = [
            message
            for message in messages
            if message.direction == "inbound" and any(attachment.local_path for attachment in message.attachments)
        ]
        if not image_messages:
            return [], [], []
        before = _attachment_paths(image_messages[0])
        after = _attachment_paths(image_messages[-1]) if len(image_messages) > 1 else []
        middle: list[Path] = []
        for message in image_messages[1:-1]:
            middle.extend(_attachment_paths(message))
        return before, after, middle

    def _find_duplicates(self) -> list[str]:
        rows = list(self.runtime.roster_by_key.values())
        duplicate_messages: list[str] = []
        seen_keys: set[str] = set()
        seen_ids: set[int] = set()
        for row in rows:
            if row.user_key in seen_keys:
                duplicate_messages.append(f"duplicate user_key found: {row.user_key}")
            seen_keys.add(row.user_key)
            if row.discord_user_id in seen_ids:
                duplicate_messages.append(f"duplicate discord_user_id found: {row.discord_user_id}")
            seen_ids.add(row.discord_user_id)
        return duplicate_messages

    def _find_snapshot(self, text: str, snapshots: list[UserDailySnapshot]) -> UserDailySnapshot | None:
        normalized = _normalize_identifier(text)
        for snapshot in snapshots:
            candidates = {
                _normalize_identifier(snapshot.user.user_key),
                _normalize_identifier(snapshot.user.display_name),
                _normalize_identifier(snapshot.user.discord_username),
                _normalize_identifier(snapshot.user.storage_folder_name),
            }
            if normalized in candidates:
                return snapshot
        return None

    def _snapshot_or_error(
        self,
        user_label: str | None,
        snapshots: list[UserDailySnapshot],
    ) -> UserDailySnapshot | str:
        target_name = (user_label or "").strip()
        if not target_name:
            return "I still need `user=` for that command."
        snapshot = self._find_snapshot(target_name, snapshots)
        if not snapshot and target_name.lower().endswith("s"):
            snapshot = self._find_snapshot(target_name[:-1], snapshots)
        if not snapshot:
            return f"I could not find roster user {target_name!r}."
        return snapshot

    def _manager_report_fallback(self, snapshots: list[UserDailySnapshot]) -> str:
        now = datetime.now(tz=resolve_timezone(ADMIN_DISPLAY_TIMEZONE))
        lines = ["Today's manager report:"]
        for snapshot in snapshots:
            clocked_in = (
                format_admin_datetime(snapshot.session.clocked_in_at, reference=now)
                if snapshot.session.clocked_in_at
                else "no"
            )
            clocked_out = (
                format_admin_datetime(snapshot.session.clocked_out_at, reference=now)
                if snapshot.session.clocked_out_at
                else "no"
            )
            lines.append(
                f"- {snapshot.user.display_name}: stage={snapshot.session.stage}; "
                f"clocked_in={clocked_in}; "
                f"clocked_out={clocked_out}; "
                f"task={snapshot.active_clickup_task_name or 'unresolved'}; "
                f"blocker={(snapshot.session.latest_blocker or 'none')}"
                + (" [no help requested]" if snapshot.blocker_state == "blocked_no_help" else "")
            )
        return "\n".join(lines)

    def _blocked_task_fallback(self, snapshots: list[UserDailySnapshot]) -> str:
        blocked = [
            f"- {snapshot.user.display_name}: {snapshot.session.latest_blocker}"
            + (" (no help requested)" if snapshot.blocker_state == "blocked_no_help" else "")
            for snapshot in snapshots
            if snapshot.session.latest_blocker
        ]
        if not blocked:
            return "No blockers have been recorded yet."
        return "Recorded blockers:\n" + "\n".join(blocked)

    def _bandwidth_fallback(self, snapshots: list[UserDailySnapshot]) -> str:
        candidates = [
            snapshot.user.display_name
            for snapshot in snapshots
            if snapshot.active_now and not snapshot.stuck_now and snapshot.session.latest_blocker is None
        ]
        return self._format_named_list("Best bandwidth candidates from current local state", candidates)

    def _recovery_fallback(self, snapshots: list[UserDailySnapshot]) -> str:
        blocked = [snapshot for snapshot in snapshots if snapshot.session.latest_blocker]
        if not blocked:
            return "No blocked work is recorded right now."
        lines = ["Three-day recovery outline:"]
        for snapshot in blocked:
            lines.append(
                f"- {snapshot.user.display_name}: day 1 clarify blocker, day 2 resolve dependency, "
                f"day 3 verify progress against {snapshot.active_clickup_task_name or 'the active task'}."
            )
        return "\n".join(lines)

    def _tasks_changed_fallback(self, snapshots: list[UserDailySnapshot]) -> str:
        lines = ["Likely ClickUp task changes from today's intern updates:"]
        found = False
        for snapshot in snapshots:
            if snapshot.active_clickup_task_name and (snapshot.session.latest_status or snapshot.session.latest_plan):
                found = True
                lines.append(
                    f"- {snapshot.active_clickup_task_name}: {snapshot.session.latest_status or snapshot.session.latest_plan}"
                )
        if not found:
            lines.append("- none inferred yet")
        return "\n".join(lines)

    def _plan_priority_fallback(self, snapshots: list[UserDailySnapshot]) -> str:
        lines = ["Plan versus ClickUp priority:"]
        for snapshot in snapshots:
            lines.append(
                f"- {snapshot.user.display_name}: plan={snapshot.session.latest_plan or 'none'} | "
                f"active ClickUp task={snapshot.active_clickup_task_name or 'unresolved'}"
            )
        return "\n".join(lines)

    def _blocked_stale_fallback(self, snapshots: list[UserDailySnapshot]) -> str:
        lines = ["Blocked, stale, or missing-update candidates:"]
        for snapshot in snapshots:
            if snapshot.stuck_now:
                suffix = " (no help requested)" if snapshot.blocker_state == "blocked_no_help" else ""
                lines.append(f"- {snapshot.user.display_name}: blocked - {snapshot.session.latest_blocker or 'no blocker text'}{suffix}")
            elif not snapshot.responded:
                lines.append(f"- {snapshot.user.display_name}: missing update - no response today")
            elif snapshot.clocked_in and not snapshot.session.latest_status and not snapshot.clocked_out:
                lines.append(f"- {snapshot.user.display_name}: stale - no meaningful progress update yet")
        if len(lines) == 1:
            lines.append("- none")
        return "\n".join(lines)


async def _safe_interaction_message(
    interaction: discord.Interaction,
    content: str,
    *,
    view: discord.ui.View | None = None,
    replace: bool,
) -> None:
    text = content[:_MAX_DISCORD_MESSAGE]
    if replace:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, view=view)
        else:
            await interaction.response.edit_message(content=text, view=view)
        return
    if interaction.response.is_done():
        await interaction.followup.send(text, view=view)
    else:
        await interaction.response.send_message(text, view=view)


def _required_arg_names(command: AdminCommandDefinition) -> list[str]:
    return [argument.name for argument in command.args if argument.required]


def _normalize_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _attachment_paths(message: MessageRecord) -> list[Path]:
    paths: list[Path] = []
    for attachment in message.attachments:
        if attachment.local_path:
            path = Path(attachment.local_path)
            if path.exists():
                paths.append(path)
    return paths


def _split_message(content: str) -> list[str]:
    if len(content) <= _MAX_DISCORD_MESSAGE:
        return [content]
    lines = content.splitlines()
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > _MAX_DISCORD_MESSAGE:
            if current:
                chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [content[:_MAX_DISCORD_MESSAGE]]


def _pick_highest_priority_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    return ClickUpClient.pick_highest_priority_task(tasks)


def _as_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"
