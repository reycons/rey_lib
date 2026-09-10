"""Where a run's execution cost is concentrated.

A high-cost step is materially more expensive than the *typical* step in the
same run. Not a slow one: a run of 2ms steps with one 30ms step is fast and
still has somewhere worth looking, which is why no absolute floor exists.

The classification is deterministic and says only *where* cost sits. Why a step
cost what it did is interpretation and stays downstream.
"""

from __future__ import annotations

from typing import Any

from rey_lib.logs.results_summary import (
    _HIGH_COST_MINIMUM_STEPS,
    _HIGH_COST_MULTIPLE,
    _step_timing,
)


def _steps(*durations: Any) -> list[dict[str, Any]]:
    """Step entries with the given durations; None means the step was untimed."""
    return [
        {"step_id": f"s{index}", "duration_ms": duration}
        for index, duration in enumerate(durations)
    ]


class TestTheBaseline:
    """Median, and why it has to be."""

    def test_one_dominant_step_is_reported(self) -> None:
        steps = _steps(2000, 3000, 2000, 120000)

        timing = _step_timing(steps)

        assert timing["classified"] is True
        assert timing["median_duration_ms"] == 2500.0
        assert timing["high_cost_step_ids"] == ["s3"]

    def test_two_dominant_steps_are_both_reported(self) -> None:
        """The case a mean-based baseline gets wrong.

        With a mean, two large values pull the baseline up far enough to hide
        inside it. The median is unmoved by them, so both still stand out.
        """
        steps = _steps(10, 10, 10, 10, 5000, 5000)

        timing = _step_timing(steps)

        assert timing["median_duration_ms"] == 10.0
        assert timing["high_cost_step_ids"] == ["s4", "s5"]

    def test_an_even_run_reports_nothing(self) -> None:
        timing = _step_timing(_steps(1000, 1100, 900, 1050))

        assert timing["classified"] is True
        assert timing["high_cost_step_ids"] == []

    def test_just_under_the_multiple_is_not_high_cost(self) -> None:
        """Two times the median is not three times it."""
        steps = _steps(100, 100, 100, 200)

        _step_timing(steps)

        assert steps[3]["duration_multiple_of_median"] == 2.0
        assert steps[3]["is_high_cost"] is False


class TestCostIsRelativeNotAbsolute:
    """No floor, so the rule works at any scale."""

    def test_a_thirty_millisecond_step_can_dominate(self) -> None:
        """The reason the absolute floor was removed.

        Nothing here is slow. One step is still where the cost is.
        """
        steps = _steps(2, 3, 2, 30)

        timing = _step_timing(steps)

        assert timing["high_cost_step_ids"] == ["s3"]
        assert steps[3]["is_high_cost"] is True

    def test_a_long_but_even_run_reports_nothing(self) -> None:
        """Every step takes ninety seconds. The run is slow; no step is dominant."""
        timing = _step_timing(_steps(90000, 91000, 89000, 90500))

        assert timing["high_cost_step_ids"] == []


class TestPopulation:
    """What is too little to classify, and what is excluded."""

    def test_two_steps_are_not_a_population(self) -> None:
        """Two observations say nothing about what is typical."""
        timing = _step_timing(_steps(1000, 120000))

        assert timing["classified"] is False
        assert timing["timed_steps"] == 2
        assert timing["median_duration_ms"] is None
        assert timing["high_cost_step_ids"] == []

    def test_the_minimum_is_the_declared_one(self) -> None:
        assert _HIGH_COST_MINIMUM_STEPS == 3
        assert _step_timing(_steps(1, 2, 3))["classified"] is True

    def test_an_untimed_step_is_excluded_never_zero(self) -> None:
        """Counting it as zero would drag the median down and skew everything.

        Three timed steps of 100 have a median of 100. Were the untimed step
        counted as zero, the median would fall to 100 -> and with four values
        {0,100,100,100} it becomes 100 as well; so the proof is the count.
        """
        steps = _steps(100, 100, 100, None)

        timing = _step_timing(steps)

        assert timing["timed_steps"] == 3
        assert "is_high_cost" not in steps[3]
        assert "duration_multiple_of_median" not in steps[3]

    def test_an_untimed_step_does_not_move_the_median(self) -> None:
        """Directly: the same timed steps give the same median either way."""
        with_untimed = _step_timing(_steps(10, 20, 300, None, None))
        without = _step_timing(_steps(10, 20, 300))

        assert with_untimed["median_duration_ms"] == without["median_duration_ms"]
        assert with_untimed["high_cost_step_ids"] == without["high_cost_step_ids"]

    def test_too_few_timed_steps_among_many_untimed(self) -> None:
        """Four steps, two of them timed, is still not a population."""
        timing = _step_timing(_steps(5, 5000, None, None))

        assert timing["classified"] is False
        assert timing["timed_steps"] == 2


class TestNoInventedConstants:
    """A median of zero is reported as unclassifiable, not rescued by a guess."""

    def test_a_zero_median_classifies_nothing(self) -> None:
        """Half the steps registered no measurable time.

        A ratio against zero has no meaning, and declaring every non-zero step
        dominant would be a constant invented to avoid saying so.
        """
        timing = _step_timing(_steps(0, 0, 0, 40))

        assert timing["classified"] is False
        assert timing["median_duration_ms"] == 0.0
        assert timing["high_cost_step_ids"] == []

    def test_the_multiple_is_the_declared_one(self) -> None:
        assert _HIGH_COST_MULTIPLE == 3.0
        steps = _steps(100, 100, 100, 300)
        _step_timing(steps)
        assert steps[3]["duration_multiple_of_median"] == 3.0
        assert steps[3]["is_high_cost"] is True


def test_the_classification_never_says_why() -> None:
    """It reports where cost sits. Cause is the downstream LLM's answer."""
    steps = _steps(10, 10, 10, 900)
    timing = _step_timing(steps)

    assert set(timing) == {
        "timed_steps", "median_duration_ms", "high_cost_step_ids", "classified",
    }
    assert set(steps[3]) == {
        "step_id", "duration_ms", "duration_multiple_of_median", "is_high_cost",
    }
