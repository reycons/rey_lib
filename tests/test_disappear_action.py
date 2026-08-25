"""`disappear` is an ordinary lifecycle fact, not a rollback compensation.

A governed file that was present and is now absent. The action says that and
nothing more -- not who removed it, not whether it was meant. It exists because
the vocabulary had no way to record it, and a writer reaching for the nearest
thing would have found only compensations:

    remove_record        undoes record_only
    delete_created_file  undoes create

Both are what a *rollback* does. Recording an ordinary disappearance with either
would file a normal lifecycle event as the reversal of something.

`delete` is not this one either. It means the system deleted the file and can
put it back, which is why it compensates to `restore_recoverable_file`.

The rollback behaviour is the part worth pinning. `_resolved_compensation`
raises KeyError for an unregistered action, so an unregistered `disappear`
would crash the first rollback that met one.
"""

from __future__ import annotations

import pytest

from rey_lib.files.log_run_rollback import (
    _COMPENSATIONS,
    _resolved_compensation,
    serialize_source_file_mutation,
)


class TestItIsPartOfTheVocabulary:
    """Registered, so rollback can resolve it."""

    def test_disappear_is_registered(self) -> None:
        assert "disappear" in _COMPENSATIONS

    def test_an_unregistered_action_would_have_crashed_rollback(self) -> None:
        """The reason registration is not optional."""
        with pytest.raises(KeyError):
            _resolved_compensation({"action": "vanished_somehow"})


class TestRollingItBackRestoresNothing:
    """Undoing the run that noticed a disappearance cannot un-disappear a file."""

    def test_it_removes_the_record(self) -> None:
        compensation = _resolved_compensation({"action": "disappear"})

        assert compensation.compensating_action == "remove_record"

    def test_it_touches_no_file(self) -> None:
        compensation = _resolved_compensation({"action": "disappear"})

        assert compensation.validate({}) is None
        assert compensation.execute({}) == {}

    def test_disappear_removes_the_record_and_nothing_else(self) -> None:
        """What the action exists to say, and what reversing it can do.

        `disappear` means the file is gone and there is nothing to put back,
        so reversing it removes the record and touches no file.

        `delete` is not registered at all. It would mean the system removed the
        file and could restore it, but no producer in this estate writes one and
        nothing ever preserved a recoverable copy, so there is no compensation
        to register.
        """
        assert _COMPENSATIONS["disappear"].compensating_action == "remove_record"
        assert "delete" not in _COMPENSATIONS


class TestItSerializesAsAnOrdinaryMutation:
    """No special shape. It is a mutation like any other."""

    def test_a_disappearance_records_as_a_mutation(self) -> None:
        record = serialize_source_file_mutation(
            action="disappear",
            status="succeeded",
            source_path="/src/inbox/gone.csv",
            destination_path="",
            recovery_path="",
            previous_version_path="",
            run_log_id=3,
            application_name="file_operator",
            file_id="17",
        )

        assert record["action"] == "disappear"
        assert record["record_type"] == "source_file_mutation"
        assert record["evidence"]["run_log_id"] == 3

    def test_it_says_nothing_about_why(self) -> None:
        """Externally, manually, or by something else -- the action is silent."""
        record = serialize_source_file_mutation(
            action="disappear",
            status="succeeded",
            source_path="/src/inbox/gone.csv",
            destination_path="",
            recovery_path="",
            previous_version_path="",
            run_log_id=3,
            application_name="file_operator",
            file_id="17",
        )

        assert "rollback" not in record
        assert record.get("result") is None or "reason" not in record["result"]
