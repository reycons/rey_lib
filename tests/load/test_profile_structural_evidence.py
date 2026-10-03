"""What `structure_validated` may claim, and what it must refuse to claim.

The delimited path already compares every data row's field count against the
header's, over the whole source rather than a sample, and used to discard the
answer as a count. A profile from that path now says so.

THE CLAIM IS NARROW AND EXACT: every non-blank row in the data region carried
the header's field count. Width, and nothing else. A consumer acting on it --
and acting on it means skipping work -- may skip re-counting fields, and
nothing more.

THE TRAP THESE MOSTLY EXIST FOR: `ragged_row_count == 0` is reported when the
check RAN AND PASSED, and equally when it never ran, because
`rey_lib.files.csv` guards the comparison with `bool(expected_width)` and a
read that located no header has a width of zero. An implementation that reads
the count alone passes almost every test here and fails
`TestTheCheckMustHaveActuallyRun`.

Copied from the legacy file_operator tests and pointed at the Loader-owned
profiling, rey_lib.load.profile (row 589, step 5).
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.load import profile as workflow
from rey_lib.load.layouts.delimited import (
    build_clean_single_file_profile,
    build_csv_parts,
)

_HEADER = "asset_id,name,amount"


def _args(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        delimiter=",", encoding="utf-8", max_sample_rows=None,
        max_sample_values_per_column=None, redact_all=False,
        redact_columns=[], redact_masks={}, header_contains=None,
        skip_blank_lines=False, feed=None, profile_scope="single_file",
        source_files=[name], file_count=1,
    )


def _profile_of(tmp_path: Path, body: str):
    """Profile one delimited file exactly as the persistence path does."""
    source = tmp_path / "asset.csv"
    source.write_text(f"{_HEADER}\n{body}", encoding="utf-8")
    random.seed(20260924)
    parts = build_csv_parts(source, _args(source.name), redact_rows=False)
    return workflow._canonical_profile_structure(
        build_clean_single_file_profile(parts, _args(source.name))
    )


class TestAFileWhoseRowsAllMatch:
    """The case the evidence exists for."""

    def test_it_is_validated(self, tmp_path: Path) -> None:
        profile = _profile_of(
            tmp_path,
            "1,Alpha,10.5\n2,Beta,20.25\n3,Gamma,0\n",
        )

        assert profile.structure_validated is True

    def test_it_is_also_complete_because_a_header_declares_it(
        self, tmp_path: Path
    ) -> None:
        """The two flags answer different questions and both are true here."""
        profile = _profile_of(tmp_path, "1,Alpha,10.5\n")

        assert profile.structure_complete is True
        assert profile.structure_validated is True


class TestAFileWithARowThatDoesNotMatch:
    """Both directions, because the comparison catches both."""

    def test_a_short_row_refuses_the_claim(self, tmp_path: Path) -> None:
        profile = _profile_of(
            tmp_path,
            "1,Alpha,10.5\n2,Beta\n3,Gamma,0\n",
        )

        assert profile.structure_validated is False
        # Still COMPLETE: the header declares the structure whether or not
        # the rows honour it. Conflating the two is what this flag prevents.
        assert profile.structure_complete is True

    def test_a_long_row_refuses_it_too(self, tmp_path: Path) -> None:
        """Asserted separately -- a test for one direction would not say so."""
        profile = _profile_of(
            tmp_path,
            "1,Alpha,10.5\n2,Beta,20.25,extra\n",
        )

        assert profile.structure_validated is False


class TestTheCheckMustHaveActuallyRun:
    """THE TRAP. Zero is not one answer.

    `is_ragged` is guarded by `bool(expected_width)`, so a read that located
    no header reports zero ragged rows because it never compared anything.
    Reading the count alone would turn that into a claim of validation.
    """

    def test_no_header_columns_means_no_evidence(self) -> None:
        """The count says zero and the answer is still False."""
        assert workflow._every_row_matched_the_header(
            {"ragged_row_count": 0}, [],
        ) is False

    def test_a_located_header_with_the_same_count_is_evidence(self) -> None:
        """The control: identical count, and the header is the difference."""
        assert workflow._every_row_matched_the_header(
            {"ragged_row_count": 0}, ["a", "b"],
        ) is True


class TestAnythingUnexpectedIsNotEvidence:
    """Absence of evidence is not evidence, which is the whole flag."""

    @pytest.mark.parametrize(
        "distribution",
        [
            pytest.param({}, id="the count is absent"),
            pytest.param({"ragged_row_count": None}, id="the count is null"),
            pytest.param({"ragged_row_count": "0"}, id="the count is text"),
            pytest.param({"ragged_row_count": 0.0}, id="the count is a float"),
            pytest.param({"ragged_row_count": 3}, id="rows did not match"),
        ],
    )
    def test_it_refuses(self, distribution) -> None:
        assert workflow._every_row_matched_the_header(
            distribution, ["a", "b"],
        ) is False

    def test_a_boolean_count_is_refused_by_rule_not_by_accident(self) -> None:
        """`True` is an int, and `False == 0`.

        Rejected for being the wrong TYPE. Without that, `False` would be
        read as a count of zero and answer True -- a flag standing in for a
        count is not a count.
        """
        assert workflow._every_row_matched_the_header(
            {"ragged_row_count": False}, ["a", "b"],
        ) is False
        assert workflow._every_row_matched_the_header(
            {"ragged_row_count": True}, ["a", "b"],
        ) is False


class TestTheSampleIsNotTheSource:
    """`row_count` in the distribution is the SAMPLE, and proves nothing."""

    def test_the_sample_size_is_not_consulted(self) -> None:
        """A tiny sample over a large file still validates on the ragged count.

        `profile_rows` reports `len(rows)` over the sampled rows, while the
        whole-source count travels separately as eligible_population_rows.
        Reasoning of the form "and N rows were seen" would be reading the
        sample and calling it the file.
        """
        assert workflow._every_row_matched_the_header(
            {"ragged_row_count": 0, "row_count": 2}, ["a", "b", "c"],
        ) is True

    def test_an_absent_sample_size_changes_nothing(self) -> None:
        assert workflow._every_row_matched_the_header(
            {"ragged_row_count": 0}, ["a"],
        ) is True
