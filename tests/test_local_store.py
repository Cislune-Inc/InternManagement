from pathlib import Path

from agent.local_store import LocalStore
from agent.models import BootstrapConfig, UserProfile


def test_local_store_creates_workspace(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "agent.config.json").write_text(
        '{"admin_discord_user_id":"1","clickup":{"workspace_id":"2"},"prompts":{}}',
        encoding="utf-8",
    )
    store = LocalStore(
        BootstrapConfig(
            agent_config_path=config_dir / "agent.config.json",
            storage_root_path=tmp_path / "storage",
            state_db_path=tmp_path / "data.sqlite3",
            default_timezone="America/Los_Angeles",
        )
    )
    workspace = store._ensure_user_workspace_sync(
        UserProfile(
            user_key="alex",
            display_name="Alex",
            discord_user_id=123,
            discord_username="alex",
            storage_folder_name="Alex / Example",
        ),
        "2026-05-27",
    )
    assert workspace.images_dir.exists()
    assert workspace.user_dir.name == "Alex _ Example"
    assert (workspace.user_dir / "profile.json").exists()
