"""Sampling behaviour of rey_lib.files.csv."""

from __future__ import annotations

from unittest.mock import patch

from rey_lib.files.csv import sample_indices


class TestSampleIndices:
    """sample_indices — index selection."""

    def test_small_total_returns_all(self) -> None:
        chosen = [4, 1, 3, 0, 2]
        with patch("rey_lib.files.csv._random.sample", return_value=chosen) as sample:
            assert sample_indices(5, 10) == chosen
        sample.assert_called_once_with(range(5), 5)

    def test_exact_total_returns_all(self) -> None:
        chosen = list(reversed(range(10)))
        with patch("rey_lib.files.csv._random.sample", return_value=chosen) as sample:
            assert sample_indices(10, 10) == chosen
        sample.assert_called_once_with(range(10), 10)

    def test_zero_size_returns_all(self) -> None:
        chosen = [2, 4, 0, 3, 1]
        with patch("rey_lib.files.csv._random.sample", return_value=chosen) as sample:
            assert sample_indices(5, 0) == chosen
        sample.assert_called_once_with(range(5), 5)

    def test_no_duplicates(self) -> None:
        idx = sample_indices(1000, 30)
        assert len(idx) == len(set(idx))

    def test_does_not_restore_file_order(self) -> None:
        chosen = [999, 400, 17]
        with patch("rey_lib.files.csv._random.sample", return_value=chosen):
            assert sample_indices(1000, 3) == chosen

    def test_count_equals_size(self) -> None:
        idx = sample_indices(1000, 30)
        assert len(idx) == 30

    def test_selects_from_complete_population_without_replacement(self) -> None:
        chosen = [999, 400, 17]
        with patch("rey_lib.files.csv._random.sample", return_value=chosen) as sample:
            assert sample_indices(1000, 3) == chosen
        sample.assert_called_once_with(range(1000), 3)

    def test_does_not_force_top_middle_or_bottom_rows(self) -> None:
        chosen = list(range(100, 130))
        with patch("rey_lib.files.csv._random.sample", return_value=chosen):
            idx = sample_indices(1000, 30)
        assert 0 not in idx
        assert not any(400 <= index <= 600 for index in idx)
        assert 999 not in idx

    def test_parallel_application(self) -> None:
        """Same indices applied to lines and dicts stay aligned."""
        lines = [f"line_{i}" for i in range(1000)]
        dicts = [{"i": i}    for i in range(1000)]
        idx   = sample_indices(1000, 30)
        sampled_lines = [lines[i] for i in idx]
        sampled_dicts = [dicts[i] for i in idx]
        for line, d in zip(sampled_lines, sampled_dicts):
            assert line == f"line_{d['i']}"
