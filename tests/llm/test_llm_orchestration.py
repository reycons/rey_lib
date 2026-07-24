"""
Tests for the LLM orchestration framework.

Covers:
- Approval semantics: run → pending_approval → approve → resume
- PipelineLock acquire/release/conflict
- PatternRedactor
- LocalArtifactStore
- cancel() / approve() / reject()
- from_csv truncation
- run_batch
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from rey_lib.llm.api import RunRequest, RunResponse
from rey_lib.llm.artifacts import ArtifactStore, LocalArtifactStore
from rey_lib.llm.document_loader import from_csv
from rey_lib.llm.exceptions import LockConflict, RateLimitFailure, TimeoutFailure
from rey_lib.llm.retry import RetryPolicy
from rey_lib.llm.locking import PipelineLock
from rey_lib.llm.pipeline import Pipeline, PipelineHooks, Stage
from rey_lib.llm.records import (
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    ApprovalRecord,
    ExecutionRecord,
    approve,
    cancel,
    load_all_records,
    reject,
    store_record,
)
from rey_lib.llm.envelope import extract_artifact_envelope, loads_llm_json
from rey_lib.llm.redaction import NoopRedactor, PatternRedactor
from rey_lib.llm.runner import (  # type: ignore[attr-defined]
    _ProviderConfig,
    _attempt_parse,
    _normalize_result_text,
    _normalized_result_value,
    run,
    run_batch,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _make_record(
    run_id:     str = "test-run-1",
    pipeline_id: str = "pipe1",
    stage_id:   str = "stage1",
    status:     str = STATUS_PENDING_APPROVAL,
    parsed:     Optional[dict[str, Any]] = None,
) -> ExecutionRecord:
    """Build a minimal ExecutionRecord for testing."""
    return ExecutionRecord(
        run_id           = run_id,
        pipeline_id      = pipeline_id,
        stage_id         = stage_id,
        contract_name    = "test-contract",
        contract_version = "1.0",
        contract_hash    = "abc123",
        status           = status,
        parsed_response  = parsed or {"result": "ok"},
    )


def _make_mock_provider(content: str = '{"result": "ok"}') -> MagicMock:
    """Return a mock BaseProvider whose run() returns the given JSON string."""
    from rey_lib.llm.providers.base import ProviderCapabilities, ProviderResponse

    caps = ProviderCapabilities(
        supports_tools           = False,
        supports_images          = False,
        supports_json_mode       = True,
        supports_streaming       = False,
        supports_system_messages = True,
    )
    response = ProviderResponse(
        content    = content,
        tokens_in  = 10,
        tokens_out = 10,
        model      = "mock-model",
        raw        = {},
    )
    provider = MagicMock()
    provider.capabilities = caps
    provider.run.return_value = response
    return provider


def _write_contract(tmp_path: Path, body: str = "Do the thing.") -> Path:
    """Write a minimal contract markdown file and return its path."""
    contract = tmp_path / "test.md"
    contract.write_text(
        "---\n"
        "name: test-contract\n"
        "version: 1.0\n"
        "effective_date: 2025-01-01\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return contract


# ---------------------------------------------------------------------------
# records — approve / reject / cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"result":{"answer":42},"status":"ok"}',
        '"{\\"result\\":{\\"answer\\":42},\\"status\\":\\"ok\\"}"',
    ],
)
def test_shared_result_parser_normalizes_direct_and_once_encoded_objects(raw: str) -> None:
    parsed, error = _attempt_parse(_normalize_result_text(raw))

    assert error is None
    assert parsed == {"result": {"answer": 42}, "status": "ok"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"# Heading\\n\\nBody with \\"quotes\\"."', '# Heading\n\nBody with "quotes".'),
        ('"Plain text\\nsecond line"', "Plain text\nsecond line"),
        ('{"result":"already structured"}', '{"result":"already structured"}'),
        ('"\\"legitimately quoted\\""', '"legitimately quoted"'),
    ],
)
def test_shared_result_normalizer_unwraps_only_one_outer_string_layer(
    raw: str,
    expected: str,
) -> None:
    assert _normalize_result_text(raw) == expected


def test_shared_result_normalizer_recovers_outer_string_with_markdown_escape() -> None:
    content_value = json.dumps(
        {"subject": "Result", "markdown": "**execution_status**"}
    )
    envelope = json.dumps(
        {"artifact_type": "json", "content": content_value, "notes": []}
    )
    raw = json.dumps(envelope).replace("execution_status", r"execution\_status")

    normalized = _normalize_result_text(raw)
    content, notes = extract_artifact_envelope(normalized, "json")

    assert json.loads(content) == {
        "subject": "Result",
        "markdown": "**execution_status**",
    }
    assert notes == []


def test_shared_json_loader_accepts_encoded_content_with_markdown_escape() -> None:
    raw = (
        '{"artifact_type":"json","content":'
        '"{\\n  \\"subject\\": \\"Result\\",\\n  \\"markdown\\": '
        '\\"**execution\\\\_status**: failed\\"\\n}","notes":[]}'
    )

    envelope = loads_llm_json(raw)
    content = loads_llm_json(envelope["content"])

    assert content == {
        "subject": "Result",
        "markdown": r"**execution\_status**: failed",
    }


def test_shared_json_loader_completes_only_truncated_final_string_and_object() -> None:
    raw = (
        '{"subject":"Result","markdown":"# Failed\\n\\n'
        '**execution_status**: failed.'
    )

    assert loads_llm_json(raw) == {
        "subject": "Result",
        "markdown": "# Failed\n\n**execution_status**: failed.",
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"result":{"answer":42},"status":"ok"}',
        '"{\\"result\\":{\\"answer\\":42},\\"status\\":\\"ok\\"}"',
    ],
)
def test_shared_result_value_normalizes_direct_and_once_encoded_objects(
    raw: str,
) -> None:
    normalized_text = _normalize_result_text(raw)

    assert _normalized_result_value(
        None, normalized_text, successful=True
    ) == {"result": {"answer": 42}, "status": "ok"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"# Heading\\n\\nBody"', "# Heading\n\nBody"),
        ('"Plain text\\nsecond line"', "Plain text\nsecond line"),
        ('"\\"legitimately quoted\\""', '"legitimately quoted"'),
    ],
)
def test_shared_result_value_preserves_normalized_text_content(
    raw: str,
    expected: str,
) -> None:
    normalized_text = _normalize_result_text(raw)

    assert _normalized_result_value(None, normalized_text, successful=True) == expected

class TestApprove:
    """Tests for records.approve()."""

    def test_status_changes_to_approved(self) -> None:
        """Approving a pending_approval record sets status to approved."""
        record = _make_record(status=STATUS_PENDING_APPROVAL)
        updated, approval = approve(record, reviewer="alice", comments="looks good")

        assert updated.status == STATUS_APPROVED
        assert updated.approved_by == "alice"
        assert updated.approved_at != ""

    def test_approval_record_fields(self) -> None:
        """ApprovalRecord captures decision, reviewer, and previous status."""
        record = _make_record(status=STATUS_PENDING_APPROVAL)
        _, approval = approve(record, reviewer="bob")

        assert isinstance(approval, ApprovalRecord)
        assert approval.decision == "approved"
        assert approval.reviewer == "bob"
        assert approval.previous_status == STATUS_PENDING_APPROVAL
        assert approval.new_status == STATUS_APPROVED
        assert approval.run_id == record.run_id

    def test_original_record_unchanged(self) -> None:
        """approve() is non-destructive — original record is immutable."""
        record = _make_record(status=STATUS_PENDING_APPROVAL)
        approve(record, reviewer="alice")
        assert record.status == STATUS_PENDING_APPROVAL


class TestReject:
    """Tests for records.reject()."""

    def test_status_changes_to_rejected(self) -> None:
        """Rejecting a pending_approval record sets status to rejected."""
        record = _make_record(status=STATUS_PENDING_APPROVAL)
        updated, approval = reject(record, reviewer="charlie", comments="wrong output")

        assert updated.status == STATUS_REJECTED
        assert approval.decision == "rejected"
        assert approval.previous_status == STATUS_PENDING_APPROVAL
        assert approval.new_status == STATUS_REJECTED


class TestCancel:
    """Tests for records.cancel()."""

    def test_status_changes_to_cancelled(self) -> None:
        """cancel() sets status to cancelled."""
        record = _make_record(status=STATUS_PENDING_APPROVAL)
        updated = cancel(record, reason="no longer needed")

        assert updated.status == STATUS_CANCELLED

    def test_original_record_unchanged(self) -> None:
        """cancel() is non-destructive."""
        record = _make_record(status=STATUS_PENDING_APPROVAL)
        cancel(record)
        assert record.status == STATUS_PENDING_APPROVAL


# ---------------------------------------------------------------------------
# redaction — PatternRedactor / NoopRedactor
# ---------------------------------------------------------------------------

class TestPatternRedactor:
    """Tests for PatternRedactor."""

    def test_single_pattern_replaced(self) -> None:
        """A matching pattern is replaced with the mask."""
        redactor = PatternRedactor(
            patterns=[re.compile(r"\d{3}-\d{2}-\d{4}")],
            mask="[SSN]",
        )
        result = redactor.redact("SSN is 123-45-6789.")
        assert result == "SSN is [SSN]."

    def test_multiple_patterns_replaced(self) -> None:
        """All matching patterns are replaced."""
        redactor = PatternRedactor(
            patterns=[
                re.compile(r"\d{3}-\d{2}-\d{4}"),
                re.compile(r"\b\d{16}\b"),
            ],
        )
        text = "SSN 123-45-6789 card 1234567890123456"
        result = redactor.redact(text)
        assert "123-45-6789" not in result
        assert "1234567890123456" not in result

    def test_no_match_unchanged(self) -> None:
        """Text with no matches is returned unchanged."""
        redactor = PatternRedactor(patterns=[re.compile(r"\d{9}")])
        text = "no digits here"
        assert redactor.redact(text) == text

    def test_default_mask(self) -> None:
        """Default mask is [REDACTED]."""
        redactor = PatternRedactor(patterns=[re.compile(r"secret")])
        assert redactor.redact("the secret is out") == "the [REDACTED] is out"


class TestNoopRedactor:
    """Tests for NoopRedactor."""

    def test_passthrough(self) -> None:
        """NoopRedactor returns text unchanged."""
        redactor = NoopRedactor()
        text = "sensitive data 123-45-6789"
        assert redactor.redact(text) == text


# ---------------------------------------------------------------------------
# artifacts — LocalArtifactStore
# ---------------------------------------------------------------------------

class TestLocalArtifactStore:
    """Tests for LocalArtifactStore."""

    def test_write_returns_file_uri(self, tmp_path: Path) -> None:
        """write() returns a file:// URI pointing to the written file."""
        store = LocalArtifactStore(tmp_path / "artifacts")
        uri = store.write(
            run_id        = "run-abc",
            run_timestamp = "20260706_091845",
            stage_id      = "extract",
            data          = {"key": "value"},
        )
        assert uri.startswith("file://")

    def test_written_file_contains_data(self, tmp_path: Path) -> None:
        """The artifact file contains the JSON-serialised data."""
        store = LocalArtifactStore(tmp_path / "artifacts")
        data  = {"items": [1, 2, 3]}
        uri   = store.write(
            run_id="run-abc", run_timestamp="20260706_091845",
            stage_id="extract", data=data,
        )

        path = Path(uri.replace("file://", ""))
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == data

    def test_file_named_with_stage_and_run_timestamp(self, tmp_path: Path) -> None:
        """Artifact filename is <stage_id>.<run_timestamp>.json — never the UUID."""
        store = LocalArtifactStore(tmp_path / "artifacts")
        store.write(
            run_id="run-xyz-uuid", run_timestamp="20260706_214500",
            stage_id="classify", data={},
        )

        files = list((tmp_path / "artifacts").iterdir())
        assert any(f.name == "classify.20260706_214500.json" for f in files)
        # The UUID run_id must not appear in the operator-facing filename.
        assert not any("run-xyz-uuid" in f.name for f in files)

    def test_collision_does_not_overwrite_previous_run(self, tmp_path: Path) -> None:
        """A same-timestamp write keeps the earlier file rather than overwriting."""
        store = LocalArtifactStore(tmp_path / "artifacts")
        store.write(run_id="r1", run_timestamp="20260706_091845", stage_id="s1", data={"v": 1})
        store.write(run_id="r2", run_timestamp="20260706_091845", stage_id="s1", data={"v": 2})
        files = list((tmp_path / "artifacts").iterdir())
        assert len(files) == 2

    def test_base_dir_created_on_first_write(self, tmp_path: Path) -> None:
        """base_dir is created if it does not exist."""
        base = tmp_path / "deep" / "nested" / "artifacts"
        assert not base.exists()
        store = LocalArtifactStore(base)
        store.write(run_id="r1", run_timestamp="20260706_091845", stage_id="s1", data={})
        assert base.exists()

    def test_slash_in_stage_id_sanitised(self, tmp_path: Path) -> None:
        """Slashes in stage_id are replaced so the filename is valid."""
        store = LocalArtifactStore(tmp_path / "artifacts")
        store.write(run_id="r1", run_timestamp="20260706_091845", stage_id="a/b/c", data={})
        files = list((tmp_path / "artifacts").iterdir())
        assert not any("/" in f.name for f in files)

    def test_write_emits_artifact_reference_with_run_ctx(self, tmp_path: Path) -> None:
        """With a run context, a written stage result emits a files/artifacts record."""
        ctx = SimpleNamespace(
            log_file=str(tmp_path / "rey_analyzer.jsonl"),
            owner_app_name="rey_analyzer",
            run_id="run-pipe-1",
            run_timestamp="20260706_130000",
        )
        store = LocalArtifactStore(tmp_path / "artifacts", run_ctx=ctx)
        uri = store.write(
            run_id="llm-uuid", run_timestamp="20260706_091845",
            stage_id="extract", data={"key": "value"},
        )

        records = [
            json.loads(line)
            for line in Path(ctx.run_log_path).read_text(encoding="utf-8").splitlines()
        ]
        artifact = next(r for r in records if r["record_type"] == "ARTIFACT_REFERENCE")
        assert artifact["record_group"] == "files"
        assert artifact["record_subgroup"] == "artifacts"
        assert artifact["artifact_role"] == "llm_result"
        assert artifact["created_by_step"] == "extract"
        # The record is stamped with the run identity, and its path is the produced file.
        assert artifact["run_id"] == "run-pipe-1"
        assert artifact["path"] == uri.replace("file://", "")

    def test_write_without_run_ctx_emits_no_run_log(self, tmp_path: Path) -> None:
        """Without a run context, the store writes the file but opens no run log."""
        store = LocalArtifactStore(tmp_path / "artifacts")
        store.write(run_id="r1", run_timestamp="20260706_091845", stage_id="s1", data={})
        assert not any(p.name.startswith("run_log.") for p in (tmp_path / "artifacts").iterdir())


