"""One composed ask: what it holds, and what it refuses to decide.

The object a reader composes before anything is sent. What is proved here is
the boundary rather than the behaviour: it seeds once, it resolves nothing, and
it executes nothing.
"""

from __future__ import annotations

import pytest

from rey_lib.ai.objects import AIObject, AIObjectStatus


class TestItHoldsOneAsk:
    """Its own identity, and its own values."""

    def test_it_mints_an_identity(self) -> None:
        """Nothing outside has a name for an ask that has not run."""
        assert AIObject().id
        assert AIObject().id != AIObject().id

    def test_a_change_keeps_the_identity(self) -> None:
        """A changed ask is the same ask, or a surface would lose it mid-edit."""
        composed = AIObject(task="default")

        assert composed.with_settings(prompt="why?").id == composed.id

    def test_the_seed_is_not_mutated(self) -> None:
        """Frozen, so nothing can change an ask from under whoever holds it."""
        composed = AIObject(subject="a node", task="default")

        composed.with_settings(prompt="why?")

        assert composed.prompt == ""


class TestItComposesOnlyWhatAReaderComposes:
    """The door a prompt change uses is not the door status uses."""

    @pytest.mark.parametrize("setting", [
        "prompt", "task", "profile_id", "instruction_id",
        "temperature", "representation",
    ])
    def test_every_composable_setting_is_accepted(self, setting: str) -> None:
        value = 0.5 if setting == "temperature" else "x"

        assert AIObject().with_settings(**{setting: value})

    @pytest.mark.parametrize("setting", ["status", "id", "answer", "error", "subject"])
    def test_everything_else_is_refused(self, setting: str) -> None:
        """Including the subject: it is seeded once and is not composable."""
        with pytest.raises(ValueError, match="no composable setting"):
            AIObject().with_settings(**{setting: "x"})


class TestItResolvesNothing:
    """The precedence lives in one place, and this is not it."""

    def test_an_unset_override_stays_unset(self) -> None:
        """Empty means 'the task's, then the defaults'' -- which is what
        AIRequest already means by it. Filling one in here would pin a model
        that configuration would later have changed."""
        request = AIObject(task="log_interpretation").request()

        assert request.task == "log_interpretation"
        assert request.profile_id == ""
        assert request.instruction_id == ""
        assert request.options.temperature is None

    def test_an_override_is_carried_as_stated(self) -> None:
        composed = AIObject(task="default").with_settings(
            profile_id="p1", instruction_id="i1", temperature=0.2,
        )

        request = composed.request()

        assert (request.profile_id, request.instruction_id) == ("p1", "i1")
        assert request.options.temperature == 0.2

    def test_the_subject_and_the_prompt_are_both_sent(self) -> None:
        """A prompt about a subject is not a prompt instead of one."""
        composed = AIObject(subject="a node").with_settings(prompt="why?")

        sent = composed.request().input.content[0].value

        assert "a node" in sent and "why?" in sent

    def test_either_one_being_empty_is_legitimate(self) -> None:
        assert AIObject(subject="only a subject").request().input.content[0].value \
            == "only a subject"
        assert AIObject().with_settings(prompt="only a prompt") \
            .request().input.content[0].value == "only a prompt"


class TestTheOutcomeGoesWithTheRunThatProducedIt:
    """A surface must not draw the last answer beside a running state."""

    def test_starting_clears_the_previous_outcome(self) -> None:
        answered = AIObject().answered({"text": "the old answer"})

        running = answered.started()

        assert running.status == AIObjectStatus.RUNNING
        assert running.answer is None
        assert running.error == ""

    def test_failing_carries_why_and_no_answer(self) -> None:
        failed = AIObject().answered({"text": "x"}).failed("the provider refused")

        assert failed.status == AIObjectStatus.FAILED
        assert failed.answer is None
        assert "refused" in failed.error

    def test_cancelling_is_neither_a_failure_nor_an_answer(self) -> None:
        cancelled = AIObject().started().cancelled()

        assert cancelled.status == AIObjectStatus.CANCELLED
        assert cancelled.answer is None
        assert cancelled.error == ""
