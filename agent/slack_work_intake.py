"""Slack-native work proposals. Deliberately independent of attendance/payroll.

Original worker statements and manager decisions are append-only events. Labels
are routing suggestions, never evidence of contract allowability or authorization.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any


PROJECTS = {
    "grasp": "GRASP — NASA Phase II",
    "cisort": "CISORT — NASA Phase II",
    "cita": "CITA / TRUST — NASA Phase II",
    "bagworm": "Bagworm — Lockheed Martin",
    "clasp": "CLASP — upcoming NASA STTR Phase I; start/charging unverified",
    "shop": "Shop organization, cleaning and maintenance",
    "meetings": "Company meetings and coordination",
    "proposals": "Proposals and business development",
    "sales": "Customer discovery, transition and sales",
    "finance": "Accounting and finance",
    "people": "Hiring, onboarding and training",
    "operations": "Company administration and operations",
    "dp": "Don Pollo and company software",
    "irad": "IRAD — intentional internal research and development; approval required",
    "exploration": "Exploration: Gweike, eufyMake, BrightDrop or another proposed idea",
}
_BOUNDARY = (
    "Your work description does not start or stop your clock. Use `clock in onsite`, `clock out`, or `hours` here in Slack. "
    "Gusto Kiosk is not the beta clock; Slack Erik actual hours if DP is unavailable."
)
_HELP = (
    "Tell me the project and what you intend to change, for example:\n"
    "`work GRASP: compare wheel-slip runs and save a plot for George`\n"
    "Then use `work detail <why / next result / rough estimate>`, "
    "`work update <what changed or what is blocked>`, or `work status`.\n"
    "Projects: 1 GRASP · 2 CISORT · 3 CITA · 4 Bagworm · 5 CLASP. "
    "Also: shop, meetings, proposals, sales, finance, people, operations, dp, irad, exploration.\n"
    "`work project <name or 1–5>` labels the current proposal. "
    "`work options` shows up to five of your previously approved plans.\n" + _BOUNDARY
)


def _safe(text: Any) -> str:
    """Prevent user-authored Slack mentions/links from becoming bot instructions."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def project_candidates(text: str) -> list[str]:
    return [key for key in PROJECTS if re.search(rf"\b{key}\b", text, re.I)]


def follow_up(text: str, previous: str = "") -> str:
    normalized = re.sub(r"\W+", " ", text.lower()).strip()
    prior = re.sub(r"\W+", " ", previous.lower()).strip()
    if normalized and normalized == prior:
        return (
            "The wording is unchanged. Repeating a real activity is fine: what changed, "
            "what did you try, or what is blocking the next step? One specific fact helps us help you."
        )
    if len(normalized.split()) < 5 or re.fullmatch(
        r"(?:still )?(?:working|worked|work)(?: on)? (?:it|project|the project|same thing)", normalized
    ):
        return "What specific thing will you change or learn next, and why is it needed? A short concrete answer is enough."
    return "What result should we expect next, and roughly how long will it take? If blocked, say what you need."


