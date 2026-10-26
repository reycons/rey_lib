"""The live Rollback (backlog 1181): one identity, the preview-before-execute protocol,
its declared actions and its observers -- FileManifest.rollback does the rollback."""

from __future__ import annotations

import threading

from typing import Any

import pytest

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.files.rollback import (
    COMPLETE,
    CONFIRMATION,
    EXECUTING,
    FILE_SCOPES,
    MUTATION_SCOPES,
    REQUEST,
    Rollback,
)


class _Manifest:
    """FileManifest.rollback's contract: records each call, answers the mutations it holds."""

    def __init__(self, mutations: list[int], *, failed: Any = None,
                 refusal: Exception | None = None) -> None:
        self.mutations = list(mutations)
        self.failed = failed
        self.refusal = refusal
        self.calls: list[dict[str, Any]] = []

    def rollback(self, **criteria: Any) -> dict[str, Any]:
        self.calls.append(dict(criteria))
        if self.refusal is not None:
            raise self.refusal
        dry_run = criteria["dry_run"]
        return {
            "scope": criteria["scope"],
            "dry_run": dry_run,
            "mutations": [{"file_mutation_id": one} for one in self.mutations],
            "reversed": [] if dry_run else list(self.mutations),
            "failed": None if dry_run else self.failed,
            "manifests_deleted": [],
            "boundary": None,
        }


class _Observer:
    def __init__(self) -> None:
        self.phases: list[str] = []

    def changed(self, rollback: Rollback) -> None:
        self.phases.append(rollback.phase)


def _ids(actions: list[dict[str, Any]]) -> list[str]:
    return [action["id"] for action in actions]


# -- opening -------------------------------------------------------------------


