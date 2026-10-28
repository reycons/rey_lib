"""Tests for explicit CSV feed grouping and combined-profile validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from rey_lib.profiling import same_header_errors

from rey_lib.files.error_utils import InputError

from rey_lib.files.feed_config import FeedConfig
from rey_lib.files.feed_config import FeedGroup, validate_feed_groups
from rey_lib.files.feeds import match_files_to_feeds, run_feed_groups


def _feed(name: str, pattern: str, mode: str = "single_file",
          strategy: str | None = None) -> FeedGroup:
    """Build a FeedGroup for tests."""
    return FeedGroup(name=name, file_pattern=pattern, profile_mode=mode,
                     combine_strategy=strategy)


def _run_ctx(tmp_path: Path) -> SimpleNamespace:
    """Build a minimal run-log context for evidence assertions."""
    return SimpleNamespace(
        run_log_path=str(tmp_path / "run_log.20260709_000000.jsonl"),
        run_id="r1",
        run_timestamp="20260709_000000",
        owner_app_name="file_operator",
    )


def _feed_args() -> argparse.Namespace:
    """Build minimal delimited feed args."""
    return argparse.Namespace(
        delimiter=None,
        encoding=None,
        sample_size=None,
        redact_columns="ID,NAME",
        redact_all=False,
        redact_masks=None,
        max_sample_values_per_column=None,
        skip_blank_lines=False,
        header_contains=None,
        classification_filename_regex=None,
    )


def _records(ctx: SimpleNamespace, run_log) -> list[dict]:
    """Read JSONL records from a test run context."""
    return [
        json.loads(line)
        for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestValidateFeedGroups:
    """Local YAML feed-config validation."""

    def test_valid_single_and_combined(self) -> None:
        feeds = [
            _feed("v1", "A_*.csv", "single_file"),
            _feed("daily", "B_*.csv", "combined_files", "same_header"),
        ]
        assert validate_feed_groups(feeds) == []

    def test_missing_required_fields(self) -> None:
        errors = validate_feed_groups([FeedGroup("", "", "")])
        joined = " ".join(errors)
        assert "name" in joined and "file_pattern" in joined and "profile_mode" in joined

    def test_invalid_profile_mode(self) -> None:
        errors = validate_feed_groups([_feed("x", "*.csv", "bogus")])
        assert any("invalid profile_mode" in e for e in errors)

    def test_combined_requires_strategy(self) -> None:
        errors = validate_feed_groups([_feed("x", "*.csv", "combined_files")])
        assert any("requires 'combine_strategy'" in e for e in errors)

    def test_invalid_combine_strategy(self) -> None:
        errors = validate_feed_groups(
            [_feed("x", "*.csv", "combined_files", "concat")]
        )
        assert any("invalid combine_strategy" in e for e in errors)

    def test_duplicate_feed_name(self) -> None:
        errors = validate_feed_groups([_feed("dup", "A_*.csv"), _feed("dup", "B_*.csv")])
        assert any("duplicate feed name" in e for e in errors)


class TestMatchFilesToFeeds:
    """File -> feed matching with 0 / 1 / many semantics."""

    def test_single_file_one_match(self) -> None:
        feeds = [_feed("v1", "Fidelity_Transactions_v1*.csv")]
        files = [Path("Fidelity_Transactions_v1.csv")]
        groups, unmatched, ambiguous = match_files_to_feeds(files, feeds)
        assert list(groups) == ["v1"]
        assert [p.name for p in groups["v1"].files] == ["Fidelity_Transactions_v1.csv"]
        assert unmatched == [] and ambiguous == []

    def test_combined_many_files_one_feed(self) -> None:
        feeds = [_feed("daily", "Fidelity_Transactions_2026-*.csv",
                       "combined_files", "same_header")]
        files = [
            Path("Fidelity_Transactions_2026-06-24.csv"),
            Path("Fidelity_Transactions_2026-06-25.csv"),
        ]
        groups, unmatched, ambiguous = match_files_to_feeds(files, feeds)
        assert len(groups["daily"].files) == 2
        assert unmatched == [] and ambiguous == []

    def test_zero_match_is_unmatched(self) -> None:
        feeds = [_feed("v1", "Fidelity_*.csv")]
        files = [Path("Unknown_File.csv")]
        groups, unmatched, ambiguous = match_files_to_feeds(files, feeds)
        assert groups == {} and [p.name for p in unmatched] == ["Unknown_File.csv"]
        assert ambiguous == []

    def test_multiple_match_is_ambiguous(self) -> None:
        feeds = [
            _feed("daily", "Fidelity_Transactions_2026-*.csv",
                  "combined_files", "same_header"),
            _feed("all", "Fidelity_Transactions_*.csv",
                  "combined_files", "same_header"),
        ]
        files = [Path("Fidelity_Transactions_2026-06-25.csv")]
        groups, unmatched, ambiguous = match_files_to_feeds(files, feeds)
        assert groups == {} and unmatched == []
        assert len(ambiguous) == 1 and "Ambiguous feed pattern match" in ambiguous[0]
        assert "daily" in ambiguous[0] and "all" in ambiguous[0]


class TestSameHeader:
    """same_header compatibility check (normalized names + order)."""

    def test_matching_headers(self) -> None:
        headers = [
            ("a.csv", ["Run Date", "Account", "Amount"]),
            ("b.csv", ["run date", "ACCOUNT", "amount"]),  # normalize-equal
        ]
        assert same_header_errors(headers) == []

    def test_header_mismatch(self) -> None:
        headers = [
            ("a.csv", ["Run Date", "Account", "Amount"]),
            ("b.csv", ["Run Date", "Amount"]),  # different count/order
        ]
        errors = same_header_errors(headers)
        assert errors and "b.csv" in errors[0]


class TestRunFeedGroupsEvidence:
    """Feed grouped outputs emit grounded artifact evidence."""

    def test_single_file_feed_logs_redactor_artifacts(self, run_log, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        source = inbox / "feed.csv"
        source.write_text("ID,NAME\n12345,SMITH\n", encoding="utf-8")

        ctx = _run_ctx(tmp_path)
        feed_cfg = FeedConfig(
            feeds=[_feed("daily", "*.csv", "single_file")],
        )

        status = run_feed_groups(ctx, run_log,
            _feed_args(),
            feed_cfg,
            inbox=inbox,
            output_dir=tmp_path / "out",
            processed_path=tmp_path / "processed",
            rejected_path=tmp_path / "rejected",
        )

        assert status == 0
        artifacts = [
            record
            for record in _records(ctx, run_log)
            if record.get("record_type") == "ARTIFACT_REFERENCE"
        ]
        assert [artifact["artifact_type"] for artifact in artifacts] == [
            "redacted_file",
            "profile",
            "inspection_jsonl",
            "file_type",
            "file_type_analysis",
        ]
        assert [artifact["producer"] for artifact in artifacts] == [
            "redactor",
            "redactor",
            "file_operator",
            "file_operator",
            "file_operator",
        ]
        assert all(artifact["safe_to_preview"] is True for artifact in artifacts)

    def test_combined_feed_classifies_all_files_without_rereading(self, run_log,
        tmp_path: Path,
    ) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        first = inbox / "first.csv"
        second = inbox / "second.csv"
        first.write_text("A,B\n1,2\n", encoding="utf-8")
        second.write_text("A,B\n3,4\n", encoding="utf-8")
        ctx = _run_ctx(tmp_path)
        feed_cfg = FeedConfig(
            feeds=[_feed("daily", "*.csv", "combined_files", "same_header")],
        )

        from rey_lib.files.file_utils import read_text_file as real_read_text
        from unittest.mock import patch

        with patch(
            "rey_lib.files.csv.read_text_file",
            wraps=real_read_text,
        ) as read_text_mock:
            status = run_feed_groups(ctx, run_log,
                _feed_args(),
                feed_cfg,
                inbox=inbox,
                output_dir=tmp_path / "out",
                processed_path=tmp_path / "processed",
                rejected_path=tmp_path / "rejected",
            )

        assert status == 0
        assert read_text_mock.call_count == 2
        # file_type_*.json also matches the analysis manifest and the per-type
        # analysis sibling, so next(glob(...)) selected whichever the filesystem
        # yielded first -- the manifest, whose matching_files sits one level inside
        # each file_types entry. Name the document class this test means.
        file_type_artifacts = [
            path
            for path in sorted((tmp_path / "out").glob("file_type_*.json"))
            if path.name != "file_type_analysis_manifest.json"
            and not path.name.endswith(".analysis.json")
        ]
        assert len(file_type_artifacts) == 1, (
            f"expected one file-type artifact, found {file_type_artifacts}"
        )
        artifact = json.loads(file_type_artifacts[0].read_text(encoding="utf-8"))
        assert artifact["matching_files"] == ["first.csv", "second.csv"]
        assert artifact["profile_artifacts"] == ["daily.profile.json"]
        profile = json.loads(
            (tmp_path / "out" / "daily.profile.json").read_text(encoding="utf-8")
        )
        assert profile["detected_header"] == {
            "index": 0,
            "row_number": 1,
            "line": "A,B",
            "ordered_columns": ["A", "B"],
        }

    def test_combined_filename_non_match_keeps_group_rejection_semantics(
        self,
        run_log,
        tmp_path: Path,
    ) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        matching = inbox / "ABC_hold.csv"
        nonmatching = inbox / "unexpected.csv"
        matching.write_text("A,B\n1,2\n", encoding="utf-8")
        nonmatching.write_text("A,B\n3,4\n", encoding="utf-8")
        args = _feed_args()
        args.classification_filename_regex = (
            r"^(?P<owner>[A-Z]+)_(?P<kind>hold|tran)[.]csv$"
        )

        status = run_feed_groups(
            _run_ctx(tmp_path),
            run_log,
            args,
            FeedConfig(
                feeds=[_feed("daily", "*.csv", "combined_files", "same_header")],
            ),
            inbox=inbox,
            output_dir=tmp_path / "out",
            processed_path=tmp_path / "processed",
            rejected_path=tmp_path / "rejected",
        )

        assert status == 1
        assert sorted(path.name for path in (tmp_path / "rejected").iterdir()) == [
            "ABC_hold.csv",
            "unexpected.csv",
        ]
        assert not list((tmp_path / "out").glob("file_type_*.json"))

    def test_ambiguous_feed_match_logs_validation_result(self, run_log,
        tmp_path: Path,
    ) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "feed.csv").write_text("ID,NAME\n12345,SMITH\n", encoding="utf-8")
        ctx = _run_ctx(tmp_path)
        feed_cfg = FeedConfig(
            feeds=[
                _feed("daily", "*.csv", "single_file"),
                _feed("all", "feed.*", "single_file"),
            ],
        )

        # Recorded, then the operation fails once with one InputError (rule 79).
        with pytest.raises(InputError, match="feed grouping failed"):
            run_feed_groups(ctx, run_log,
                _feed_args(),
                feed_cfg,
                inbox=inbox,
                output_dir=tmp_path / "out",
                processed_path=tmp_path / "processed",
                rejected_path=tmp_path / "rejected",
            )

        validations = [
            record
            for record in _records(ctx, run_log)
            if record.get("record_type") == "VALIDATION_RESULT"
        ]
        assert validations[0]["validation_name"] == "feed_grouping"
        assert validations[0]["status"] == "failed"
        assert validations[0]["error_count"] == 1

    def test_combined_feed_header_mismatch_logs_validation_result(self, run_log,
        tmp_path: Path,
    ) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "a.csv").write_text("ID,NAME\n12345,SMITH\n", encoding="utf-8")
        (inbox / "b.csv").write_text("ID,AMT\n67890,99.00\n", encoding="utf-8")
        ctx = _run_ctx(tmp_path)
        feed_cfg = FeedConfig(
            feeds=[_feed("daily", "*.csv", "combined_files", "same_header")],
        )

        status = run_feed_groups(ctx, run_log,
            _feed_args(),
            feed_cfg,
            inbox=inbox,
            output_dir=tmp_path / "out",
            processed_path=tmp_path / "processed",
            rejected_path=tmp_path / "rejected",
        )

        assert status == 1
        validations = [
            record
            for record in _records(ctx, run_log)
            if record.get("record_type") == "VALIDATION_RESULT"
        ]
        assert validations[0]["validation_name"] == "combined_feed_headers"
        assert validations[0]["status"] == "failed"
        assert validations[0]["feed_name"] == "daily"
