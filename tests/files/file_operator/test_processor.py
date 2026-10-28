"""Tests for file_operator.processor."""

from __future__ import annotations

import argparse
import json
import types

from tests.files.file_operator.conftest import make_run_log
from pathlib import Path
from unittest.mock import patch

import pytest

from rey_lib.files.error_utils import RedactorError

from rey_lib.config.config_utils import PathResolver
from rey_lib.files.layouts.common import effective_max_sample_rows
from rey_lib.files.layouts.delimited import build_csv_parts
from rey_lib.files.processor import process_file, run_batch


def _make_ctx() -> types.SimpleNamespace:
    """Build a minimal ctx-like namespace (used for logging only)."""
    return types.SimpleNamespace()


def _make_run_ctx(tmp_path: Path) -> types.SimpleNamespace:
    """Build a minimal run-log context for evidence assertions."""
    return types.SimpleNamespace(
        run_log_path=str(tmp_path / "run_log.20260709_000000.jsonl"),
        run_id="r1",
        run_timestamp="20260709_000000",
        owner_app_name="data_engine",
    )


def _make_state_ctx(tmp_path: Path) -> types.SimpleNamespace:
    """Build ctx matching what build_ctx_from_path would produce."""
    return types.SimpleNamespace(
        paths=PathResolver({
            "root":                  tmp_path,
            "file_operations_state": tmp_path / "state" / "v01" / "file_operations.jsonl",
        }),
        run_log_path=str(tmp_path / "logs" / "daily.20260713_120000.jsonl"),
        run_id="run-1",
        run_timestamp="20260713_120000",
        owner_app_name="data_engine",
        pipeline_name="daily",
        pipeline_run_id="run-1",
        pipeline_step_name="redact",
        pipeline_step_id="step-1",
        source_name="daily",
    )