class SlackWorkIntake:
    def __init__(self, state_store: Any) -> None:
        self.store = state_store
        with self.store._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS work_intake_items (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_name TEXT NOT NULL,
                    project_key TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_intake_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES work_intake_items(id)
                );
                CREATE TABLE IF NOT EXISTS work_intake_receipts (
                    event_key TEXT PRIMARY KEY, response TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_intake_actor_activity
                    ON work_intake_events(actor_id, created_at);
                CREATE TABLE IF NOT EXISTS work_evidence (
                    item_id TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
                    source_id TEXT NOT NULL, actor_id TEXT NOT NULL, captured_at TEXT NOT NULL,
                    retrieval_status TEXT NOT NULL DEFAULT 'not_verified',
                    PRIMARY KEY(item_id,url)
                );
                CREATE TABLE IF NOT EXISTS work_decision_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
                    text TEXT NOT NULL, delivered_at TEXT
                );
            """)

    def handle(self, *, actor_id: str, actor_name: str, is_manager: bool,
               text: str, event_id: str, now: datetime | None = None) -> str:
        if not event_id or not actor_id:
            raise ValueError("A stable Slack event id and authenticated actor are required.")
        moment = (now or datetime.now(timezone.utc)).isoformat()
        key = f"{actor_id}:{event_id}"
        with self.store._connect() as conn:
            # Slack retries and simultaneous manager/worker edits cannot duplicate
            # proposals or approve a version different from the one inspected.
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute("SELECT response FROM work_intake_receipts WHERE event_key=?", (key,)).fetchone()
            if receipt:
                return str(receipt["response"])
            response = self._dispatch(conn, actor_id, actor_name, is_manager, text, key, moment)
            conn.execute("INSERT INTO work_intake_receipts VALUES (?, ?)", (key, response))
            return response

    def _dispatch(self, conn: Any, actor: str, name: str, manager: bool,
                  text: str, key: str, now: str) -> str:
        body = re.sub(r"^work(?:\s+|$)", "", text.strip(), count=1, flags=re.I).strip()
        command, _, content = body.partition(" ")
        command = command.lower()
        content = content.strip()
        if not body or command == "help":
            from .work_evidence import COMPANY_HANDOFF
            return _HELP + "\n`work evidence` lists source links; `work handoff` prepares a reusable summary; `work edit <correction>` preserves the original.\n" + COMPANY_HANDOFF
        if len(body) > 6000:
            return "Please keep this note under 6,000 characters and link longer evidence. Nothing was replaced."
        if command in {"queue", "review", "approve", "redirect"}:
            if not manager:
                return "Only configured managers can review or decide other people's work. Your records are unchanged."
            return self._manager(conn, actor, command, content, now)
        item = conn.execute(
            "SELECT * FROM work_intake_items WHERE owner_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (actor,),
        ).fetchone()
        if command == "options":
            rows = conn.execute(
                "SELECT * FROM work_intake_items WHERE owner_id=? AND status='approved' ORDER BY updated_at DESC LIMIT 5",
                (actor,),
            ).fetchall()
            if not rows:
                return "No approved options yet. Describe your intended work with `work <project>: <plan>`; we will preserve it for alignment review."
            return "Your previously approved plans (reference only; approval does not authorize overtime or remote work):\n" + "\n".join(
                f"{i}. {_safe(PROJECTS.get(row['project_key'], 'Project unconfirmed'))} — `{row['id']}`: "
                + _safe(self._events(conn, row['id'])[0]['text'][:180]) for i, row in enumerate(rows, 1)
            )
        if command in {"status", "detail", "update", "project", "edit", "evidence", "handoff"}:
            if not item:
                return "No proposal yet. " + _HELP
            if command == "status":
                return self._describe(conn, item) + "\n" + _BOUNDARY
            if command in {"evidence", "handoff"}:
                from .work_evidence import COMPANY_HANDOFF
                rows = conn.execute("SELECT source,url,retrieval_status FROM work_evidence WHERE item_id=? ORDER BY captured_at LIMIT 5", (item["id"],)).fetchall()
                links = "\n".join(f"{row['source']}: {_safe(row['url'])} — access/content not verified" for row in rows)
                return ((self._describe(conn, item) + "\n") if command == "handoff" else "") + (links or "No source links recorded for this work yet.") + "\n" + COMPANY_HANDOFF
            if not content:
                return f"Add your actual note after `work {command}`. Nothing was replaced."
            project = item["project_key"]
            if command == "project":
                project = dict(zip("12345", list(PROJECTS)[:5])).get(content, content.lower())
                if project not in PROJECTS:
                    return "Use a project name or 1–5 from `work help`. Your original note is still saved."
            previous_events = self._events(conn, item["id"])
            previous = next((row["text"] for row in reversed(previous_events) if row["actor_id"] == actor), "")
            self._append(conn, item["id"], actor, command, content, now)
            # A changed plan or label invalidates approval; a progress update
            # remains an observation, not authorization for new scope.
            status = "pending" if command in {"detail", "project", "edit"} else item["status"]
            conn.execute("UPDATE work_intake_items SET project_key=?, status=?, revision=revision+1, updated_at=? WHERE id=?",
                         (project, status, now, item["id"]))
            question = follow_up(content, previous)
            if command == "project":
                question = "Project label saved for review; this does not decide which contract may be charged."
            elif len(content.split()) >= 5 and "wording is unchanged" not in question:
                question = "Update me when the result changes, you need help, or you want to change direction."
            return f"Saved to `{item['id']}`; original wording preserved.\n{question}"
        project_matches = project_candidates(body)
        project = project_matches[0] if len(project_matches) == 1 else ""
        item_id = "DP-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        conn.execute("INSERT INTO work_intake_items VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)",
                     (item_id, actor, name, project, now, now))
        self._append(conn, item_id, actor, "proposal", body, now)
        question = follow_up(body) if project else "Which project or overhead area is this for? Use `work project <name or 1–5>`; your description is saved."
        label = PROJECTS.get(project, "Project needs confirmation")
        return f"Saved `{item_id}` under {_safe(label)} for alignment review.\n{question}\n{_BOUNDARY}"

    @staticmethod
    def _append(conn: Any, item: str, actor: str, kind: str, text: str, now: str) -> None:
        conn.execute("INSERT INTO work_intake_events(item_id,actor_id,kind,text,created_at) VALUES (?,?,?,?,?)",
                     (item, actor, kind, text, now))
        if kind in {"proposal", "detail", "update", "edit"}:
            from .work_evidence import source_references
            for ref in source_references(text):
                conn.execute("INSERT OR IGNORE INTO work_evidence(item_id,source,url,source_id,actor_id,captured_at,retrieval_status) VALUES (?,?,?,?,?,?,?)",
                             (item, ref["source"], ref["url"], ref["source_id"], actor, now, ref["retrieval_status"]))

    @staticmethod
    def _events(conn: Any, item: str) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute("SELECT * FROM work_intake_events WHERE item_id=? ORDER BY id", (item,))]

    def _describe(self, conn: Any, item: Any) -> str:
        events = self._events(conn, item["id"])
        text = "\n".join(f"{row['kind']}: {_safe(row['text'][:700])}" for row in events[-5:])
        return (f"`{item['id']}` revision {item['revision']} — {item['status']}\n"
                f"{_safe(item['owner_name'])} · {_safe(PROJECTS.get(item['project_key'], 'Project unconfirmed'))}\n{text}")

    def _manager(self, conn: Any, actor: str, command: str, content: str, now: str) -> str:
        if command == "queue":
            rows = conn.execute("SELECT * FROM work_intake_items WHERE status='pending' ORDER BY created_at LIMIT 5").fetchall()
            count = conn.execute("SELECT COUNT(*) FROM work_intake_items WHERE status='pending'").fetchone()[0]
            return f"{count} work proposals need alignment review; showing up to five.\n" + "\n\n".join(
                self._describe(conn, row) for row in rows
            ) + "\nUse `work review DP-id`, then `work approve DP-id revision reason` or `work redirect DP-id revision next step`. These decisions do not alter pay, time, or contract charging."
        parts = content.split(maxsplit=2)
        item_id = "DP-" + parts[0][3:].lower() if parts and parts[0].lower().startswith("dp-") else ""
        item = conn.execute("SELECT * FROM work_intake_items WHERE id=?", (item_id,)).fetchone()
        if not item:
            return "Proposal not found. Use `work queue` for current IDs."
        if command == "review":
            return self._describe(conn, item) + "\nCheck the controlling SOW, accepted changes and latest evidence before approval; a project label alone is not alignment evidence."
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
            return f"Use `work {command} {item_id} {item['revision']} <reason or next step>`."
        if int(parts[1]) != item["revision"]:
            return "This proposal changed since you reviewed it. Use `work review " + item_id + "` before deciding."
        if command == "approve" and not item["project_key"]:
            return "Confirm the contract, intentional IRAD or overhead destination with the worker before approval."
        status = "approved" if command == "approve" else "redirected"
        self._append(conn, item_id, actor, status, parts[2], now)
        conn.execute("UPDATE work_intake_items SET status=?, revision=revision+1, updated_at=? WHERE id=?", (status, now, item_id))
        conn.execute("INSERT INTO work_decision_notices(owner_id,text) VALUES (?,?)",
                     (item["owner_id"], f"Work alignment update for `{item_id}`: {status}.\n{_safe(parts[2])}\nThis decision does not change recorded hours or authorize overtime, remote work or contract charging."))
        return f"Recorded {status} for `{item_id}`. A worker notification is queued; your note is also in `work status`. No time or payroll change was made."

    def pending_notices(self) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM work_decision_notices WHERE delivered_at IS NULL ORDER BY id LIMIT 50")]

    def notice_delivered(self, notice_id: int) -> None:
        with self.store._connect() as conn:
            conn.execute("UPDATE work_decision_notices SET delivered_at=? WHERE id=?",
                         (datetime.now(timezone.utc).isoformat(), notice_id))

    def pending_exceptions(self) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute("SELECT * FROM work_intake_items WHERE status='pending' ORDER BY created_at LIMIT 250").fetchall()
            return [{
                "id": row["id"], "severity": "info", "category": "work_alignment",
                "person": row["owner_name"], "user_key": "", "session_date": "",
                "summary": "Work description needs alignment review",
                "source": "work_intake", "last_seen_at": row["updated_at"], "occurrence_count": 1,
                "details": {"project": PROJECTS.get(row["project_key"], "Unconfirmed"),
                            "revision": row["revision"], "events": self._events(conn, row["id"]),
                            "recommended_action": f"DM Don Pollo: work review {row['id']}. Confirm purpose and next result against project sources, then approve or redirect with a reason."},
            } for row in rows]

    def latest_activity(self, actor_id: str) -> str | None:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) FROM work_intake_events WHERE actor_id=? "
            "AND kind IN ('proposal','detail','update','project','edit')", (actor_id,)
            ).fetchone()
            return row[0] if row else None

    def has_item(self, actor_id: str) -> bool:
        with self.store._connect() as conn:
            return conn.execute("SELECT 1 FROM work_intake_items WHERE owner_id=? LIMIT 1", (actor_id,)).fetchone() is not None

    def attach_ai_draft(self, item_id: str, actor_id: str, result: dict[str, Any]) -> None:
        text = json.dumps(result, sort_keys=True)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owned = conn.execute("SELECT 1 FROM work_intake_items WHERE id=? AND owner_id=?", (item_id, actor_id)).fetchone()
            duplicate = conn.execute("SELECT 1 FROM work_intake_events WHERE item_id=? AND kind='ai_draft' AND text=?", (item_id, text)).fetchone()
            if owned and not duplicate:
                self._append(conn, item_id, "openai_draft_not_approval", "ai_draft", text, datetime.now(timezone.utc).isoformat())
