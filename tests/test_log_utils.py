"""Tests for shared logging setup."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from rey_lib.logs.log_utils import log_file_metadata, read_jsonl_records, setup_logging


def test_httpx_429_records_are_promoted_to_warning(tmp_path) -> None:
    """OpenAI/HTTPX 429 messages are warning-level in text and JSONL logs."""
    ctx = SimpleNamespace(
        env="test",
        log_level="INFO",
        log_path=str(tmp_path / "app.{operation}.{timestamp}.log"),
        jsonl_path=str(tmp_path / "app.{operation}.{timestamp}.jsonl"),
        jsonl_ctx_fields=(),
    )
    setup_logging(ctx, operation="run")

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


def test_setup_logging_writes_jsonl_by_default_when_only_text_log_configured(tmp_path) -> None:
    """JSONL is the authoritative default even when YAML only names log_path."""
    ctx = SimpleNamespace(
        env="test",
        log_level="INFO",
        app_name="sample_app",
        log_path=str(tmp_path / "sample.{operation}.{timestamp}.log"),
        jsonl_ctx_fields=(),
    )

    setup_logging(ctx, operation="run")
    logging.getLogger("sample").info("hello")

    for handler in logging.getLogger().handlers:
        handler.flush()

    text_log = next(tmp_path.glob("*.log"))
    jsonl_log = next(tmp_path.glob("*.jsonl"))

    assert text_log.read_text(encoding="utf-8")
    assert json.loads(jsonl_log.read_text(encoding="utf-8"))["message"] == "hello"
    assert ctx.log_file == str(jsonl_log.resolve())


def test_setup_logging_can_disable_jsonl_with_yaml_flag(tmp_path) -> None:
    """A YAML logging flag can opt out of JSONL for the rare readable-only case."""
    ctx = SimpleNamespace(
        env="test",
        log_level="INFO",
        log_path=str(tmp_path / "sample.{operation}.{timestamp}.log"),
        logging=SimpleNamespace(jsonl_enabled=False),
    )

    setup_logging(ctx, operation="run")
    logging.getLogger("sample").info("hello")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert next(tmp_path.glob("*.log")).read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.jsonl")) == []


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


def test_read_jsonl_records_rejects_text_logs(tmp_path) -> None:
    """Text logs are not parsed as structured records."""
    result = read_jsonl_records(tmp_path / "app.run.log", "ERROR failed\n")

    assert result["records"] == []
    assert result["authoritative"] is False
    assert "JSONL" in result["error"]


# ---------------------------------------------------------------------------
# New-contract ctx shapes (no .env, log_path as Path from PathResolver)
# ---------------------------------------------------------------------------

def test_setup_logging_works_without_env_attribute(tmp_path) -> None:
    """ctx built via build_ctx_from_path has no .env — must not raise."""
    ctx = SimpleNamespace(
        log_level="INFO",
        log_path=str(tmp_path / "app.{operation}.{timestamp}.log"),
        jsonl_ctx_fields=(),
    )
    setup_logging(ctx, operation="run")
    logging.getLogger("sample").info("no-env ctx")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")


def test_setup_logging_defaults_to_info_without_env(tmp_path) -> None:
    """When neither .env nor .log_level is set, INFO is the fallback."""
    ctx = SimpleNamespace(
        log_path=str(tmp_path / "app.{operation}.{timestamp}.log"),
        jsonl_ctx_fields=(),
    )
    setup_logging(ctx, operation="run")
    assert ctx.log_level == "INFO"


def test_setup_logging_accepts_path_object_for_log_path(tmp_path) -> None:
    """PathResolver sets log_path as a Path; setup_logging must handle it."""
    from pathlib import Path

    log_template = Path(tmp_path) / "app.{operation}.{timestamp}.log"
    ctx = SimpleNamespace(
        log_level="INFO",
        log_path=log_template,
        jsonl_ctx_fields=(),
    )
    setup_logging(ctx, operation="run")
    logging.getLogger("path_test").info("path object ctx")

    for handler in logging.getLogger().handlers:
        handler.flush()

    logs = list(tmp_path.glob("*.log"))
    assert len(logs) == 1
    assert "run" in logs[0].name


def test_setup_logging_substitutes_operation_and_timestamp(tmp_path) -> None:
    """{operation} and {timestamp} in the resolved log path are filled at runtime."""
    ctx = SimpleNamespace(
        log_level="INFO",
        log_path=str(tmp_path / "app.{operation}.{timestamp}.log"),
        jsonl_path=str(tmp_path / "app.{operation}.{timestamp}.jsonl"),
        jsonl_ctx_fields=(),
    )
    setup_logging(ctx, operation="ingest")
    logging.getLogger("sub").info("check placeholders")

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_files = list(tmp_path.glob("*.log"))
    jsonl_files = list(tmp_path.glob("*.jsonl"))
    assert len(log_files) == 1
    assert "ingest" in log_files[0].name
    assert "{operation}" not in log_files[0].name
    assert "{timestamp}" not in log_files[0].name
    assert len(jsonl_files) == 1
    assert "ingest" in jsonl_files[0].name
