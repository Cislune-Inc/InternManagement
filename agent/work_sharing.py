"""Explicit, private work-update previews. Never publishes to channels or email.

Worker confirmation creates a versioned handoff for owner review, not permission
to publish. Original work events and the attendance ledger remain independent.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .slack_work_intake import PROJECTS, _safe


class WorkSharing:
    def __init__(self, store: Any) -> None:
        self.store = store
        with store._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS work_share_drafts (
                id TEXT PRIMARY KEY, item_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                revision INTEGER NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, confirmed_at TEXT,
                UNIQUE(item_id,revision)
            )""")

    @staticmethod
    def _render(row: Any) -> str:
        payload = json.loads(row["payload"])
        lines = [f"*{_safe(payload['project'])} · {_safe(payload['worker'])}*",
                 f"Worker report: {_safe(payload['result'])}"]
        if payload["next"]:
            lines.append("Next / blocker: " + _safe(payload["next"]))
        for url in payload["evidence"]:
            lines.append("Reference (not verified): " + _safe(url))
        return "\n".join(lines)

    def preview(self, actor: str) -> str:
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM work_intake_items WHERE owner_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (actor,)).fetchone()
            if not item:
                return "Describe work first with `work <project>: <plan>`, then record what changed with `work update <result>`."
            if not item["project_key"]:
                return "Choose the project with `work project <name>` before preparing a team update. Nothing was sent."
            # Only the worker's explicitly supplied report is eligible. Do not
            # copy manager notes, AI speculation, prior DMs or clock data.
            events = conn.execute("SELECT kind,text FROM work_intake_events WHERE item_id=? AND actor_id=? ORDER BY id DESC", (item["id"], actor)).fetchall()
            result = ""
            for event in events:
                if event["kind"] in {"edit", "project", "detail"}:
                    break  # A revised plan is not a new result.
                if event["kind"] == "update":
                    result = event["text"]
                    break
            if not result:
                return "No result recorded yet. Use `work update <what changed or what blocked you>`; a plan alone is not a completed result. Nothing was sent."
            follow = next((e["text"] for e in events if e["kind"] == "next"), "")
            refs = [r[0] for r in conn.execute("SELECT url FROM work_evidence WHERE item_id=? AND actor_id=? ORDER BY captured_at DESC LIMIT 3", (item["id"], actor))]
            payload = {"project": PROJECTS[item["project_key"]], "project_key": item["project_key"],
                       "worker": item["owner_name"], "result": result[:2200], "next": follow[:700],
                       "evidence": refs, "source_item": item["id"], "source_revision": item["revision"]}
            ident = "SH-" + hashlib.sha256(f"{item['id']}:{item['revision']}".encode()).hexdigest()[:12]
            conn.execute("INSERT OR IGNORE INTO work_share_drafts(id,item_id,owner_id,revision,payload,created_at) VALUES (?,?,?,?,?,?)",
                         (ident, item["id"], actor, item["revision"], json.dumps(payload), datetime.now(timezone.utc).isoformat()))
            row = conn.execute("SELECT * FROM work_share_drafts WHERE id=?", (ident,)).fetchone()
            return (f"*Private draft `{ident}` — not sent*\n" + self._render(row)
                    + f"\n\nConfirm accuracy: `work confirm {ident}`. This saves a Codex/owner handoff; it does not post to a channel."
                    + "\nCorrect it with `work update <corrected result>` or add `work next <next step / blocker>`, then request `work draft` again.")

    def confirm(self, actor: str, ident: str) -> str:
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM work_share_drafts WHERE id=? AND owner_id=?", (ident, actor)).fetchone()
            if not row:
                return "Draft not found for your identity. Use `work draft`."
            item = conn.execute("SELECT revision FROM work_intake_items WHERE id=?", (row["item_id"],)).fetchone()
            if not item or item["revision"] != row["revision"]:
                return "Your work changed after this draft. Use `work draft` and confirm the updated version. Nothing was sent."
            conn.execute("UPDATE work_share_drafts SET confirmed_at=COALESCE(confirmed_at,?) WHERE id=?", (datetime.now(timezone.utc).isoformat(), ident))
            return f"Confirmed `{ident}` for the owner/Codex handoff. Nothing was posted to a channel or emailed. Only Erik can explicitly request publication."

    def confirmed(self, *, actor: str | None = None) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            return [dict(r) for r in conn.execute("""SELECT d.* FROM work_share_drafts d
                JOIN work_intake_items i ON i.id=d.item_id AND i.revision=d.revision
                WHERE d.confirmed_at IS NOT NULL AND (? IS NULL OR d.owner_id=?)
                ORDER BY d.confirmed_at DESC LIMIT 5""", (actor, actor))]

    def queue(self, *, actor: str | None = None) -> str:
        rows = self.confirmed(actor=actor)
        return ("Confirmed handoffs (private, nothing sent):\n" + "\n\n".join(
            f"`{r['id']}` · source `{r['item_id']}` revision {r['revision']}\n" + self._render(r) for r in rows)
            if rows else "No current confirmed handoffs yet. Use `work update`, `work draft`, then `work confirm SH-id`.")


async def handle(runtime: Any, slack_id: str, text: str) -> bool:
    normalized = text.strip().lower()
    if not (normalized in {"work draft", "work drafts", "work handoffs"} or normalized.startswith(("work confirm ", "work publish ", "work send "))):
        return False
    from .slack_work_intake import SlackWorkIntake
    SlackWorkIntake(runtime.state_store)
    service = WorkSharing(runtime.state_store)
    admin = runtime.admin_profile_by_slack_user_id(slack_id)
    owner = bool(admin and admin.discord_user_id == runtime.config.admin_discord_user_id)
    if normalized == "work draft":
        response = service.preview(slack_id)
    elif normalized.startswith("work confirm "):
        response = service.confirm(slack_id, text.strip().split(maxsplit=2)[2].strip())
    elif normalized in {"work drafts", "work handoffs"}:
        response = service.queue(actor=None if owner else slack_id)
    else:
        response = "Channel publishing is disabled. Your draft stays private. Erik must explicitly request the exact message and destination; no automatic posts or emails."
    await runtime.slack.post_message(slack_id, response)
    return True
