import asyncio
from datetime import datetime

from agent.image_intelligence import ImageInsight, ImageIntelligence, build_image_manifest
from agent.models import AttachmentRecord, MessageRecord, SessionState, UserProfile


def test_image_intelligence_falls_back_without_api() -> None:
    analyzer = ImageIntelligence()
    analyzer.enabled = False
    insight = asyncio.run(
        analyzer.analyze_attachment(
            user=UserProfile(
                user_key="alex",
                display_name="Alex",
                discord_user_id=1,
                discord_username="alex",
                storage_folder_name="Alex",
            ),
            session=SessionState(user_key="alex", session_date="2026-05-28"),
            original_filename="before start bench photo.png",
            content=b"not-a-real-image",
            content_type="image/png",
            inbound_text="Here is the bench before I start.",
            recent_messages=[],
        )
    )
    assert insight.slug.startswith("before-start-bench")
    assert insight.tags


def test_build_storage_filename_includes_slug() -> None:
    analyzer = ImageIntelligence()
    filename = analyzer.build_storage_filename(
        timestamp_prefix="090501",
        index=1,
        original_filename="IMG_1234.JPG",
        insight=ImageInsight(
            slug="wiring harness closeup",
            description="A close view of a wiring harness.",
            tags=["wiring", "harness", "closeup"],
        ),
    )
    assert filename == "090501_1_wiring-harness-closeup.jpg"


def test_build_image_manifest_includes_descriptions() -> None:
    messages = [
        MessageRecord(
            message_id="1",
            direction="inbound",
            author_id=1,
            created_at=datetime(2026, 5, 28, 9, 5, 1),
            content="Here is the before image.",
            attachments=[
                AttachmentRecord(
                    filename="090501_1_wiring-harness-closeup.jpg",
                    original_filename="IMG_1234.JPG",
                    url="demo://image",
                    content_type="image/jpeg",
                    size=1234,
                    local_path="C:/tmp/image.jpg",
                    description="A close view of a wiring harness on a workbench.",
                    tags=["wiring", "harness", "workbench"],
                    analysis_model="gpt-4.1-mini",
                )
            ],
        )
    ]
    manifest = build_image_manifest(messages)
    assert manifest[0]["description"] == "A close view of a wiring harness on a workbench."
    assert manifest[0]["tags"] == ["wiring", "harness", "workbench"]