# ---------------------------------------------------------------------------
# locking — PipelineLock
# ---------------------------------------------------------------------------

class TestPipelineLock:
    """Tests for PipelineLock."""

    def test_lock_file_created_on_enter(self, tmp_path: Path) -> None:
        """Entering the context manager creates the lock file."""
        log  = tmp_path / "pipeline.jsonl"
        lock = PipelineLock(log, "pipe1")
        with lock:
            lock_file = tmp_path / "pipeline.pipe1.lock"
            assert lock_file.exists()

    def test_lock_file_removed_on_exit(self, tmp_path: Path) -> None:
        """Lock file is removed after the context manager exits."""
        log  = tmp_path / "pipeline.jsonl"
        lock = PipelineLock(log, "pipe1")
        with lock:
            pass
        lock_file = tmp_path / "pipeline.pipe1.lock"
        assert not lock_file.exists()

    def test_lock_file_removed_on_exception(self, tmp_path: Path) -> None:
        """Lock file is removed even when the body raises."""
        log  = tmp_path / "pipeline.jsonl"
        lock = PipelineLock(log, "pipe1")
        with pytest.raises(ValueError):
            with lock:
                raise ValueError("boom")
        lock_file = tmp_path / "pipeline.pipe1.lock"
        assert not lock_file.exists()

    def test_lock_conflict_when_alive_pid_holds_lock(self, tmp_path: Path) -> None:
        """LockConflict is raised when another live process holds the lock."""
        log       = tmp_path / "pipeline.jsonl"
        lock_file = tmp_path / "pipeline.pipe1.lock"
        lock_file.write_text(str(os.getpid()), encoding="utf-8")

        lock = PipelineLock(log, "pipe1")
        with pytest.raises(LockConflict):
            lock.__enter__()

    def test_stale_lock_file_overwritten(self, tmp_path: Path) -> None:
        """A lock file holding a dead PID is overwritten without error."""
        log       = tmp_path / "pipeline.jsonl"
        lock_file = tmp_path / "pipeline.pipe1.lock"
        lock_file.write_text("99999999", encoding="utf-8")  # very likely dead PID

        lock = PipelineLock(log, "pipe1")
        with lock:
            assert lock_file.read_text(encoding="utf-8") == str(os.getpid())

    def test_different_pipelines_do_not_conflict(self, tmp_path: Path) -> None:
        """Two different pipeline_ids use separate lock files."""
        log   = tmp_path / "pipeline.jsonl"
        lock1 = PipelineLock(log, "pipe-a")
        lock2 = PipelineLock(log, "pipe-b")
        with lock1:
            with lock2:
                pass  # no conflict — different lock files


