"""Sampling behaviour of rey_lib.files.csv."""

from __future__ import annotations

from rey_lib.files.csv import sample_indices


class TestSampleIndices:
    """sample_indices — index selection."""

    def test_small_total_returns_all(self) -> None:
        assert sample_indices(5, 10) == list(range(5))

    def test_exact_total_returns_all(self) -> None:
        assert sample_indices(10, 10) == list(range(10))

    def test_zero_size_returns_all(self) -> None:
        assert sample_indices(5, 0) == list(range(5))

    def test_first_index_included(self) -> None:
        assert 0 in sample_indices(1000, 30)

    def test_last_index_included(self) -> None:
        assert 999 in sample_indices(1000, 30)

    def test_middle_index_included(self) -> None:
        assert any(400 <= i <= 600 for i in sample_indices(1000, 30))

    def test_no_duplicates(self) -> None:
        idx = sample_indices(1000, 30)
        assert len(idx) == len(set(idx))

    def test_ascending_order(self) -> None:
        idx = sample_indices(1000, 30)
        assert idx == sorted(idx)

    def test_count_near_size(self) -> None:
        idx = sample_indices(1000, 30)
        assert 28 <= len(idx) <= 30

    def test_large_population_is_bounded_and_not_first_n(self) -> None:
        idx = sample_indices(1000, 30)
        assert len(idx) <= 30
        assert idx != list(range(30))
        assert idx == sample_indices(1000, 30)
        assert min(idx) == 0
        assert max(idx) == 999

    def test_parallel_application(self) -> None:
        """Same indices applied to lines and dicts stay aligned."""
        lines = [f"line_{i}" for i in range(1000)]
        dicts = [{"i": i}    for i in range(1000)]
        idx   = sample_indices(1000, 30)
        sampled_lines = [lines[i] for i in idx]
        sampled_dicts = [dicts[i] for i in idx]
        for line, d in zip(sampled_lines, sampled_dicts):
            assert line == f"line_{d['i']}"
