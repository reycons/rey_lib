"""#1211: the existing Run owns eligibility; the existing Rollback enforces it."""
from typing import Any

import pytest

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.files.rollback import Rollback
from rey_lib.run import AppExecute


class Control:
    def __init__(self, mutations: list[int]) -> None:
        self.mutations = mutations
        self.status = 'SUCCEEDED'
        self.recorded = True
        self.calls: list[int] = []
        self.failure: Exception | None = None

    def get_run_manifest(self, run_id: int) -> dict[str, Any] | None:
        return {'run_manifest_id': run_id, 'status': self.status} if self.recorded else None

    def request_file_rollback(self, *, run_id: int) -> list[dict[str, Any]]:
        self.calls.append(run_id)
        if self.failure:
            raise self.failure
        return [{"file_mutation_id": one} for one in self.mutations]


class Manifest:
    def __init__(self, mutations: list[int]) -> None:
        self.mutations = mutations
        self.calls: list[dict[str, Any]] = []

    def rollback(self, **criteria: Any) -> dict[str, Any]:
        self.calls.append(criteria)
        return {"mutations": [{"file_mutation_id": i} for i in self.mutations],
                "reversed": [], "failed": None}


@pytest.mark.parametrize('status', ['SUCCEEDED', 'FAILED', 'ABORTED'])
def test_terminal_run_requires_remaining_governed_mutations(status: str) -> None:
    control = Control([10])
    control.status = status
    run = AppExecute(run_id=77, control=control, status=status)
    assert run.rollback_available()
    assert run.projection(include_rollback=True)['rollback_available']
    assert control.calls == [77, 77]
    control.mutations.clear()
    assert not run.rollback_available()


@pytest.mark.parametrize('status', ['RUNNING', 'SKIPPED', 'NOT RUN', 'ROLLED_BACK', 'ROLLBACK', 'PARTIAL_ROLLBACK', ''])
def test_other_lifecycles_do_not_offer_or_query_rollback(status: str) -> None:
    control = Control([10])
    control.status = status
    run = AppExecute(run_id=77, control=control, status=status)
    assert not run.rollback_available()
    assert control.calls == []


def test_ordinary_stdout_projection_does_not_read_file_history() -> None:
    control = Control([10])
    run = AppExecute(run_id=77, control=control, status='SUCCEEDED')
    assert 'rollback_available' not in run.projection()
    assert control.calls == []


def test_evidence_read_failure_is_not_reported_as_no_available_rollback() -> None:
    control = Control([10])
    control.failure = AppError('file history unavailable')
    run = AppExecute(run_id=77, control=control, status='SUCCEEDED')
    with pytest.raises(AppError, match='file history unavailable'):
        run.projection(include_rollback=True)


def test_run_opening_requires_the_canonical_owner_and_same_identity() -> None:
    manifest = Manifest([10])
    with pytest.raises(ConfigError, match='canonical Run'):
        Rollback(manifest, run_id=77)
    with pytest.raises(ConfigError, match='same identity'):
        Rollback(manifest, run_id=77, run=AppExecute(run_id=88, control=Control([10])))


def test_unavailable_run_projects_no_controls_and_cannot_preview() -> None:
    control = Control([])
    manifest = Manifest([10])
    run = AppExecute(run_id=77, control=control, status='SUCCEEDED')
    rollback = Rollback(manifest, run_id=77, run=run)
    shown = rollback.projection()
    assert shown['run']['run_id'] == 77
    assert shown['run']['rollback_available'] is False
    assert shown['actions'] == []
    assert 'no rollback available' in shown['failure']
    rollback.preview()
    assert rollback.phase == 'request'
    assert manifest.calls == []


def test_availability_is_rechecked_before_execute_not_just_when_drawing() -> None:
    control = Control([10])
    manifest = Manifest([10])
    run = AppExecute(run_id=77, control=control, status='SUCCEEDED')
    rollback = Rollback(manifest, run_id=77, run=run)
    rollback.preview()
    assert rollback.phase == 'confirmation'
    control.mutations.clear()
    assert rollback.actions() == []
    with pytest.raises(ConfigError, match='no rollback available'):
        rollback.execute()
    assert len(manifest.calls) == 1 and manifest.calls[0]['dry_run'] is True


def test_for_control_uses_the_existing_run_owner_and_missing_run_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    control = Control([10])
    control.get_run_manifest = lambda run_id: {'run_manifest_id':run_id,'status':'SUCCEEDED'}
    monkeypatch.setattr('rey_lib.files.rollback.FileManifest',lambda control: Manifest([10]))
    rollback = Rollback.for_control(control, run_id=77)
    assert rollback.projection()['run']['rollback_available']
    control.get_run_manifest = lambda run_id: None
    with pytest.raises(ConfigError, match='canonical Run'):
        Rollback.for_control(control, run_id=77)


def test_projection_stays_responsive_without_history_reads_while_executing() -> None:
    import threading

    control = Control([10])
    entered, release = threading.Event(), threading.Event()

    class SlowManifest(Manifest):
        def rollback(self, **criteria: Any) -> dict[str, Any]:
            if not criteria['dry_run']:
                entered.set()
                assert release.wait(5)
            return super().rollback(**criteria)

    rollback = Rollback(SlowManifest([10]), run_id=77,
                        run=AppExecute(run_id=77, control=control, status='SUCCEEDED'))
    rollback.preview()
    worker = threading.Thread(target=rollback.execute)
    worker.start()
    try:
        assert entered.wait(5)
        before = list(control.calls)
        shown = rollback.projection()
        assert shown['phase'] == 'executing'
        assert shown['actions'] == []
        assert shown['failure'] is None
        assert control.calls == before
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_lifecycle_is_rechecked_before_execute() -> None:
    control = Control([10])
    manifest = Manifest([10])
    run = AppExecute(run_id=77, control=control, status='SUCCEEDED')
    rollback = Rollback(manifest, run_id=77, run=run)
    rollback.preview()
    control.status = 'RUNNING'
    with pytest.raises(ConfigError, match='no rollback available'):
        rollback.execute()
    assert all(call['dry_run'] for call in manifest.calls)


def test_invalid_run_identity_is_refused_before_reading_a_manifest() -> None:
    control = Control([10])
    for identity in ['not-an-id', 1.5]:
        with pytest.raises(ConfigError, match='whole number'):
            Rollback.for_control(control, run_id=identity)


def test_durable_lifecycle_changes_are_seen_without_reconstructing_the_run() -> None:
    control = Control([10])
    control.status = 'RUNNING'
    run = AppExecute(run_id=77, control=control, status='RUNNING')
    assert not run.rollback_available()
    control.status = 'SUCCEEDED'
    assert run.rollback_available()
    control.status = 'ROLLED_BACK'
    assert not run.rollback_available()
    control.recorded = False
    assert not run.rollback_available()