# ---------------------------------------------------------------------------
# document_loader — from_csv truncation
# ---------------------------------------------------------------------------

class TestFromCsvTruncation:
    """Tests for from_csv max_rows enforcement."""

    def _write_csv(self, tmp_path: Path, n_rows: int) -> Path:
        """Write a CSV with a header row + n_rows data rows."""
        p = tmp_path / "data.csv"
        lines = ["col_a,col_b"] + [f"val_{i},num_{i}" for i in range(n_rows)]
        p.write_text("\n".join(lines), encoding="utf-8")
        return p

    def test_rows_within_limit_returned(self, tmp_path: Path) -> None:
        """All rows are returned when count <= max_rows."""
        csv = self._write_csv(tmp_path, 5)
        result, _ = from_csv(csv, max_rows=10)
        assert result.count("|") > 0
        assert "val_4" in result

    def test_rows_truncated_at_limit(self, tmp_path: Path) -> None:
        """Rows beyond max_rows are not included in output."""
        csv = self._write_csv(tmp_path, 20)
        result, _ = from_csv(csv, max_rows=5)
        assert "val_4" in result
        assert "val_5" not in result

    def test_truncation_warning_in_output(self, tmp_path: Path) -> None:
        """A truncation notice appears when rows were dropped."""
        csv = self._write_csv(tmp_path, 20)
        result, _ = from_csv(csv, max_rows=5)
        assert "truncated" in result.lower() or "rows" in result.lower()

    def test_no_truncation_notice_when_all_fit(self, tmp_path: Path) -> None:
        """No truncation notice when all rows fit within max_rows."""
        csv = self._write_csv(tmp_path, 3)
        result, _ = from_csv(csv, max_rows=10)
        assert "truncated" not in result.lower()


