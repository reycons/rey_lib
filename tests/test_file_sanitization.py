from __future__ import annotations

import codecs
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from rey_lib.encryption import sha256_file
from rey_lib.files import (
    FileSanitizationCollisionPolicy,
    FileSanitizationContext,
    FileSanitizationError,
    FileSanitizationEvidenceError,
    GovernedFileReference,
    SourceFileMutationEvidenceError,
    SourceFileMutationEvidenceFailurePhase,
    SourceFileMutationEvidenceResult,
    compose_sanitization_policy,
    sanitize_file,
)
from rey_lib.files import sanitization
from rey_lib.files import primitive_file_io
from rey_lib.files.primitive_file_io import StagedStreamWrite


def _ctx(
    tmp_path: Path,
    *,
    collision: FileSanitizationCollisionPolicy = (
        FileSanitizationCollisionPolicy.FAIL
    ),
    dry_run: bool = False,
) -> FileSanitizationContext:
    policy = compose_sanitization_policy(
        {
            "policy_name": "platform",
            "policy_version": "1.0",
            "remove": {
                "U+0000": {"name": "NULL", "reason": "invalid null"},
                "U+0007": {"name": "BELL", "reason": "invalid bell"},
            },
            "preserve": {
                "U+0009": {"name": "TAB", "reason": "valid tab"},
            },
            "preserve_if_quoted": {
                "U+000A": {"name": "LF", "reason": "quoted newline"},
                "U+000D": {"name": "CR", "reason": "quoted newline"},
            },
            "replace": {},
            "line_repair": {},
        },
        {
            "policy_name": "feed",
            "policy_version": "1.0",
            "remove": {},
            "preserve": {},
            "preserve_if_quoted": {},
            "replace": {},
            "line_repair": {},
        },
    )
    return FileSanitizationContext(
        state_ctx=object(),
        application_name="test_app",
        destination_path=tmp_path / "out" / "positions.clean.csv",
        governed_roots=(tmp_path,),
        policy=policy,
        collision_policy=collision,
        dry_run=dry_run,
        mutation_run_log_fields={"source_record_id": 9},
    )


def _file(path: Path, classification: dict | None = None) -> GovernedFileReference:
    return GovernedFileReference(
        file_id="file-1",
        current_path=path,
        classification=classification,
    )


def _sanitize(
    ctx: FileSanitizationContext,
    file: GovernedFileReference,
):
    with patch.object(sanitization, "log_source_file_mutation", return_value=41):
        return sanitize_file(ctx, file)


def test_canonical_utf8_lf_input_is_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    payload = "Name,Value\nJosé,10\n字,20".encode()
    source.write_bytes(payload)

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert source.read_bytes() == payload
    assert result.destination_path.read_bytes() == payload
    assert result.output_bytes_differ is False
    assert result.destination_encoding == "utf-8"
    assert result.resolved_source_encoding == "utf-8"
    assert result.source_encoding_resolution_method == "utf8_validation"
    assert result.source_bom == "absent"
    assert result.source_bom_present is False
    assert result.output_encoding_changed is False
    assert result.normalized_true_line_ending_count == 2
    assert result.remove_counts_by_rule == {"U+0000": 0, "U+0007": 0}