@pytest.mark.parametrize("identity", [
    {}, {"file_manifest_id": 1, "run_id": 2}, {"file_mutation_id": "", "run_id": None},
])
def test_exactly_one_identity_opens_a_rollback(identity: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        Rollback(_Manifest([]), **identity)


def test_an_identity_that_is_not_a_whole_number_is_refused() -> None:
    with pytest.raises(ConfigError):
        Rollback(_Manifest([]), file_mutation_id="seven")


@pytest.mark.parametrize("identity, scopes", [
    ({"run_id": "42"}, ("run",)),
    ({"file_manifest_id": 3}, FILE_SCOPES),
    ({"file_mutation_id": 7}, MUTATION_SCOPES),
])
def test_the_identity_decides_the_scopes_and_the_first_is_the_default(
    identity: dict[str, Any], scopes: tuple[str, ...],
) -> None:
    rollback = Rollback(_Manifest([]), **identity)
    projected = rollback.projection()
    assert projected["scopes"] == list(scopes)
    assert projected["scope"] == scopes[0]
    assert projected["phase"] == REQUEST
    assert projected["identity"] == {key: int(value) for key, value in identity.items()}


# -- the protocol --------------------------------------------------------------


def test_a_preview_is_the_manifests_dry_run_and_asks_for_confirmation() -> None:
    manifest = _Manifest([9, 8])
    rollback = Rollback(manifest, file_mutation_id=7)
    rollback.preview()
    assert manifest.calls == [{"scope": "file", "dry_run": True, "file_mutation_id": 7}]
    projected = rollback.projection()
    assert projected["phase"] == CONFIRMATION
    assert [row["file_mutation_id"] for row in projected["preview"]["mutations"]] == [9, 8]
    assert "rollback_execute" in _ids(projected["actions"])


def test_execute_is_refused_before_a_preview() -> None:
    manifest = _Manifest([9])
    rollback = Rollback(manifest, run_id=4)
    with pytest.raises(ConfigError):
        rollback.execute()
    assert manifest.calls == []
    assert "rollback_execute" not in _ids(rollback.actions())


def test_execute_applies_what_was_previewed_and_completes() -> None:
    manifest = _Manifest([9, 8])
    rollback = Rollback(manifest, file_mutation_id=7)
    rollback.preview()
    rollback.execute()
    assert [call["dry_run"] for call in manifest.calls] == [True, True, False]
    projected = rollback.projection()
    assert projected["phase"] == COMPLETE
    assert projected["result"]["reversed"] == [9, 8]
    assert projected["failure"] is None


def test_what_changed_since_the_preview_is_not_executed() -> None:
    manifest = _Manifest([9, 8])
    rollback = Rollback(manifest, file_mutation_id=7)
    rollback.preview()
    manifest.mutations = [10, 9, 8]
    rollback.execute()
    assert [call["dry_run"] for call in manifest.calls] == [True, True]
    projected = rollback.projection()
    assert projected["phase"] == REQUEST
    assert projected["preview"] is None
    assert "preview it again" in projected["failure"]


def test_a_reversal_the_manifest_could_not_do_is_held_with_the_result() -> None:
    manifest = _Manifest([9], failed={"file_mutation_id": 9, "reason": "file is gone"})
    rollback = Rollback(manifest, run_id=4)
    rollback.preview()
    rollback.execute()
    projected = rollback.projection()
    assert projected["phase"] == COMPLETE
    assert projected["failure"] == "file is gone"
    assert projected["result"]["failed"]["file_mutation_id"] == 9


@pytest.mark.parametrize("refusal", [ValueError("not among the scope's records"),
                                     AppError("control is down")])
def test_a_refused_preview_is_held_and_the_rollback_stays_requesting(
    refusal: Exception,
) -> None:
    rollback = Rollback(_Manifest([], refusal=refusal), file_mutation_id=7)
    rollback.preview()
    projected = rollback.projection()
    assert projected["phase"] == REQUEST
    assert projected["failure"] == str(refusal)
    assert projected["preview"] is None


def test_a_refused_execution_returns_to_requesting() -> None:
    manifest = _Manifest([9])
    rollback = Rollback(manifest, run_id=4)
    rollback.preview()
    manifest.refusal = AppError("control is down")
    rollback.execute()
    projected = rollback.projection()
    assert projected["phase"] == REQUEST
    assert projected["failure"] == "control is down"


def test_choosing_a_scope_discards_the_preview_and_the_next_preview_uses_it() -> None:
    manifest = _Manifest([9])
    rollback = Rollback(manifest, file_manifest_id=3)
    rollback.preview()
    rollback.choose_scope("file_type")
    assert rollback.phase == REQUEST
    assert rollback.projection()["preview"] is None
    rollback.preview()
    assert manifest.calls[-1] == {"scope": "file_type", "dry_run": True, "file_manifest_id": 3}


def test_a_scope_the_identity_does_not_offer_is_refused() -> None:
    rollback = Rollback(_Manifest([]), file_manifest_id=3)
    with pytest.raises(ConfigError):
        rollback.choose_scope("batch")
    assert rollback.scope == "file"


def test_a_run_offers_no_scope_choice() -> None:
    assert _ids(Rollback(_Manifest([]), run_id=4).actions()) == ["rollback_preview"]


# -- declared actions ----------------------------------------------------------


def test_each_action_is_a_complete_command_and_the_scope_choice_names_its_input() -> None:
    rollback = Rollback(_Manifest([9]), file_mutation_id=7)
    rollback.preview()
    actions = {action["id"]: action for action in rollback.actions()}
    assert actions["rollback_scope"]["command"] == {"command": "choose_scope"}
    assert actions["rollback_scope"]["inputs"] == {"scope": list(MUTATION_SCOPES)}
    assert actions["rollback_preview"]["command"] == {"command": "preview"}
    assert actions["rollback_execute"]["command"] == {"command": "execute"}


# -- one operation at a time and its observers ---------------------------------


def test_while_executing_nothing_is_offered_and_a_second_operation_is_refused() -> None:
    entered, release = threading.Event(), threading.Event()

    class _Slow(_Manifest):
        def rollback(self, **criteria: Any) -> dict[str, Any]:
            if not criteria["dry_run"]:
                entered.set()
                release.wait(5)
            return super().rollback(**criteria)

    rollback = Rollback(_Slow([9]), run_id=4)
    rollback.preview()
    running = threading.Thread(target=rollback.execute)
    running.start()
    assert entered.wait(5)
    assert rollback.phase == EXECUTING
    assert rollback.actions() == []
    with pytest.raises(ConfigError):
        rollback.preview()
    release.set()
    running.join(5)
    assert rollback.phase == COMPLETE


def test_observers_are_told_each_change() -> None:
    observer = _Observer()
    rollback = Rollback(_Manifest([9]), run_id=4)
    rollback.observe(observer)
    rollback.preview()
    rollback.execute()
    assert observer.phases == [CONFIRMATION, EXECUTING, COMPLETE]


def test_a_failing_observer_does_not_stop_the_rollback_or_the_next_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[str] = []
    monkeypatch.setattr(AppError, "report", lambda self, *a, **k: reported.append(str(self)))

    class _Broken:
        def changed(self, rollback: Rollback) -> None:
            raise OSError("channel closed")

    after = _Observer()
    rollback = Rollback(_Manifest([9]), run_id=4)
    rollback.observe(_Broken())
    rollback.observe(after)
    rollback.preview()
    assert rollback.phase == CONFIRMATION
    assert after.phases == [CONFIRMATION]
    assert reported == ["rollback: display observer failed"]