# ---------------------------------------------------------------------------
# runner — run() with mock provider
# ---------------------------------------------------------------------------

class TestRunnerApprovalSemantics:
    """Tests for run() requires_approval flag."""

    def test_requires_approval_stores_pending_approval(self, tmp_path: Path) -> None:
        """When requires_approval=True and stage succeeds, status is pending_approval."""
        contract = _write_contract(tmp_path)
        log      = tmp_path / "pipeline.jsonl"
        provider = _make_mock_provider('{"result": "ok"}')

        schema = {"type": "object", "properties": {"result": {"type": "string"}}}

        request = RunRequest(
            pipeline_id       = "test-pipe",
            stage_id          = "stage1",
            contract_path     = contract,
            input_data        = "test input",
            output_schema     = schema,
            log               = log,
            requires_approval = True,
        )

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            response = run(request)

        assert response.status == STATUS_PENDING_APPROVAL
        assert response.parsed_response == {"result": "ok"}

    def test_requires_approval_false_stores_success(self, tmp_path: Path) -> None:
        """When requires_approval=False, successful run stores status=success."""
        contract = _write_contract(tmp_path)
        log      = tmp_path / "pipeline.jsonl"
        provider = _make_mock_provider('{"result": "ok"}')

        schema = {"type": "object", "properties": {"result": {"type": "string"}}}

        request = RunRequest(
            pipeline_id   = "test-pipe",
            stage_id      = "stage1",
            contract_path = contract,
            input_data    = "test input",
            output_schema = schema,
            log           = log,
        )

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            response = run(request)

        assert response.status == STATUS_SUCCESS

    def test_record_written_once_with_correct_status(self, tmp_path: Path) -> None:
        """Only one record is written to the log, with the correct status."""
        contract = _write_contract(tmp_path)
        log      = tmp_path / "pipeline.jsonl"
        provider = _make_mock_provider('{"result": "ok"}')

        schema = {"type": "object", "properties": {"result": {"type": "string"}}}

        request = RunRequest(
            pipeline_id       = "test-pipe",
            stage_id          = "stage1",
            contract_path     = contract,
            input_data        = "test input",
            output_schema     = schema,
            log               = log,
            requires_approval = True,
        )

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            run(request)

        records = load_all_records(log)
        assert len(records) == 1
        assert records[0].status == STATUS_PENDING_APPROVAL

    def test_artifact_uri_embedded_in_record(self, tmp_path: Path) -> None:
        """Artifact URI is stored in the execution record when store is provided."""
        contract       = _write_contract(tmp_path)
        log            = tmp_path / "pipeline.jsonl"
        provider       = _make_mock_provider('{"result": "ok"}')
        artifact_store = LocalArtifactStore(tmp_path / "artifacts")

        schema = {"type": "object", "properties": {"result": {"type": "string"}}}

        request = RunRequest(
            pipeline_id   = "test-pipe",
            stage_id      = "stage1",
            contract_path = contract,
            input_data    = "test input",
            output_schema = schema,
            log           = log,
        )

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            response = run(request, artifact_store=artifact_store)

        assert response.record is not None
        assert len(response.record.artifact_uris) == 1
        assert response.record.artifact_uris[0].startswith("file://")


