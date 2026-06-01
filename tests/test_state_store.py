from datetime import datetime
from pathlib import Path

from agent.models import MessageRecord
from agent.state_store import StateStore


def test_append_message_ignores_exact_duplicates(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    message = MessageRecord(
        message_id="123",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T10:00:00+00:00"),
        content="hello",
        attachments=[],
    )
    inserted_first = store.append_message("user", "2026-05-28", message)
    inserted_second = store.append_message("user", "2026-05-28", message)
    saved = store.list_messages("user", "2026-05-28")
    assert inserted_first is True
    assert inserted_second is False
    assert len(saved) == 1