def _make_args(**kwargs: object) -> argparse.Namespace:
    """Build a minimal Namespace with safe defaults."""
    defaults = dict(
        output_dir="/tmp/test_out",
        layout="delimited",
        delimiter=None,
        encoding=None,
        max_sample_rows=None,
        sample_size=None,
        redact_columns=None,
        redact_ranges=None,
        worksheet=None,
        processed_path=None,
        rejected_path=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestRunBatch:
    """run_batch — batch loop, exit codes, and file movement."""

    def test_all_success_returns_zero(self, run_log, tmp_path: Path) -> None:
        f = tmp_path / "feed.csv"
        f.write_text("A,B\n1,2\n", encoding="utf-8")

        with patch("rey_lib.files.processor.process_file"):
            result = run_batch([f], _make_ctx(), run_log, _make_args(output_dir=str(tmp_path / "out")))

        assert result == 0

    def test_one_failure_returns_one(self, run_log, tmp_path: Path) -> None:
        f = tmp_path / "feed.csv"
        f.touch()

        with patch("rey_lib.files.processor.process_file", side_effect=RuntimeError("boom")):
            result = run_batch([f], _make_ctx(), run_log, _make_args(output_dir=str(tmp_path / "out")))

        assert result == 1

    def test_filename_classification_non_match_uses_rejected_path(self, run_log,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "unexpected.csv"
        source.write_text("A,B\n1,2\n", encoding="utf-8")
        rejected = tmp_path / "rejected"
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            rejected_path=rejected,
            classification_filename_regex=r"^(?P<owner>ABC)holdJul26[.]csv$",
        )

        assert run_batch([source], _make_ctx(), run_log, args) == 1
        assert not source.exists()
        assert (rejected / source.name).exists()

    def test_empty_file_list_returns_zero(self, run_log) -> None:
        assert run_batch([], _make_ctx(), run_log, _make_args()) == 0

    def test_batch_logs_input_discovery_and_counts(self, run_log, tmp_path: Path,
                                                   caplog: pytest.LogCaptureFixture) -> None:
        f1 = tmp_path / "feed1.csv"
        f2 = tmp_path / "feed2.csv"
        f1.write_text("A,B\n1,2\n", encoding="utf-8")
        f2.write_text("A,B\n3,4\n", encoding="utf-8")
        ctx = _make_run_ctx(tmp_path)

        failure = RuntimeError("boom")
        with patch("rey_lib.files.processor.process_file") as process_file_mock, \
                caplog.at_level("ERROR"):
            process_file_mock.side_effect = [None, failure]
            result = run_batch(
                [f1, f2],
                ctx, run_log,
                _make_args(output_dir=str(tmp_path / "out")),
            )

        assert result == 1
        logged = [r for r in caplog.records if r.levelname == "ERROR"]
        # Reported where it was handled, as a RedactorError with its cause (rule 79).
        assert len(logged) == 1
        assert type(logged[0].exc_info[1]) is RedactorError
        assert logged[0].exc_info[1].__cause__ is failure
        records = [
            json.loads(line)
            for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [record["record_type"] for record in records] == [
            "INPUT_DISCOVERED",
            "INPUT_DISCOVERED",
            "ROW_COUNT",
            "VALIDATION_RESULT",
            "ROW_COUNT",
            "ROW_COUNT",
        ]
        discovered = [record["input_name"] for record in records[:2]]
        assert discovered == ["feed1.csv", "feed2.csv"]
        validation = next(
            record
            for record in records
            if record["record_type"] == "VALIDATION_RESULT"
        )
        assert validation["validation_name"] == "file_processing"
        assert validation["status"] == "failed"
        assert validation["file_name"] == "feed2.csv"
        counts = {
            record["count_name"]: record["count"]
            for record in records
            if record["record_type"] == "ROW_COUNT"
        }
        assert counts == {
            "files_queued": 2,
            "files_processed": 1,
            "files_failed": 1,
        }

    def test_file_moved_to_processed_on_success(self, run_log, tmp_path: Path) -> None:
        f             = tmp_path / "feed.csv"
        f.write_text("A,B\n1,2\n", encoding="utf-8")
        processed_dir = tmp_path / "processed"

        with patch("rey_lib.files.processor.process_file"):
            run_batch(
                [f], _make_ctx(), run_log,
                _make_args(output_dir=str(tmp_path / "out"), processed_path=processed_dir),
            )

        assert (processed_dir / "feed.csv").exists()
        assert not f.exists()

    def test_file_move_writes_run_log_not_state_log(self, run_log, tmp_path: Path) -> None:
        f = tmp_path / "data" / "pipelines" / "daily" / "inbox" / "client_a" / "feed.csv"
        f.parent.mkdir(parents=True)
        f.write_text("A,B\n1,2\n", encoding="utf-8")
        processed_dir = tmp_path / "data" / "pipelines" / "daily" / "processed" / "client_a"

        state_ctx = _make_state_ctx(tmp_path)
        log_path = tmp_path / "logs" / "daily.20260713_120000.jsonl"
        # The run log owns where records land, so it is opened on the log this
        # test reads rather than on the fixture's default.
        batch_log = make_run_log(
            tmp_path, app="data_engine", run_id="run-1",
            run_timestamp="20260713_120000", path=str(log_path),
        )

        with patch("rey_lib.files.processor.process_file"):
            run_batch(
                [f],
                state_ctx, batch_log,
                _make_args(output_dir=str(tmp_path / "out"), processed_path=processed_dir),
            )

        records = [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        record = next(item for item in records if item["record_type"] == "FILE_OPERATION")
        assert record["record_type"] == "FILE_OPERATION"
        assert record["pipeline"] == "daily"
        assert record["source"] == "data/pipelines/daily/inbox/client_a/feed.csv"
        assert record["destination"] == "data/pipelines/daily/processed/client_a/feed.csv"
        assert not (tmp_path / "state" / "v01" / "file_operations.jsonl").exists()

    def test_file_moved_to_rejected_on_failure(self, run_log, tmp_path: Path) -> None:
        f            = tmp_path / "feed.csv"
        f.write_text("A,B\n1,2\n", encoding="utf-8")
        rejected_dir = tmp_path / "rejected"

        with patch("rey_lib.files.processor.process_file", side_effect=RuntimeError("boom")):
            run_batch(
                [f], _make_ctx(), run_log,
                _make_args(output_dir=str(tmp_path / "out"), rejected_path=rejected_dir),
            )

        assert (rejected_dir / "feed.csv").exists()
        assert not f.exists()

    def test_no_movement_when_paths_not_set(self, run_log, tmp_path: Path) -> None:
        """Single-file mode: no processed/rejected paths, file stays in place."""
        f = tmp_path / "feed.csv"
        f.write_text("A,B\n1,2\n", encoding="utf-8")

        with patch("rey_lib.files.processor.process_file"):
            run_batch([f], _make_ctx(), run_log, _make_args())

        assert f.exists()


class TestProcessFile:
    """process_file — routing by layout."""

    def test_routes_to_delimited(self, run_log, tmp_path: Path) -> None:
        f = tmp_path / "feed.csv"
        f.write_text("A,B\n1,2\n", encoding="utf-8")

        with patch("rey_lib.files.processor.get_handler") as get_handler:
            process_file(_make_ctx(), run_log, f, _make_args(output_dir=str(tmp_path / "out"), layout="auto"))
            get_handler.assert_called_once_with("delimited")
            get_handler.return_value.assert_called_once()

    def test_routes_to_fixed_width(self, run_log, tmp_path: Path) -> None:
        f = tmp_path / "feed.fix"
        f.write_text("12345SMITH\n", encoding="utf-8")

        with patch("rey_lib.files.processor.get_handler") as get_handler:
            process_file(_make_ctx(), run_log, f, _make_args(output_dir=str(tmp_path / "out"), layout="fixed_width"))
            get_handler.assert_called_once_with("fixed_width")
            get_handler.return_value.assert_called_once()

    def test_unknown_layout_raises(self, run_log, tmp_path: Path) -> None:
        from rey_lib.files.error_utils import LayoutError
        f = tmp_path / "feed.parquet"
        f.touch()

        with pytest.raises(LayoutError):
            process_file(_make_ctx(), run_log, f, _make_args(output_dir=str(tmp_path / "out"), layout="auto"))


class TestProcessDelimitedIntegration:
    """End-to-end delimited pipeline."""

    def test_absent_sample_configuration_uses_default_500(self) -> None:
        assert effective_max_sample_rows(_make_args()) == 500

    def test_default_sampler_randomizes_500_from_the_complete_row_count(
        self,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "feed.csv"
        source.write_text(
            "A,B\n" + "".join(f"{index},{index * 2}\n" for index in range(750)),
            encoding="utf-8",
        )
        chosen = [599, *range(100, 599)]

        with patch(
            "rey_lib.files.csv._random.sample",
            return_value=chosen,
        ) as sample:
            parts = build_csv_parts(
                source,
                _make_args(redact_columns=None),
            )

        sample.assert_called_once_with(range(750), 500)
        assert len(parts.sampled_original) == 500
        assert parts.sampled_original[0]["A"] == "599"
        assert parts.sampled_original[-1]["A"] == "598"

    def test_max_sample_rows_controls_the_random_sampler(self, tmp_path: Path) -> None:
        source = tmp_path / "feed.csv"
        source.write_text(
            "A,B\n" + "".join(f"{index},{index * 2}\n" for index in range(30)),
            encoding="utf-8",
        )

        with patch(
            "rey_lib.files.csv._random.sample",
            return_value=[29, 4, 18, 7, 22, 11],
        ):
            parts = build_csv_parts(
                source,
                _make_args(max_sample_rows=6, redact_columns=None),
            )

        assert len(parts.sampled_original) == 6
        sampled = [int(row["A"]) for row in parts.sampled_original]
        assert sampled == [29, 4, 18, 7, 22, 11]
        assert sampled != list(range(6))

    def test_sampling_does_not_reintroduce_excluded_rows(self, tmp_path: Path) -> None:
        source = tmp_path / "feed.csv"
        source.write_text(
            "Report title\n"
            "Generated today\n"
            "\n"
            "A,B\n"
            "1,10\n"
            "\n"
            "2,20\n"
            "3,30\n"
            "4,40\n",
            encoding="utf-8",
        )

        with patch(
            "rey_lib.files.csv._random.sample",
            return_value=[3, 0, 1],
        ):
            parts = build_csv_parts(
                source,
                _make_args(
                    max_sample_rows=3,
                    redact_columns=None,
                    skip_blank_lines=True,
                    header_contains=None,
                ),
            )

        assert [row["A"] for row in parts.sampled_original] == ["4", "1", "2"]
        assert all("Report" not in line and "Generated" not in line for line in parts.sampled_lines)

    def test_profile_sample_and_file_type_artifacts_written(self, run_log,
        sample_csv: Path,
        tmp_path: Path,
    ) -> None:
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns="ACCOUNT NUMBER,NAME",
        )
        process_file(_make_ctx(), run_log, sample_csv, args)

        out_dir = tmp_path / "out"
        assert (out_dir / f"{sample_csv.stem}.redacted.sample.csv").exists()
        assert (out_dir / f"{sample_csv.stem}.profile.json").exists()
        assert (out_dir / f"{sample_csv.stem}.inspection.jsonl").exists()
        assert len(
            [
                path
                for path in out_dir.glob("file_type_*.json")
                if ".analysis." not in path.name
                and path.name != "file_type_analysis_manifest.json"
            ]
        ) == 1
        assert len(list(out_dir.glob("file_type_*.analysis.json"))) == 1
        assert (out_dir / "file_type_analysis_manifest.json").exists()
        assert not (out_dir / f"{sample_csv.stem}.packet.md").exists()

    def test_structural_header_detection_skips_preamble(self, run_log, tmp_path: Path) -> None:
        src = tmp_path / "feed.csv"
        src.write_text(
            "Brokerage transaction export\n"
            "Generated for account group\n"
            "\n"
            "Run Date,Account,Amount\n"
            "2026-07-01,Primary,10.00\n"
            "2026-07-02,Secondary,20.00\n",
            encoding="utf-8",
        )
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns=None,
            skip_blank_lines=True,
            header_contains=None,
        )

        process_file(_make_ctx(), run_log, src, args)

        profile = json.loads(
            (tmp_path / "out" / "feed.profile.json").read_text(encoding="utf-8")
        )
        assert profile["detected_header"] == {
            "index": 3,
            "row_number": 4,
            "line": "Run Date,Account,Amount",
            "ordered_columns": ["Run Date", "Account", "Amount"],
        }
        assert [column["raw_name"] for column in profile["columns"]] == [
            "Run Date",
            "Account",
            "Amount",
        ]

    def test_structural_header_detection_allows_sparse_data_before_footer(self, run_log,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "feed.csv"
        src.write_text(
            "Brokerage transaction export\n"
            "Run Date,Account,Amount\n"
            "2026-07-01,Primary,10.00\n"
            "The data provided is informational only.\n"
            "Brokerage services provided by Example.\n",
            encoding="utf-8",
        )
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns=None,
            header_contains=None,
        )

        process_file(_make_ctx(), run_log, src, args)

        profile = json.loads(
            (tmp_path / "out" / "feed.profile.json").read_text(encoding="utf-8")
        )
        assert profile["detected_header"]["index"] == 1
        assert profile["header"] == "Run Date,Account,Amount"

    def test_explicit_header_contains_keeps_priority(self, run_log, tmp_path: Path) -> None:
        src = tmp_path / "feed.csv"
        src.write_text(
            "Wrong,Potential,Header\n"
            "Run Date,Account,Amount\n"
            "2026-07-01,Primary,10.00\n",
            encoding="utf-8",
        )
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns=None,
            skip_blank_lines=False,
            header_contains="Run Date",
        )

        process_file(_make_ctx(), run_log, src, args)

        profile = json.loads(
            (tmp_path / "out" / "feed.profile.json").read_text(encoding="utf-8")
        )
        assert profile["detected_header"]["index"] == 1
        assert profile["header"] == "Run Date,Account,Amount"

    def test_header_detection_and_classification_share_one_source_read(self, run_log,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "feed.csv"
        src.write_text("A,B\n1,2\n3,4\n", encoding="utf-8")
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns=None,
        )

        from rey_lib.files.file_utils import read_text_file as real_read_text

        with patch(
            "rey_lib.files.csv.read_text_file",
            wraps=real_read_text,
        ) as read_text_mock:
            process_file(_make_ctx(), run_log, src, args)

        read_text_mock.assert_called_once_with(src, encoding="utf-8-sig", errors="strict")

    def test_header_detection_rejects_insufficient_structure(self, run_log,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "feed.csv"
        src.write_text("Report title\nOnly one value\n", encoding="utf-8")
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns=None,
        )

        from rey_lib.files.error_utils import InputError

        with pytest.raises(InputError, match="sufficient structural confidence"):
            process_file(_make_ctx(), run_log, src, args)

    def test_batch_groups_same_layout_and_separates_distinct_layouts(self, run_log,
        tmp_path: Path,
    ) -> None:
        first = tmp_path / "first.csv"
        second = tmp_path / "second.csv"
        distinct = tmp_path / "distinct.csv"
        first.write_text("A,B\n1,2\n", encoding="utf-8")
        second.write_text("a,b\n3,4\n", encoding="utf-8")
        distinct.write_text("A,C\n5,6\n", encoding="utf-8")
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns=None,
        )

        from rey_lib.files.file_utils import read_text_file as real_read_text

        with patch(
            "rey_lib.files.csv.read_text_file",
            wraps=real_read_text,
        ) as read_text_mock:
            assert run_batch(
                [first, second, distinct],
                _make_ctx(), run_log,
                args,
            ) == 0

        assert read_text_mock.call_count == 3
        artifacts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((tmp_path / "out").glob("file_type_*.json"))
            if ".analysis." not in path.name
            and path.name != "file_type_analysis_manifest.json"
        ]
        assert len(artifacts) == 2
        matching_sets = {
            tuple(artifact["matching_files"])
            for artifact in artifacts
        }
        assert matching_sets == {
            ("distinct.csv",),
            ("first.csv", "second.csv"),
        }
        assert all(artifact["columns"] for artifact in artifacts)

    def test_sample_preserves_raw_lines(self, run_log, tmp_path: Path) -> None:
        src = tmp_path / "pipe_feed.tr1"
        src.write_text(
            "ACCT|NAME|AMT\n"
            "12345|SMITH  |1500.00\n"
            "67890|JONES  |2200.50\n",
            encoding="utf-8",
        )
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            delimiter="|",
            redact_columns="ACCT,NAME",
        )
        process_file(_make_ctx(), run_log, src, args)

        sample = (tmp_path / "out" / "pipe_feed.redacted.sample.tr1").read_text()
        profile = json.loads((tmp_path / "out" / "pipe_feed.profile.json").read_text(encoding="utf-8"))
        assert "|" in sample
        assert "  |" in sample  # trailing spaces on NAME field preserved
        # Delimiter and encoding are read instructions and are stated once, in
        # loader_hints; file_pattern and the header line describe the dataset.
        assert profile["loader_hints"]["delimiter"] == "|"
        assert profile["loader_hints"]["encoding"] == "utf-8-sig"
        assert "delimiter" not in profile
        assert "encoding" not in profile
        assert profile["file_pattern"] == "*.tr1"
        assert profile["header"] == "ACCT|NAME|AMT"

    def test_sensitive_values_replaced(self, run_log, tmp_path: Path) -> None:
        src = tmp_path / "feed.csv"
        src.write_text("ID,NAME,AMT\n12345,SMITH,99.00\n", encoding="utf-8")
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns="ID,NAME",
        )
        process_file(_make_ctx(), run_log, src, args)

        sample = (tmp_path / "out" / "feed.redacted.sample.csv").read_text()
        assert "12345" not in sample
        assert "SMITH" not in sample
        assert "99.00" in sample

    def test_delimited_processing_logs_redaction_statistics(self, run_log,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "feed.csv"
        src.write_text(
            "ID,NAME,AMT\n"
            "12345,SMITH,99.00\n"
            "67890,JONES,105.25\n",
            encoding="utf-8",
        )
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns="ID,NAME",
        )
        ctx = _make_run_ctx(tmp_path)

        process_file(ctx, run_log, src, args)

        records = [
            json.loads(line)
            for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        counts = {
            record["count_name"]: record
            for record in records
            if record.get("record_type") == "ROW_COUNT"
        }
        assert counts["rows_total"]["count"] == 2
        assert counts["rows_sampled"]["count"] == 2
        assert counts["redacted_values"]["count"] == 4
        assert counts["redacted_values"]["redacted_columns"] == ["ID", "NAME"]

    def test_profile_types_use_original_values_when_columns_are_redacted(self, run_log,
        tmp_path: Path,
    ) -> None:
        src = tmp_path / "feed.csv"
        src.write_text(
            "ID,AMT,TRADE_DATE\n"
            "12345,99.00,2026-05-01\n"
            "67890,105.25,2026-05-02\n",
            encoding="utf-8",
        )
        args = _make_args(
            output_dir=str(tmp_path / "out"),
            layout="delimited",
            redact_columns="ID,AMT,TRADE_DATE",
        )
        process_file(_make_ctx(), run_log, src, args)

        profile_path = tmp_path / "out" / "feed.profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        types_by_name = {col["name"]: col["type"] for col in profile["columns"]}
        samples_by_name = {
            col["name"]: [entry["value"] for entry in col["sample_values"]]
            for col in profile["columns"]
        }

        assert types_by_name["ID"] == "integer"
        assert types_by_name["AMT"] == "decimal"
        assert types_by_name["TRADE_DATE"] == "date"
        assert "12345" not in samples_by_name["ID"]
        assert "99.00" not in samples_by_name["AMT"]

        amount_profile = next(col for col in profile["columns"] if col["name"] == "AMT")
        assert amount_profile["max_decimal_places"] == 2

        sample = (tmp_path / "out" / "feed.redacted.sample.csv").read_text(encoding="utf-8")
        assert "99.00" not in sample
        assert "105.25" not in sample