class TestRunnerEvaluationPersistence:
    """The evaluation run record retains the exact contract and returned result."""

    def test_payload_log_stores_package_source_without_changing_provider_input(
        self, tmp_path: Path
    ) -> None:
        contract = _write_contract(tmp_path)
        payload_log = tmp_path / "payloads.jsonl"
        provider = _make_mock_provider("# Exact result")
        source = {"record_type": "RESULTS_SUMMARY", "status": "failed"}
        package = {
            "analysis_name": "log_interpreter",
            "source_record_type": "RESULTS_SUMMARY",
            "instructions": {"contract": {"name": "log_interpreter"}},
            "source": source,
        }
        runner_input = json.dumps(package) + "\n\nEXISTING ENVELOPE INSTRUCTIONS"
        request = RunRequest(
            pipeline_id="test-pipe",
            stage_id="stage1",
            contract_path=contract,
            input_data=runner_input,
            raw_output=True,
            eval_payload_log_path=payload_log,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(
                name="mock", model="mock-model", provider=provider
            ),
        ):
            run(request)

        payload_record = json.loads(payload_log.read_text(encoding="utf-8"))
        messages = provider.run.call_args.kwargs["messages"]
        assert payload_record["payload"] == source
        assert payload_record["payload"] != package
        assert messages[1].content == runner_input

    def test_structured_success_stores_contract_body_and_parsed_result(
        self, tmp_path: Path
    ) -> None:
        contract_body = "Use this exact contract body."
        contract = _write_contract(tmp_path, body=contract_body)
        payload_log = tmp_path / "payloads.jsonl"
        run_log = tmp_path / "runs.jsonl"
        provider = _make_mock_provider('{"result": "exact"}')
        request = RunRequest(
            pipeline_id="test-pipe",
            stage_id="stage1",
            contract_path=contract,
            input_data="test input",
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
            },
            eval_payload_log_path=payload_log,
            eval_run_log_path=run_log,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(
                name="mock", model="mock-model", provider=provider
            ),
        ):
            response = run(request)

        payload_record = json.loads(payload_log.read_text(encoding="utf-8"))
        run_record = json.loads(run_log.read_text(encoding="utf-8"))
        assert run_record["contract"] == contract_body
        assert run_record["result"] == response.parsed_response == {"result": "exact"}
        assert run_record["payload_id"] == payload_record["payload_id"]
        assert str(UUID(run_record["payload_id"])) == run_record["payload_id"]
        assert run_record["llm_run_id"] == response.run_id
        assert payload_record == {
            "record_type": "LLM_EVALUATION_PAYLOAD",
            "payload_id": run_record["payload_id"],
            "created_at": payload_record["created_at"],
            "payload": "test input",
        }

    @pytest.mark.parametrize(
        "provider_result",
        [
            '{"result":{"answer":42},"status":"ok"}',
            '"{\\"result\\":{\\"answer\\":42},\\"status\\":\\"ok\\"}"',
        ],
    )
    def test_raw_json_success_stores_normalized_result_value(
        self, tmp_path: Path, provider_result: str
    ) -> None:
        contract_text = "Return JSON exactly."
        run_log = tmp_path / "runs.jsonl"
        provider = _make_mock_provider(provider_result)
        request = RunRequest(
            pipeline_id="direct_ask",
            stage_id="direct_ask",
            contract_path=Path("<direct_ask>"),
            contract_text=contract_text,
            input_data="test input",
            raw_output=True,
            eval_run_log_path=run_log,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(
                name="mock", model="mock-model", provider=provider
            ),
        ):
            response = run(request)

        run_record = json.loads(run_log.read_text(encoding="utf-8"))
        assert run_record["contract"] == contract_text
        assert run_record["result"] == {
            "result": {"answer": 42},
            "status": "ok",
        }
        assert response.raw_text == '{"result":{"answer":42},"status":"ok"}'

    def test_raw_success_stores_inline_contract_and_returned_text(
        self, tmp_path: Path
    ) -> None:
        contract_text = "Return Markdown exactly."
        run_log = tmp_path / "runs.jsonl"
        provider = _make_mock_provider("# Exact result")
        request = RunRequest(
            pipeline_id="direct_ask",
            stage_id="direct_ask",
            contract_path=Path("<direct_ask>"),
            contract_text=contract_text,
            input_data="test input",
            raw_output=True,
            eval_run_log_path=run_log,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(
                name="mock", model="mock-model", provider=provider
            ),
        ):
            response = run(request)

        run_record = json.loads(run_log.read_text(encoding="utf-8"))
        assert run_record["contract"] == contract_text
        assert run_record["result"] == response.raw_text == "# Exact result"

    def test_reused_payload_keeps_id_across_runs_with_new_run_ids(
        self, tmp_path: Path
    ) -> None:
        contract = _write_contract(tmp_path)
        payload_log = tmp_path / "payloads.jsonl"
        run_log = tmp_path / "runs.jsonl"
        provider = _make_mock_provider('{"result": "exact"}')
        request = RunRequest(
            pipeline_id="test-pipe",
            stage_id="stage1",
            contract_path=contract,
            input_data="existing payload",
            payload_id="existing-payload-id",
            eval_payload_log_path=payload_log,
            eval_run_log_path=run_log,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(
                name="mock", model="mock-model", provider=provider
            ),
        ):
            first = run(request)
            second = run(request)

        run_records = [
            json.loads(line) for line in run_log.read_text(encoding="utf-8").splitlines()
        ]
        assert not payload_log.exists()
        assert [record["payload_id"] for record in run_records] == [
            "existing-payload-id",
            "existing-payload-id",
        ]
        assert [record["llm_run_id"] for record in run_records] == [
            first.run_id,
            second.run_id,
        ]
        assert first.run_id != second.run_id

    def test_failure_stores_contract_and_error_without_result(
        self, tmp_path: Path
    ) -> None:
        contract_body = "Use this contract even on failure."
        contract = _write_contract(tmp_path, body=contract_body)
        run_log = tmp_path / "runs.jsonl"
        provider = _make_mock_provider()
        provider.run.side_effect = TimeoutFailure("provider timed out")
        request = RunRequest(
            pipeline_id="test-pipe",
            stage_id="stage1",
            contract_path=contract,
            input_data="test input",
            retry_policy=RetryPolicy(max_attempts=1),
            eval_run_log_path=run_log,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(
                name="mock", model="mock-model", provider=provider
            ),
        ):
            response = run(request)

        run_record = json.loads(run_log.read_text(encoding="utf-8"))
        assert response.status == STATUS_FAILED
        assert run_record["status"] == STATUS_FAILED
        assert run_record["contract"] == contract_body
        assert run_record["result"] is None
        assert run_record["error"] == response.errors == ["provider timed out"]
        assert run_record["llm_run_id"] == response.run_id


