"""Tests for shared logging setup."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from tests.conftest import recorded_run  # noqa: F401  (fixture)

from rey_lib.config.bootstrap import build_ctx_for_app

from rey_lib.logs.log_utils import (
    log_file_metadata,
    read_jsonl_file_record_page,
    read_jsonl_records,
)


def test_log_utils_public_api_includes_package_facade_exports() -> None:
    """log_utils.__all__ covers the supported rey_lib.logs facade surface."""
    import rey_lib.logs as logs
    import rey_lib.logs.log_utils as log_utils

    # Names owned by peer modules rather than log_utils itself. The governed
    # file manifest is a separate destination from the application log, so its
    # surface lives in rey_lib.logs.file_manifest, and the run-to-manifest
    # query that joins them lives in rey_lib.logs.run_file_records.
    non_log_utils_exports = {
        # The application logging object owns its own module; it is not part
        # of the log_utils facade (backlog 771).
        "AppLogger",
        "JsonlHandler",
        "run_app_operation",
        "FileManifestError",
        "FileManifestSession",
        "file_manifest_session",
        "log_file_manifest_record",
        "resolve_file_manifest_path",
        "FileHierarchyError",
        "FileHierarchyFeed",
        "FileHierarchyFile",
        "FileHierarchyMutation",
        "FileHierarchyPage",
        "FileHierarchyStage",
        "FileHierarchyStagePage",
        "FileHierarchyFeedSummary",
        "FileHierarchyFeedPage",
        "build_file_hierarchy",
        "build_file_hierarchy_feeds",
        "build_file_hierarchy_feed",
        "build_file_hierarchy_stages",
        # The profile library: a record type, its reader and the presentation
        # it resolves. Reached through rey_lib.logs, and not part of the
        # log_utils facade.
        "PROFILE_RECORD_TYPE",
        "ProfileLibraryError",
        "lookup_profile_record",
        "read_profile_records",
        "resolve_profile_presentation",
        # What a record says about its own content, and the helpers that write
        # it. Part of the record envelope rather than the log_utils facade: a
        # caller classifies a record where it builds one.
        "SENSITIVE_FIELD",
        "classified",
        "sensitive",
    }
    missing = [
        name for name in logs.__all__
        if name not in non_log_utils_exports and name not in log_utils.__all__
    ]
    assert missing == []

    for name in log_utils.__all__:
        assert hasattr(log_utils, name), name


def test_httpx_429_records_are_promoted_to_warning(run_log, recorded_run, tmp_path) -> None:
    """OpenAI/HTTPX 429 messages are warning-level in text and JSONL logs."""
    ctx = SimpleNamespace(
        logging=SimpleNamespace(run_store="jsonl"),
        env="test",
        log_level="INFO",
        log_path=str(tmp_path / "app.{operation}.{timestamp}.log"),
        jsonl_path=str(tmp_path / "app.{operation}.{timestamp}.jsonl"),
        jsonl_ctx_fields=(),
        # Opt in to the legacy readable log so the text handler is exercised too.
        readable_enabled=True,
    )
    build_ctx_for_app(ctx=ctx, operation="run")

    logger = logging.getLogger("httpx")
    logger.info(
        'HTTP Request: POST https://api.openai.com/v1/chat/completions '
        '"HTTP/1.1 429 Too Many Requests"'
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    text_log = next(tmp_path.glob("*.log")).read_text(encoding="utf-8")
    json_log = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")
    record = json.loads(json_log.strip())

    assert "WARNING" in text_log
    assert "429 Too Many Requests" in text_log
    assert record["level"] == "WARNING"
    assert "429 Too Many Requests" in record["message"]


def test_configuration_writes_jsonl_only_when_only_text_log_configured(run_log, recorded_run, tmp_path) -> None:
    """JSONL is written beside the configured log_path; no readable log is produced."""
    ctx = SimpleNamespace(
        logging=SimpleNamespace(run_store="jsonl"),
        env="test",
        log_level="INFO",
        app_name="sample_app",
        log_path=str(tmp_path / "sample.{operation}.{timestamp}.log"),
        jsonl_ctx_fields=(),
    )

    build_ctx_for_app(ctx=ctx, operation="run")
    logging.getLogger("sample").info("hello")

    for handler in logging.getLogger().handlers:
        handler.flush()

    jsonl_log = next(tmp_path.glob("*.jsonl"))

    # New runs produce only the JSONL run log, written beside the configured path.
    assert list(tmp_path.glob("*.log")) == []
    assert json.loads(jsonl_log.read_text(encoding="utf-8"))["message"] == "hello"
    assert ctx.log_file == str(jsonl_log.resolve())


def test_the_database_mode_writes_no_jsonl(
    run_log, recorded_run, tmp_path, monkeypatch,
) -> None:
    """logging.run_store: db writes the database and no JSONL file at all.

    The database output is stood in for: its rows are captured as
    DatabaseLogWriter translates them. The readable log is opted into so the
    text handler shows the event reached the pipeline.
    """
    from rey_lib.logs import log_writer

    rows: list[dict] = []
    monkeypatch.setattr(log_writer, "_run_log_connection", lambda _ctx: None)
    monkeypatch.setattr(
        log_writer.DatabaseLogWriter, "write",
        lambda self, record: rows.append(self.row(record)) or len(rows),
    )
    ctx = SimpleNamespace(
        env="test",
        log_level="INFO",
        log_path=str(tmp_path / "sample.{operation}.{timestamp}.log"),
        logging=SimpleNamespace(run_store="db", readable_enabled=True),
    )

    build_ctx_for_app(ctx=ctx, operation="run")
    logging.getLogger("sample").info("hello")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert next(tmp_path.glob("*.log")).read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.jsonl")) == []
    hello = [row for row in rows if row.get("message") == "hello"]
    assert hello and hello[0]["record_type"] == "INFO"
    assert hello[0]["info"] == {"logger": "sample"}


def test_log_file_metadata_marks_jsonl_as_authoritative(tmp_path) -> None:
    """JSONL logs are authoritative; text logs are derived views."""
    jsonl_path = tmp_path / "app.run.jsonl"
    text_path = tmp_path / "app.run.log"
    stems = {jsonl_path.with_suffix("").as_posix()}

    assert log_file_metadata(jsonl_path, stems)["authoritative"] is True
    text_meta = log_file_metadata(text_path, stems)
    assert text_meta["derived"] is True
    assert text_meta["derived_from"] == str(jsonl_path)


def test_read_jsonl_records_filters_errors(tmp_path) -> None:
    """JSONL record parsing and filtering lives in rey_lib logging utilities."""
    path = tmp_path / "app.run.jsonl"
    content = (
        '{"level": "INFO", "message": "ok"}\n'
        '{"level": "ERROR", "message": "failed"}\n'
    )

    result = read_jsonl_records(path, content, filters={"errors_only": "true"})

    assert result["authoritative"] is True
    assert result["records_matched"] == 1
    assert result["records"][0]["message"] == "failed"
    assert "ERROR" in result["rendered_text"]
    assert "failed" in result["rendered_text"]


def test_read_jsonl_records_returns_requested_page(
    tmp_path,
) -> None:
    path = tmp_path / "app.run.jsonl"
    content = "".join(
        json.dumps({"record_id": record_id}) + "\n"
        for record_id in range(1, 301)
    )

    result = read_jsonl_records(path, content, max_records=50, offset=250)

    assert result["records_matched"] == 300
    assert result["records_returned"] == 50
    assert result["offset"] == 250
    assert result["limit"] == 50
    assert result["has_more"] is False
    assert result["next_offset"] is None
    assert [record["record_id"] for record in result["records"]] == list(
        range(251, 301)
    )


def test_streamed_jsonl_page_sorts_before_slicing(tmp_path) -> None:
    path = tmp_path / "app.run.jsonl"
    path.write_text(
        "".join(
            json.dumps({"record_id": record_id, "name": f"item-{record_id:03d}"})
            + "\n"
            for record_id in range(1, 301)
        ),
        encoding="utf-8",
    )

    result = read_jsonl_file_record_page(
        path,
        max_records=25,
        offset=0,
        sort_field="record_id",
        sort_direction="desc",
    )

    assert result["records_matched"] == 300
    assert result["records_returned"] == 25
    assert result["has_more"] is True
    assert [record["record_id"] for record in result["records"]] == list(
        range(300, 275, -1)
    )
    assert result["record_line_numbers"] == list(range(300, 275, -1))


def test_streamed_jsonl_page_has_no_byte_window_limit(tmp_path) -> None:
    path = tmp_path / "large.run.jsonl"
    padding = "x" * 20_000
    path.write_text(
        "".join(
            json.dumps({"record_id": record_id, "padding": padding}) + "\n"
            for record_id in range(1, 80)
        ),
        encoding="utf-8",
    )

    result = read_jsonl_file_record_page(path, max_records=10, offset=70)

    assert path.stat().st_size > 1_000_000
    assert result["records_matched"] == 79
    assert [record["record_id"] for record in result["records"]] == list(
        range(71, 80)
    )
    assert result["truncated_file"] is False


def test_read_jsonl_records_rejects_text_logs(tmp_path) -> None:
    """Text logs are not parsed as structured records."""
    result = read_jsonl_records(tmp_path / "app.run.log", "ERROR failed\n")

    assert result["records"] == []
    assert result["authoritative"] is False
    assert "JSONL" in result["error"]


# ---------------------------------------------------------------------------
# New-contract ctx shapes (no .env, log_path as Path from PathResolver)
# ---------------------------------------------------------------------------

def test_configuration_works_without_env_attribute(run_log, recorded_run, tmp_path) -> None:
    """ctx built via build_ctx_from_path has no .env — must not raise."""
    ctx = SimpleNamespace(
        logging=SimpleNamespace(run_store="jsonl"),
        log_level="INFO",
        log_path=str(tmp_path / "app.{operation}.{timestamp}.log"),
        jsonl_ctx_fields=(),
    )
    build_ctx_for_app(ctx=ctx, operation="run")
    logging.getLogger("sample").info("no-env ctx")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")


def test_configuration_accepts_path_object_for_log_path(run_log, recorded_run, tmp_path) -> None:
    """PathResolver sets log_path as a Path; configuration must handle it."""
    from pathlib import Path

    log_template = Path(tmp_path) / "app.{operation}.{timestamp}.log"
    ctx = SimpleNamespace(
        logging=SimpleNamespace(run_store="jsonl"),
        log_level="INFO",
        log_path=log_template,
        jsonl_ctx_fields=(),
    )
    build_ctx_for_app(ctx=ctx, operation="run")
    logging.getLogger("path_test").info("path object ctx")

    for handler in logging.getLogger().handlers:
        handler.flush()

    # A Path log_path is accepted and used to place the JSONL run log; no readable
    # log is produced for new runs.
    logs = list(tmp_path.glob("*.jsonl"))
    assert len(logs) == 1
    assert "run" in logs[0].name
    assert list(tmp_path.glob("*.log")) == []


def test_configuration_substitutes_operation_and_timestamp(run_log, recorded_run, tmp_path) -> None:
    """{operation} and {timestamp} in the resolved log path are filled at runtime."""
    ctx = SimpleNamespace(
        logging=SimpleNamespace(run_store="jsonl"),
        log_level="INFO",
        log_path=str(tmp_path / "app.{operation}.{timestamp}.log"),
        jsonl_path=str(tmp_path / "app.{operation}.{timestamp}.jsonl"),
        jsonl_ctx_fields=(),
    )
    build_ctx_for_app(ctx=ctx, operation="ingest")
    logging.getLogger("sub").info("check placeholders")

    for handler in logging.getLogger().handlers:
        handler.flush()

    jsonl_files = list(tmp_path.glob("*.jsonl"))
    # No readable execution log for new runs; substitution is verified on the JSONL.
    assert list(tmp_path.glob("*.log")) == []
    assert len(jsonl_files) == 1
    assert "ingest" in jsonl_files[0].name
    assert "{operation}" not in jsonl_files[0].name
    assert "{timestamp}" not in jsonl_files[0].name