def test_utf8_bom_is_removed(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(codecs.BOM_UTF8 + b"A,B\n1,2\n")

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.source_bom == "utf-8"
    assert result.source_bom_present is True
    assert result.source_encoding_resolution_method == "bom"
    assert result.output_encoding_changed is True
    assert result.destination_path.read_bytes() == b"A,B\n1,2\n"
    assert result.output_bytes_differ is True


def test_bom_declared_utf16_becomes_utf8_without_bom(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(codecs.BOM_UTF16_LE + "A,B\r\n1,é".encode("utf-16-le"))

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.resolved_source_encoding == "utf-16-le"
    assert result.source_encoding_resolution_method == "bom"
    assert result.source_bom_present is True
    assert result.output_encoding_changed is True
    assert result.source_bom == "utf-16-le"
    assert result.destination_path.read_bytes() == "A,B\n1,é".encode()
    assert result.normalized_true_line_ending_count == 1


def test_detected_windows_1252_becomes_utf8(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes("Name\rCafé".encode("windows-1252"))

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.resolved_source_encoding == "cp1252"
    assert result.source_encoding_resolution_method == "windows_1252_fallback"
    assert result.source_bom_present is False
    assert result.output_encoding_changed is True
    assert result.destination_path.read_bytes() == "Name\nCafé".encode()


def test_invalid_bytes_fail_closed_without_destination(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\n\x81,2\n")

    with pytest.raises(FileSanitizationError):
        _sanitize(_ctx(tmp_path), _file(source))

    assert source.read_bytes() == b"A,B\n\x81,2\n"
    assert not (tmp_path / "out" / "positions.clean.csv").exists()
    assert list((tmp_path / "out").glob(".*.tmp")) == []


def test_bom_declares_source_encoding_without_configuration(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(codecs.BOM_UTF16_LE + "A,B".encode("utf-16-le"))

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.resolved_source_encoding == "utf-16-le"
    assert result.destination_path.read_bytes() == b"A,B"


def test_unsupported_bom_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"+/v8A,B")

    with pytest.raises(FileSanitizationError, match="unsupported BOM.*UTF-7"):
        _sanitize(_ctx(tmp_path), _file(source))

    assert not (tmp_path / "out" / "positions.clean.csv").exists()


def test_newlines_final_state_blank_lines_and_character_counts(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_text(
        "A\x00,B\tC\r\n\r\n1,ab\u200bcd\r2,x\x07y\nlast",
        encoding="utf-8",
        newline="",
    )

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.destination_path.read_text() == "A,B\tC\n\n1,ab\u200bcd\n2,xy\nlast"
    assert result.normalized_true_line_ending_count == 4
    assert result.remove_counts_by_rule == {"U+0000": 1, "U+0007": 1}
    assert result.preserve_counts_by_rule == {"U+0009": 1}
    assert not result.destination_path.read_bytes().endswith(b"\n")


def test_final_newline_presence_is_preserved(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\r")

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.destination_path.read_bytes() == b"A,B\n"
    assert result.normalized_true_line_ending_count == 1


def test_incremental_decoder_and_crlf_state_cross_chunk_boundaries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes("é\x00,字\r\nΩ,x".encode())

    with patch.object(sanitization, "_CHUNK_BYTES", 2):
        result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.destination_path.read_bytes() == "é,字\nΩ,x".encode()
    assert result.normalized_true_line_ending_count == 1
    assert result.remove_counts_by_rule["U+0000"] == 1


def test_chunk_size_never_changes_output_or_counts(tmp_path: Path) -> None:
    payload = "é\x00,字\r\n\rnext\tvalue\n".encode()
    outputs = []
    counts = []
    for index, chunk_size in enumerate((1, 2, 7, 65536)):
        root = tmp_path / str(index)
        root.mkdir()
        source = root / "positions.csv"
        source.write_bytes(payload)
        with patch.object(sanitization, "_CHUNK_BYTES", chunk_size):
            result = _sanitize(_ctx(root), _file(source))
        outputs.append(result.destination_path.read_bytes())
        counts.append(
                (
                    result.normalized_true_line_ending_count,
                    tuple(result.remove_counts_by_rule.items()),
                    tuple(result.preserve_counts_by_rule.items()),
                )
        )

    assert outputs == [outputs[0]] * 4
    assert counts == [counts[0]] * 4


def test_collision_fails_closed_and_overwrite_requires_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "positions.csv"
    destination = tmp_path / "out" / "positions.clean.csv"
    destination.parent.mkdir()
    source.write_bytes(b"A,B\r\n")
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        _sanitize(_ctx(tmp_path), _file(source))
    assert destination.read_bytes() == b"existing"

    result = _sanitize(
        _ctx(
            tmp_path,
            collision=FileSanitizationCollisionPolicy.OVERWRITE,
        ),
        _file(source),
    )
    assert destination.read_bytes() == b"A,B\n"
    assert result.destination_replaced is True


def test_install_failure_leaves_no_partial_destination(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\n")

    with patch.object(
        StagedStreamWrite,
        "install",
        side_effect=OSError("install failed"),
    ):
        with pytest.raises(FileSanitizationError, match="install failed"):
            _sanitize(_ctx(tmp_path), _file(source))

    destination = tmp_path / "out" / "positions.clean.csv"
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_fsync_failure_leaves_no_partial_destination(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\n")

    with patch.object(
        primitive_file_io,
        "_flush_file",
        side_effect=OSError("fsync failed"),
    ):
        with pytest.raises(FileSanitizationError, match="fsync failed"):
            _sanitize(_ctx(tmp_path), _file(source))

    destination = tmp_path / "out" / "positions.clean.csv"
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_classification_is_passed_unchanged_and_not_mutated(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\n")
    classification = {"type": "regex", "values": {"feed": "one", "nested": [1]}}
    snapshot = deepcopy(classification)
    reference = _file(source, classification)

    with patch.object(
        sanitization,
        "log_source_file_mutation",
        return_value=41,
    ) as logged:
        sanitize_file(_ctx(tmp_path), reference)

    assert logged.call_args.kwargs["file_id"] == "file-1"
    assert logged.call_args.kwargs["source_path"] == source
    assert logged.call_args.kwargs["destination_path"] == (
        tmp_path / "out" / "positions.clean.csv"
    )
    assert logged.call_args.kwargs["classification"] == snapshot
    evidence = logged.call_args.kwargs["run_log_fields"]
    assert evidence["source_encoding"] == "utf-8"
    assert evidence["source_encoding_resolution_method"] == "utf8_validation"
    assert evidence["source_bom"] == "absent"
    assert evidence["source_bom_present"] is False
    assert evidence["output_encoding_changed"] is False
    assert classification == snapshot


def test_evidence_failure_preserves_exact_acknowledgement_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\r\n")
    failure = SourceFileMutationEvidenceError(
        "manifest acknowledgement failed",
        phase=(
            SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED
        ),
        run_log_record_id=17,
        run_log_file="run.jsonl",
    )

    with patch.object(
        sanitization,
        "log_source_file_mutation",
        side_effect=failure,
    ):
        with pytest.raises(FileSanitizationEvidenceError) as raised:
            sanitize_file(_ctx(tmp_path), _file(source))

    result = raised.value.result
    assert result is not None
    assert result.filesystem_applied is True
    assert result.complete_evidence_acknowledged is False
    assert result.mutation_run_log_record_id == 17
    assert result.mutation_run_log_file == "run.jsonl"
    assert result.evidence_phase is failure.phase


def test_dry_run_does_not_create_output_or_evidence(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\r\n")

    with patch.object(sanitization, "log_source_file_mutation") as logged:
        result = sanitize_file(_ctx(tmp_path, dry_run=True), _file(source))

    assert result.status == "planned"
    assert result.filesystem_applied is False
    assert not result.destination_path.exists()
    logged.assert_not_called()


def test_hashes_and_size_are_exact(tmp_path: Path) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A\x00,B\r\n")

    result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.source_sha256 == sha256_file(source)
    assert result.destination_sha256 == sha256_file(result.destination_path)
    assert result.destination_size == len(b"A,B\n")
    assert result.file_manifest_record_id == 41
    assert result.complete_evidence_acknowledged is True


def test_feed_override_reclassifies_global_rule_and_digest_is_deterministic() -> None:
    global_policy = {
        "policy_name": "platform",
        "policy_version": "1.0",
        "remove": {"U+0009": {"name": "TAB", "reason": "global removal"}},
        "preserve": {},
        "preserve_if_quoted": {},
        "replace": {},
        "line_repair": {},
    }
    feed = {
        "policy_name": "feed",
        "policy_version": "2.0",
        "remove": {},
        "preserve": {"U+0009": {"reason": "feed delimiter", "name": "TAB"}},
        "preserve_if_quoted": {},
        "replace": {},
        "line_repair": {},
    }

    first = compose_sanitization_policy(global_policy, feed)
    second = compose_sanitization_policy(dict(reversed(global_policy.items())), feed)

    assert first.digest == second.digest
    assert [(key, action) for key, action, _ in first.character_rules] == [
        ("U+0009", "preserve")
    ]


@pytest.mark.parametrize(
    "policy, message",
    [
        (
            {
                "policy_name": "bad",
                "policy_version": "1",
                "remove": {"U+0009": {"name": "TAB", "reason": "one"}},
                "preserve": {"U+0009": {"name": "TAB", "reason": "two"}},
                "preserve_if_quoted": {},
                "replace": {},
                "line_repair": {},
            },
            "multiple global policy tables",
        ),
        (
            {
                "policy_name": "bad",
                "policy_version": "1",
                "remove": {"u+0000": {"name": "NULL", "reason": "bad key"}},
                "preserve": {},
                "preserve_if_quoted": {},
                "replace": {},
                "line_repair": {},
            },
            "Invalid Unicode code-point",
        ),
    ],
)
def test_policy_conflicts_and_malformed_entries_fail_closed(
    policy: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        compose_sanitization_policy(policy, {
            "policy_name": "feed", "policy_version": "1", "remove": {},
            "preserve": {}, "preserve_if_quoted": {}, "replace": {},
            "line_repair": {},
        })


def test_quoted_newlines_remain_exact_while_true_endings_normalize(
    tmp_path: Path,
) -> None:
    source = tmp_path / "quoted.csv"
    source.write_bytes(b'Name,Text\r\nA,"one\rtwo\nthree\r\nfour"\rB,end\n')

    with patch.object(sanitization, "_CHUNK_BYTES", 1):
        result = _sanitize(_ctx(tmp_path), _file(source))

    assert result.destination_path.read_bytes() == (
        b'Name,Text\nA,"one\rtwo\nthree\r\nfour"\nB,end\n'
    )
    assert result.normalized_true_line_ending_count == 3
    assert result.preserve_if_quoted_counts_by_rule == {
        "U+000A": 2,
        "U+000D": 2,
    }


def test_explicit_replacement_and_unclassified_unicode_are_preserved(
    tmp_path: Path,
) -> None:
    source = tmp_path / "visible.csv"
    source.write_text("A\u00a0B,“quote”—dash\u200b\n", encoding="utf-8")
    policy = compose_sanitization_policy(
        {
            "policy_name": "platform", "policy_version": "1", "remove": {},
            "preserve": {}, "preserve_if_quoted": {},
            "replace": {"U+00A0": {"name": "NBSP", "with": " ", "reason": "feed contract"}},
            "line_repair": {},
        },
        {
            "policy_name": "feed", "policy_version": "1", "remove": {},
            "preserve": {}, "preserve_if_quoted": {}, "replace": {},
            "line_repair": {},
        },
    )
    ctx = replace(_ctx(tmp_path), policy=policy)

    result = _sanitize(ctx, _file(source))

    assert result.destination_path.read_text() == "A B,“quote”—dash\u200b\n"
    assert result.replacement_counts_by_rule == {"U+00A0": 1}


def test_line_repair_is_absent_by_default_and_applies_only_when_configured(
    tmp_path: Path,
) -> None:
    source = tmp_path / "spaces.csv"
    source.write_bytes(b"A,B  \r\n")
    untouched = _sanitize(_ctx(tmp_path), _file(source))
    assert untouched.destination_path.read_bytes() == b"A,B  \n"
    untouched.destination_path.unlink()

    base = {
        "policy_name": "platform", "policy_version": "1", "remove": {},
        "preserve": {}, "preserve_if_quoted": {}, "replace": {},
        "line_repair": {},
    }
    feed = {
        "policy_name": "feed", "policy_version": "1", "remove": {},
        "preserve": {}, "preserve_if_quoted": {}, "replace": {},
        "max_logical_line_characters": 20,
        "line_repair": {
            "trim_trailing": {
                "pattern": "[ \\t]+$", "replacement": "", "reason": "feed contract"
            }
        },
    }
    repaired = _sanitize(replace(_ctx(tmp_path), policy=compose_sanitization_policy(base, feed)), _file(source))
    assert repaired.destination_path.read_bytes() == b"A,B\n"
    assert repaired.line_repair_counts_by_rule == {"trim_trailing": 1}


def test_default_rule_matrix_removes_only_named_controls_and_preserves_exceptions(
    tmp_path: Path,
) -> None:
    removed = [0x00, 0x08, 0x0B, 0x0C, 0x1B, 0x7F]
    preserved = [0x09, 0x1C, 0x1D, 0x1E, 0x1F]
    global_policy = {
        "policy_name": "platform", "policy_version": "1",
        "remove": {
            f"U+{value:04X}": {"name": f"C{value:02X}", "reason": "configured"}
            for value in removed
        },
        "preserve": {
            f"U+{value:04X}": {"name": f"C{value:02X}", "reason": "configured"}
            for value in preserved
        },
        "preserve_if_quoted": {}, "replace": {}, "line_repair": {},
    }
    feed = {
        "policy_name": "feed", "policy_version": "1", "remove": {},
        "preserve": {}, "preserve_if_quoted": {}, "replace": {},
        "line_repair": {},
    }
    source = tmp_path / "matrix.csv"
    source.write_text(
        "start" + "".join(map(chr, removed + preserved)) + "\u00a0“”—–end",
        encoding="utf-8",
    )

    result = _sanitize(
        replace(_ctx(tmp_path), policy=compose_sanitization_policy(global_policy, feed)),
        _file(source),
    )

    assert result.destination_path.read_text() == (
        "start" + "".join(map(chr, preserved)) + "\u00a0“”—–end"
    )
    assert all(count == 1 for count in result.remove_counts_by_rule.values())
    assert all(count == 1 for count in result.preserve_counts_by_rule.values())


def test_removing_a_rule_disables_it_and_line_bound_fails_before_publication(
    tmp_path: Path,
) -> None:
    base = {
        "policy_name": "platform", "policy_version": "1", "remove": {},
        "preserve": {}, "preserve_if_quoted": {}, "replace": {},
        "line_repair": {},
    }
    feed = {
        "policy_name": "feed", "policy_version": "1", "remove": {},
        "preserve": {}, "preserve_if_quoted": {}, "replace": {},
        "max_logical_line_characters": 3,
        "line_repair": {
            "trim": {"pattern": " +$", "replacement": "", "reason": "configured"}
        },
    }
    source = tmp_path / "bounded.csv"
    source.write_bytes(b"abcd\x08")

    with pytest.raises(FileSanitizationError, match="exceeds configured maximum"):
        _sanitize(
            replace(_ctx(tmp_path), policy=compose_sanitization_policy(base, feed)),
            _file(source),
        )

    assert source.read_bytes() == b"abcd\x08"
    assert not (tmp_path / "out" / "bounded.clean.csv").exists()


@pytest.mark.parametrize(
    ("action", "entry", "expected"),
    [
        ("remove", {"name": "QUOTE", "reason": "feed rule"}, b"a\nb\n"),
        (
            "replace",
            {"name": "QUOTE", "with": "'", "reason": "feed rule"},
            b"'a\nb'\n",
        ),
    ],
)
def test_quote_state_is_maintained_while_quote_rules_are_applied(
    tmp_path: Path,
    action: str,
    entry: dict[str, str],
    expected: bytes,
) -> None:
    tables = {name: {} for name in ("remove", "preserve", "preserve_if_quoted", "replace")}
    tables[action] = {"U+0022": entry}
    policy = compose_sanitization_policy(
        {
            "policy_name": "platform", "policy_version": "1", **tables,
            "line_repair": {},
        },
        {
            "policy_name": "feed", "policy_version": "1", "remove": {},
            "preserve": {}, "preserve_if_quoted": {}, "replace": {},
            "line_repair": {},
        },
    )
    source = tmp_path / "quoted.csv"
    source.write_bytes(b'"a\nb"\r\n')

    result = _sanitize(replace(_ctx(tmp_path), policy=policy), _file(source))

    assert result.destination_path.read_bytes() == expected
    counts = result.remove_counts_by_rule if action == "remove" else result.replacement_counts_by_rule
    assert counts == {"U+0022": 2}
    assert result.normalized_true_line_ending_count == 1


def test_non_string_replacement_is_rejected_without_coercion() -> None:
    with pytest.raises(ValueError, match="requires 'with' to be a string"):
        compose_sanitization_policy(
            {
                "policy_name": "platform", "policy_version": "1", "remove": {},
                "preserve": {}, "preserve_if_quoted": {},
                "replace": {"U+00A0": {"name": "NBSP", "with": 7, "reason": "bad"}},
                "line_repair": {},
            },
            {
                "policy_name": "feed", "policy_version": "1", "remove": {},
                "preserve": {}, "preserve_if_quoted": {}, "replace": {},
                "line_repair": {},
            },
        )


def test_success_result_carries_both_acknowledged_evidence_references(
    tmp_path: Path,
) -> None:
    source = tmp_path / "positions.csv"
    source.write_bytes(b"A,B\n")
    acknowledged = SourceFileMutationEvidenceResult(
        41,
        run_log_record_id=17,
        run_log_file="run.jsonl",
    )

    with patch.object(
        sanitization,
        "log_source_file_mutation",
        return_value=acknowledged,
    ):
        result = sanitize_file(_ctx(tmp_path), _file(source))

    assert result.file_manifest_record_id == 41
    assert result.mutation_run_log_record_id == 17
    assert result.mutation_run_log_file == "run.jsonl"
    assert result.complete_evidence_acknowledged is True


def test_zero_width_line_repair_is_rejected_before_processing() -> None:
    with pytest.raises(ValueError, match="may match an empty string"):
        compose_sanitization_policy(
            {
                "policy_name": "platform", "policy_version": "1", "remove": {},
                "preserve": {}, "preserve_if_quoted": {}, "replace": {},
                "line_repair": {},
            },
            {
                "policy_name": "feed", "policy_version": "1", "remove": {},
                "preserve": {}, "preserve_if_quoted": {}, "replace": {},
                "max_logical_line_characters": 100,
                "line_repair": {
                    "lookahead": {
                        "pattern": "(?=a)", "replacement": "x", "reason": "bad"
                    }
                },
            },
        )