class TestRunnerRedaction:
    """Tests for run() redaction_filter integration."""

    def test_redacted_text_not_in_provider_call(self, tmp_path: Path) -> None:
        """Sensitive text is replaced before the provider receives the input."""
        contract  = _write_contract(tmp_path)
        provider  = _make_mock_provider('{"result": "ok"}')
        redactor  = PatternRedactor(patterns=[re.compile(r"SECRET")])

        schema = {"type": "object", "properties": {"result": {"type": "string"}}}

        request = RunRequest(
            pipeline_id   = "test-pipe",
            stage_id      = "stage1",
            contract_path = contract,
            input_data    = "value=SECRET",
            output_schema = schema,
        )

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            run(request, redaction_filter=redactor)

        # Verify the provider never received the unredacted text.
        call_args = provider.run.call_args
        messages  = call_args[1]["messages"] if call_args[1] else call_args[0][0]
        all_content = " ".join(m.content for m in messages)
        assert "SECRET" not in all_content


class TestRunnerProviderTimeouts:
    """Tests for provider timeout retry behaviour."""

    def test_timeout_attempts_log_warnings_and_fail_after_retry_limit(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Repeated provider timeouts are warnings until the retry limit fails the run."""
        contract = _write_contract(tmp_path)
        provider = _make_mock_provider()
        provider.run.side_effect = TimeoutFailure("provider timed out")

        request = RunRequest(
            pipeline_id   = "test-pipe",
            stage_id      = "stage1",
            contract_path = contract,
            input_data    = "test input",
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider),
        ):
            response = run(request)

        assert response.status == STATUS_FAILED
        assert provider.run.call_count == 3
        assert "attempt 1/3 timed out" in caplog.text
        assert "too many provider timeouts (3 attempts)" in caplog.text

    def test_timeout_limit_promotes_to_failure_before_max_attempts(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A timeout_limit shorter than max_attempts short-circuits the run early."""
        contract = _write_contract(tmp_path)
        provider = _make_mock_provider()
        provider.run.side_effect = TimeoutFailure("provider timed out")

        policy  = RetryPolicy(max_attempts=5, timeout_limit=2)
        request = RunRequest(
            pipeline_id   = "test-pipe",
            stage_id      = "stage1",
            contract_path = contract,
            input_data    = "test input",
            retry_policy  = policy,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider),
        ):
            response = run(request)

        assert response.status == STATUS_FAILED
        assert provider.run.call_count == 2
        assert "timeout threshold reached (2/5 attempts)" in caplog.text


