"""Byte-level golden baseline for the structural analysis artifact.

Captured from commit 849191f before any of the reduction logic moved to
rey_lib. The artifact's bytes are its identity: analysis_artifact_sha256 is
recorded in file_type_analysis_manifest.json and in ARTIFACT_REFERENCE
evidence, so parsed equality is not good enough here.

The artifact is deterministic and carries no path or timestamp, which is what
makes a stored golden meaningful; test_the_artifact_is_reproducible asserts
that rather than assuming it.

Both callers of the analysis path are covered -- layouts.delimited.process via
processor, and feeds.py -- because feeds is an independent entry point that the
extraction must not overlook.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files import inspection as inspection_module
from rey_lib.files.processor import process_file, run_batch

# Inputs are defined here rather than beside the goldens so there is one source
# of truth. Regenerating a golden means changing these and re-capturing.
SCENARIOS: dict[str, dict[str, str]] = {
    # Sanitized content, because that is what this stage receives: the
    # profiling process selects mutations on `result.reason: file_sanitization`,
    # so a preamble line, a blank row and a ragged row have all been removed
    # before an analysis artifact is ever built. Feeding them here profiled
    # input production cannot produce, and a blank or ragged row landing first
    # in the sample left `profile_rows` with no columns -- so validation raised
    # and the artifact this test is about was never reached.
    #
    # A leading zero, an empty value and a non-ASCII value are kept: those are
    # legal in a sanitized file and are what the artifact should be pinned over.
    "single_file": {
        "statement.csv": (
            "Account,Name,Amount\n00123,Alice,10.50\n"
            "00456,Bob,\n00789,Ünïcode café,7\n00999,Dave,1\n"
        ),
    },
    "multi_file": {
        "jan.csv": "Account,Name,Amount\n1,Alice,10\n2,Bob,20\n",
        "feb.csv": "Account,Name,Amount\n3,Carol,30\n4,字段,40\n5,Eve,\n",
    },
    "varied_structure": {
        "varied.csv": "A,B,C\n"
        + "".join(f"{i},{'x' * (i % 7)},{'y' * (i % 3)}\n" for i in range(1, 40)),
    },
}


def _args(output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        output_dir=str(output_dir),
        layout="delimited",
        delimiter=None,
        encoding=None,
        sample_size=None,
        redact_columns=None,
        redact_ranges=None,
        worksheet=None,
        processed_path=None,
        rejected_path=None,
    )


def _run(scenario: str, run_log, tmp_path: Path) -> list[Path]:
    """Run the real pipeline and return the analysis artifacts it wrote."""
    files = SCENARIOS[scenario]
    output_dir = tmp_path / "out"
    sources = []
    for name, content in files.items():
        source = tmp_path / name
        source.write_text(content, encoding="utf-8")
        sources.append(source)

    if len(sources) == 1:
        process_file(SimpleNamespace(), run_log, sources[0], _args(output_dir))
    else:
        run_batch(sources, SimpleNamespace(), run_log, _args(output_dir))

    return sorted(p for p in output_dir.iterdir() if p.name.endswith(".analysis.json"))


# ---------------------------------------------------------------------------
# BPT-001: byte-identical golden artifacts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_analysis_states_the_facts_it_is_read_for(run_log,
    scenario: str, tmp_path: Path
) -> None:
    """The analysis says what it observed, completely and without truncating.

    This replaced a byte-for-byte comparison against a stored golden. Nothing
    in production depends on the artifact's bytes being stable across versions:
    `_verify_lineage_artifacts` compares an artifact against the
    analysis_artifact_sha256 recorded *beside it*, so a new run writes new bytes
    and records the matching hash, and an older lineage record still matches the
    artifact it points at. The digest is an integrity pairing, not a
    cross-version contract -- so freezing every serialization detail of a large
    JSON blob only made a deliberate change to datatype naming look like a
    failure.
    """
    produced = _run(scenario, run_log, tmp_path)

    assert produced, f"{scenario} produced no analysis artifact"
    for artifact in produced:
        analysis = json.loads(artifact.read_text(encoding="utf-8"))

        # The file type it is about, and the sources it covers.
        assert analysis["signature"]
        assert analysis["layout"] == "delimited"
        assert analysis["matching_files"] == sorted(SCENARIOS[scenario])
        assert len(analysis["inspection_artifacts"]) == len(SCENARIOS[scenario])

        # Every observed row and pattern accounted for, nothing dropped.
        completeness = analysis["completeness"]
        assert completeness["bounded"] is True
        assert completeness["truncated"] is False
        for section in ("ordered_context", "patterns"):
            counts = completeness[section]
            assert counts["omitted"] == 0
            assert counts["included"] == counts["observed"]
            assert counts["observed"] > 0

        # The header is stated once, normalized, and every pattern record
        # carries the structural facts a reader groups on.
        assert analysis["normalized_headers"]
        for record in analysis["structural_patterns"]["records"]:
            assert record["structural_signature"]
            assert record["occurrence_count"] >= 1
            assert record["descriptor"]["field_count"] >= 0


def test_two_sources_of_one_file_type_share_one_analysis(run_log, tmp_path: Path) -> None:
    """The cross-file grouping the extraction must not turn per-file."""
    produced = _run("multi_file", run_log, tmp_path)

    assert len(produced) == 1
    analysis = json.loads(produced[0].read_text(encoding="utf-8"))
    assert analysis["matching_files"] == ["feb.csv", "jan.csv"]
    assert len(analysis["inspection_artifacts"]) == 2


def test_the_artifact_is_reproducible(run_log, tmp_path: Path) -> None:
    """Deterministic, and free of anything environment-specific.

    A golden is only meaningful if the artifact does not embed the directory it
    was produced in or the moment it was produced.
    """
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _run("single_file", run_log, first_dir)[0].read_bytes()
    second = _run("single_file", run_log, second_dir)[0].read_bytes()

    assert first == second
    text = first.decode("utf-8")
    assert str(tmp_path) not in text
    assert "/var/folders" not in text and "/tmp" not in text


# ---------------------------------------------------------------------------
# BPT-002: the digest recorded in the manifest
# ---------------------------------------------------------------------------

def test_the_recorded_digest_matches_the_artifact_bytes(run_log, tmp_path: Path) -> None:
    """What the manifest claims and what is on disk must agree."""
    from rey_lib.encryption import sha256_bytes

    produced = _run("single_file", run_log, tmp_path)[0]
    manifest = json.loads(
        (produced.parent / "file_type_analysis_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    entry = next(
        item
        for item in manifest["file_types"]
        if item["analysis_artifact"] == produced.name
    )

    assert entry["analysis_artifact_sha256"] == sha256_bytes(produced.read_bytes())
    assert entry["analysis_artifact_bytes"] == produced.stat().st_size


# ---------------------------------------------------------------------------
# BPT-003: all ten limits, including the six this SGC never documented
# ---------------------------------------------------------------------------

DOCUMENTED_LIMITS = {
    "_MAX_PATTERN_RECORDS": 1_000,
    "_MAX_REPRESENTATIVES_PER_PATTERN": 3,
    "_MAX_ORDERED_CONTEXT_RECORDS": 2_000,
    "_MAX_ANALYSIS_BYTES": 2_000_000,
}

UNDOCUMENTED_LIMITS = {
    "_COMMON_PATTERN_RESERVE": 10,
    "_CONTEXT_RADIUS": 2,
    "_MAX_EVIDENCE_TEXT_BYTES": 1_024,
    "_MAX_REPRESENTATIVE_TEXT_BYTES": 512,
    "_MAX_REPRESENTATIVE_FIELDS": 20,
    "_MAX_FIELD_TEXT_BYTES": 128,
}


# The limits moved to rey_lib with the reduction they bound, and are now
# readable through a public view instead of private constants.
LIMIT_FIELDS = {
    "_MAX_PATTERN_RECORDS": "max_pattern_records",
    "_MAX_REPRESENTATIVES_PER_PATTERN": "max_representatives_per_pattern",
    "_MAX_ORDERED_CONTEXT_RECORDS": "max_ordered_context_records",
    "_MAX_ANALYSIS_BYTES": "max_analysis_bytes",
    "_COMMON_PATTERN_RESERVE": "common_pattern_reserve",
    "_CONTEXT_RADIUS": "context_radius",
    "_MAX_EVIDENCE_TEXT_BYTES": "max_evidence_text_bytes",
    "_MAX_REPRESENTATIVE_TEXT_BYTES": "max_representative_text_bytes",
    "_MAX_REPRESENTATIVE_FIELDS": "max_representative_fields",
    "_MAX_FIELD_TEXT_BYTES": "max_field_text_bytes",
}


@pytest.mark.parametrize(
    ("name", "value"), sorted({**DOCUMENTED_LIMITS, **UNDOCUMENTED_LIMITS}.items())
)
def test_every_limit_keeps_its_value(name: str, value: int) -> None:
    """Ten limits govern the output. Four are in the SGC; six are not.

    All ten are pinned, because a limit that changes silently changes every
    artifact produced afterwards. Both the public view and the constant the
    reduction actually reads are checked, so they cannot drift apart.
    """
    from rey_lib.profiling import ANALYSIS_LIMITS
    from rey_lib.profiling import structural_analysis

    assert getattr(ANALYSIS_LIMITS, LIMIT_FIELDS[name]) == value
    assert getattr(structural_analysis, name) == value


def test_file_operator_no_longer_holds_the_limits() -> None:
    """They belong to the reduction, and the reduction is not here."""
    for name in LIMIT_FIELDS:
        assert not hasattr(inspection_module, name), name


def test_the_published_limits_block_is_unchanged(run_log, tmp_path: Path) -> None:
    """The four limits the artifact itself advertises."""
    analysis = json.loads(_run("single_file", run_log, tmp_path)[0].read_text(encoding="utf-8"))

    assert analysis["analysis_limits"] == {
        "max_ordered_context_records": 2_000,
        "max_pattern_records": 1_000,
        "max_representatives_per_pattern": 3,
        "max_serialized_bytes": 2_000_000,
    }


def test_representatives_per_pattern_are_capped(run_log, tmp_path: Path) -> None:
    """A limit crossed by real input, not only asserted as a constant."""
    source = tmp_path / "repeated.csv"
    source.write_text("A,B\n" + "".join(f"{i},x\n" for i in range(50)), encoding="utf-8")
    output_dir = tmp_path / "out"
    process_file(SimpleNamespace(), run_log, source, _args(output_dir))

    analysis = json.loads(
        next(
            p for p in output_dir.iterdir() if p.name.endswith(".analysis.json")
        ).read_text(encoding="utf-8")
    )
    for pattern in analysis["structural_patterns"]["records"]:
        assert len(pattern.get("representatives", [])) <= 3


def test_completeness_reports_what_was_omitted(run_log, tmp_path: Path) -> None:
    """Completeness metadata is part of the contract, not decoration."""
    analysis = json.loads(_run("varied_structure", run_log, tmp_path)[0].read_text("utf-8"))
    completeness = analysis["completeness"]

    assert set(completeness) == {"bounded", "ordered_context", "patterns", "truncated"}
    for section in ("ordered_context", "patterns"):
        assert set(completeness[section]) == {"included", "observed", "omitted"}
        assert (
            completeness[section]["included"] + completeness[section]["omitted"]
            == completeness[section]["observed"]
        )


# ---------------------------------------------------------------------------
# The second caller
# ---------------------------------------------------------------------------

def test_the_feeds_caller_produces_an_analysis_too(run_log, tmp_path: Path) -> None:
    """feeds.py is an independent entry into the same path.

    It reaches the analysis through write_inspection_artifact and the file-type
    artifact rather than through processor, so covering only
    layouts.delimited.process would leave it unverified.
    """
    from rey_lib.files.feed_config import FeedConfig, FeedGroup
    from rey_lib.files.feeds import run_feed_groups

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "feed.csv").write_text("ID,NAME\n12345,SMITH\n", encoding="utf-8")

    feed = FeedGroup(
        name="daily",
        file_pattern="*.csv",
        profile_mode="single_file",
        combine_strategy=None,
    )
    feed_args = argparse.Namespace(
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
    status = run_feed_groups(
        SimpleNamespace(),
        run_log,
        feed_args,
        FeedConfig(feeds=[feed]),
        inbox=inbox,
        output_dir=tmp_path / "out",
        processed_path=tmp_path / "processed",
        rejected_path=tmp_path / "rejected",
    )

    assert status == 0
    analyses = [
        p for p in (tmp_path / "out").iterdir() if p.name.endswith(".analysis.json")
    ]
    assert len(analyses) == 1
    analysis = json.loads(analyses[0].read_text(encoding="utf-8"))
    assert analysis["matching_files"] == ["feed.csv"]
