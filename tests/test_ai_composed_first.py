"""The flag that says a task is composed before it is run.

The one field beside it that does **not** inherit. That
distinction is the whole reason these exist: a default scope composing
first must not make every configured task do so.
"""

from __future__ import annotations

from rey_lib.ai.settings import AISettings, AISettingsTask


class TestItDefaultsToTheBehaviourThatExistedBefore:
    """An estate that has not been told behaves exactly as it did."""

    def test_the_default_scope_is_false(self) -> None:
        assert AISettings().composed_first is False

    def test_a_task_is_false(self) -> None:
        assert AISettingsTask(name="log_interpretation").composed_first is False


class TestItDoesNotInherit:
    """The one way it differs from every other field beside it."""

    def test_a_default_that_opts_in_does_not_opt_every_task_in(self) -> None:
        """Temperature and representation inherit; this must not. A task
        inheriting it would mean configuring the default scope silently changed
        what every configured task does when it is asked for."""
        settings = AISettings(
            composed_first=True,
            tasks=(AISettingsTask(name="log_interpretation"),),
        )

        assert settings.composed_first is True
        assert settings.task("log_interpretation").composed_first is False

    def test_a_task_that_opts_in_does_not_opt_the_default_in(self) -> None:
        settings = AISettings(
            tasks=(AISettingsTask(name="tree_inspection", composed_first=True),),
        )

        assert settings.task("tree_inspection").composed_first is True
        assert settings.composed_first is False


class TestItSurvivesTheWaySettingsAreChanged:
    """Every mutation goes through `replace`, so nothing quietly drops it."""

    def test_choosing_a_profile_keeps_it(self) -> None:
        settings = AISettings(composed_first=True)

        assert settings.with_profile("p1").composed_first is True

    def test_choosing_an_instruction_keeps_it(self) -> None:
        settings = AISettings(composed_first=True)

        assert settings.with_instruction("i1").composed_first is True
