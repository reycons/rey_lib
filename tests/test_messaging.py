"""Tests for rey_lib.messaging lifecycle behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.config.config_utils import Namespace
from rey_lib.messaging import approve_message, create_message, send_message, send_pipeline_summary
from rey_lib.messaging.errors import MessageApprovalError, MessageValidationError
from rey_lib.messaging.models import Attachment, MessageContent, MessageRequest
from rey_lib.messaging.repository import FileMessageRepository


def _ctx(tmp_path: Path) -> Namespace:
    return Namespace(
        {
            "messaging": {
                "archive_path": tmp_path / "messages" / "archive.jsonl",
                "approvals": {
                    "required_audiences": ["external"],
                },
                "recipient_groups": [
                    {
                        "name": "internal_ops",
                        "description": "Internal operations notifications.",
                        "email": {
                            "to": ["ops@example.com", "support@example.com"],
                            "cc": ["manager@example.com"],
                            "bcc": ["audit@example.com"],
                            "reply_to": "reply@example.com",
                        },
                        "phone": {"to": []},
                        "slack": {"channels": [], "users": []},
                    }
                ],
                "providers": {
                    "email": {
                        "provider": "smtp",
                    }
                },
                "delivery": {"dry_run": True},
                "pipeline_summary": {
                    "audience": "internal",
                    "record_limit": 500,
                    "subject": "Pipeline $pipeline_name: $pipeline_status",
                    "body": "# Pipeline $pipeline_name\n\n$overall_summary\n\n## Errors\n$error_summary\n",
                },
            }
        }
    )


def test_email_dry_run_lifecycle_writes_archive(tmp_path: Path) -> None:
    """A dry-run email follows generation, validation, rendering, and audit persistence."""
    ctx = _ctx(tmp_path)

    message = create_message(
        ctx,
        message_type="etl_failure_summary",
        audience="internal",
        channel="email",
        recipients=["ops@example.com"],
        subject="Failure: $pipeline",
        markdown="# Failed\nPipeline $pipeline failed.",
        data={"pipeline": "daily"},
        dry_run=True,
    )
    result = send_message(ctx, message)

    assert result.status == "sent"
    assert result.dry_run is True
    assert message.status == "sent"
    assert message.content.subject == "Failure: daily"
    assert "<h1>Failed</h1>" in message.content.body_html

    records = list(FileMessageRepository(tmp_path / "messages" / "archive.jsonl").records())
    assert any(record["kind"] == "event" and record["event_type"] == "message_sent" for record in records)
    assert any(record["kind"] == "message" and record["message_id"] == message.message_id for record in records)


def test_email_recipient_group_resolves_channel_shape(tmp_path: Path) -> None:
    """Recipient groups use the channel-shaped YAML structure."""
    message = create_message(
        _ctx(tmp_path),
        message_type="pipeline_summary",
        audience="internal",
        channel="email",
        recipient_group="internal_ops",
        subject="Daily",
        body="Complete",
        dry_run=True,
    )

    assert message.request.recipients == ["ops@example.com", "support@example.com"]
    assert message.request.cc == ["manager@example.com"]
    assert message.request.bcc == ["audit@example.com"]
    assert message.request.reply_to == "reply@example.com"


def test_email_recipient_group_allows_explicit_empty_overrides(tmp_path: Path) -> None:
    """Callers may intentionally clear optional channel fields."""
    message = create_message(
        _ctx(tmp_path),
        message_type="pipeline_summary",
        audience="internal",
        channel="email",
        recipient_group="internal_ops",
        recipients=["direct@example.com"],
        cc=[],
        bcc=[],
        reply_to="",
        subject="Daily",
        body="Complete",
        dry_run=True,
    )

    assert message.request.recipients == ["direct@example.com"]
    assert message.request.cc == []
    assert message.request.bcc == []
    assert message.request.reply_to == "reply@example.com"


def test_pipeline_summary_uses_log_and_recipient_group(tmp_path: Path) -> None:
    """Pipeline summary reads JSONL log content and delegates delivery to messaging."""
    log_file = tmp_path / "pipeline.jsonl"
    log_file.write_text(
        "\n".join(
            [
                json.dumps({"level": "WARNING", "message": "source delayed", "pipeline_step_name": "sync"}),
                json.dumps(
                    {
                        "level": "INFO",
                        "event_type": "pipeline_step_completed",
                        "status": "failed",
                        "message": "load failed",
                        "pipeline_step_name": "load",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    result = send_pipeline_summary(
        _ctx(tmp_path),
        log_file=log_file,
        pipeline_name="daily",
        pipeline_status="failed",
        run_id="run-1",
        recipient_group="internal_ops",
    )

    assert result["delivery_status"] == "sent"
    assert result["dry_run"] is True
    assert result["warning_count"] == "1"
    assert result["error_count"] == "1"


def test_external_audience_requires_approval_before_send(tmp_path: Path) -> None:
    """Approval policy blocks delivery until a deterministic approval is recorded."""
    ctx = _ctx(tmp_path)

    message = create_message(
        ctx,
        message_type="client_summary",
        audience="external",
        channel="email",
        recipients=["client@example.com"],
        subject="Summary",
        body="Ready",
        dry_run=True,
    )

    assert message.status == "approval_required"
    with pytest.raises(MessageApprovalError):
        send_message(ctx, message)

    approve_message(ctx, message, reviewer="ops")
    result = send_message(ctx, message)

    assert result.status == "sent"
    assert message.approval.status == "approved"


def test_validation_rejects_missing_body(tmp_path: Path) -> None:
    """Validation failures prevent delivery."""
    with pytest.raises(MessageValidationError):
        create_message(
            _ctx(tmp_path),
            message_type="empty",
            audience="internal",
            channel="email",
            recipients=["ops@example.com"],
            subject="Empty",
            dry_run=True,
        )


def test_llm_drafter_is_constrained_to_content_only(tmp_path: Path) -> None:
    """LLM drafting can provide content but routing remains request controlled."""
    ctx = _ctx(tmp_path)

    def drafter(_request: MessageRequest) -> MessageContent:
        return MessageContent(subject="Drafted", body_text="Generated body")

    message = create_message(
        ctx,
        message_type="llm_summary",
        audience="internal",
        channel="email",
        recipients=["ops@example.com"],
        generation_mode="llm",
        dry_run=True,
        llm_drafter=drafter,
    )

    assert message.request.recipients == ["ops@example.com"]
    assert message.content.subject == "Drafted"


def test_attachment_validation_uses_metadata_only(tmp_path: Path) -> None:
    """Attachment validation checks existence before dry-run delivery."""
    attachment = tmp_path / "report.txt"
    attachment.write_text("ok\n", encoding="utf-8")

    message = create_message(
        _ctx(tmp_path),
        message_type="report",
        audience="internal",
        channel="email",
        recipients=["ops@example.com"],
        subject="Report",
        body="Attached",
        attachments=[Attachment(attachment)],
        dry_run=True,
    )

    assert message.request.attachments[0].path == attachment
