import asyncio
from types import SimpleNamespace

from agent.models import AdminProfile, UserProfile
from agent.workforce_mapping import build_workforce_mapping


def test_workforce_mapping_preserves_roster_and_separates_identity_from_worker_type() -> None:
    intern = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        slack_user_id="UINTERN",
        clickup_user_id="10",
    )

    class Slack:
        async def list_users(self):
            return [
                {
                    "id": "UINTERN",
                    "profile": {"real_name": "Alex Intern", "email": "other@example.com"},
                },
                {
                    "id": "UADMIN",
                    "profile": {"real_name": "Pat Admin", "email": "pat@example.com"},
                },
                {
                    "id": "UVENDOR",
                    "profile": {
                        "real_name": "Acme Engineering",
                        "email": "vendor@example.com",
                    },
                },
            ]

    class ClickUp:
        async def list_workspace_members(self):
            return [
                {"id": 10, "username": "Alex Intern", "email": "alex@example.com"},
                {"id": 20, "username": "Pat Admin", "email": "pat@example.com"},
                {"id": 30, "username": "Acme Engineering", "email": "vendor@example.com"},
            ]

    runtime = SimpleNamespace(
        slack=Slack(),
        clickup=ClickUp(),
        roster_by_key={"alex": intern},
        config=SimpleNamespace(
            admins=[AdminProfile(discord_user_id=2, name="Pat", clickup_user_id="20")]
        ),
        _storage_root_path=lambda: None,
    )

    rows = asyncio.run(build_workforce_mapping(runtime))
    by_slack = {row["slack_user_id"]: row for row in rows}
    assert by_slack["UINTERN"]["identity_match"] == "confirmed_roster"
    assert by_slack["UINTERN"]["worker_type"] == "intern"
    assert by_slack["UINTERN"]["needs_confirmation"] is False
    assert by_slack["UADMIN"]["identity_confidence"] == "high"
    assert by_slack["UADMIN"]["worker_type"] == "employee"
    assert by_slack["UADMIN"]["worker_type_confidence"] == "medium_admin_inference"
    assert by_slack["UVENDOR"]["worker_type"] == "contractor"