class TestRunnerRateLimits:
    """Tests for provider rate-limit retry behaviour."""

    def test_rate_limit_attempts_log_warnings_and_fail_after_retry_limit(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Repeated rate-limit responses are logged as warnings; run fails after exhausting retries."""
        contract = _write_contract(tmp_path)
        provider = _make_mock_provider()
        provider.run.side_effect = RateLimitFailure("rate limited")

        request = RunRequest(
            pipeline_id   = "test-pipe",
            stage_id      = "stage1",
            contract_path = contract,
            input_data    = "test input",
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider),
        ):
            response = run(request)

        assert response.status == STATUS_FAILED
        assert provider.run.call_count == 3
        assert "attempt 1/3 rate-limited" in caplog.text
        assert "too many rate-limit responses (3/3 attempts)" in caplog.text

    def test_rate_limit_limit_promotes_to_failure_before_max_attempts(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A rate_limit_limit shorter than max_attempts short-circuits the run early."""
        contract = _write_contract(tmp_path)
        provider = _make_mock_provider()
        provider.run.side_effect = RateLimitFailure("rate limited")

        policy  = RetryPolicy(max_attempts=5, rate_limit_limit=2)
        request = RunRequest(
            pipeline_id   = "test-pipe",
            stage_id      = "stage1",
            contract_path = contract,
            input_data    = "test input",
            retry_policy  = policy,
        )

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider),
        ):
            response = run(request)

        assert response.status == STATUS_FAILED
        assert provider.run.call_count == 2
        assert "rate-limit threshold reached (2/5 attempts)" in caplog.text


# ---------------------------------------------------------------------------
# runner — run_batch()
# ---------------------------------------------------------------------------

class TestRunBatch:
    """Tests for run_batch()."""

    def test_batch_returns_one_response_per_request(self, tmp_path: Path) -> None:
        """run_batch returns a response for each request in order."""
        contract = _write_contract(tmp_path)
        provider = _make_mock_provider('{"result": "ok"}')
        schema   = {"type": "object", "properties": {"result": {"type": "string"}}}

        requests = [
            RunRequest(
                pipeline_id   = "test-pipe",
                stage_id      = f"stage{i}",
                contract_path = contract,
                input_data    = f"input {i}",
                output_schema = schema,
            )
            for i in range(3)
        ]

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            responses = run_batch(requests)

        assert len(responses) == 3
        for resp in responses:
            assert resp.status == STATUS_SUCCESS


# ---------------------------------------------------------------------------
# pipeline — approval flow (run → pending_approval → approve → resume)
# ---------------------------------------------------------------------------

class TestPipelineApprovalFlow:
    """End-to-end approval flow through Pipeline."""

    def _make_pipeline(self, tmp_path: Path, contract: Path, log: Path) -> Pipeline:
        """Build a two-stage pipeline where stage 1 requires approval."""
        return Pipeline(
            stages=[
                Stage(
                    stage_id          = "extract",
                    contract_path     = contract,
                    requires_approval = True,
                    output_schema     = {"type": "object", "properties": {"result": {"type": "string"}}},
                ),
                Stage(
                    stage_id      = "classify",
                    contract_path = contract,
                    output_schema = {"type": "object", "properties": {"result": {"type": "string"}}},
                ),
            ],
            log      = log,
            use_lock = False,
        )

    def test_run_stops_at_requires_approval_stage(self, tmp_path: Path) -> None:
        """Pipeline halts after the first requires_approval stage."""
        contract = _write_contract(tmp_path)
        log      = tmp_path / "pipeline.jsonl"
        provider = _make_mock_provider('{"result": "ok"}')
        pipeline = self._make_pipeline(tmp_path, contract, log)

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            responses = pipeline.run("test input", "pipe1")

        assert len(responses) == 1
        assert responses[0].status == STATUS_PENDING_APPROVAL

    def test_resume_skips_approved_stage(self, tmp_path: Path) -> None:
        """After approving stage 1, resume() skips it and runs stage 2."""
        contract = _write_contract(tmp_path)
        log      = tmp_path / "pipeline.jsonl"
        provider = _make_mock_provider('{"result": "ok"}')
        pipeline = self._make_pipeline(tmp_path, contract, log)

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            pipeline.run("test input", "pipe1")

        # Approve the pending record.
        from rey_lib.llm.records import approve, load_latest_record, store_record

        record  = load_latest_record(log, "pipe1", "extract")
        assert record is not None
        updated, _ = approve(record, reviewer="alice")
        store_record(updated, log)

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            responses = pipeline.resume("test input", "pipe1")

        # First response is the skipped (approved) stage, second is newly executed.
        assert len(responses) == 2
        assert responses[0].status == STATUS_APPROVED
        assert responses[1].status == STATUS_SUCCESS

    def test_hooks_fire_at_correct_points(self, tmp_path: Path) -> None:
        """pre_stage and on_approval_required hooks are called appropriately."""
        contract = _write_contract(tmp_path)
        log      = tmp_path / "pipeline.jsonl"
        provider = _make_mock_provider('{"result": "ok"}')

        pre_calls      : list[str] = []
        approval_calls : list[str] = []

        hooks = PipelineHooks(
            pre_stage            = lambda sid, _: pre_calls.append(sid),
            on_approval_required = lambda sid, _: approval_calls.append(sid),
        )
        pipeline = Pipeline(
            stages=[
                Stage(
                    stage_id          = "extract",
                    contract_path     = contract,
                    requires_approval = True,
                    output_schema     = {"type": "object", "properties": {"result": {"type": "string"}}},
                ),
            ],
            log      = log,
            hooks    = hooks,
            use_lock = False,
        )

        with patch("rey_lib.llm.runner._resolve_provider_config", return_value=_ProviderConfig(name="mock", model="mock-model", provider=provider)):
            pipeline.run("test input", "pipe1")

        assert "extract" in pre_calls
        assert "extract" in approval_calls
