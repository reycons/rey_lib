"""What the run log records when a file is rejected.

The structural checks moved out of ``file_loader`` and into the DataFile
subtypes. That move is only safe if the EVIDENCE is unchanged: a run log is
read later, by someone asking why a delivery did not land, and silently
renaming what a rejection is called would strand every saved query and every
operator's memory of it.

So this asserts the recorded values rather than merely that a rejection
happened. A test checking only ``loaded == 0`` would have passed while the
validation_name changed underneath it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.load import load_operation
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.files.data_file import data_file_for
from tests.support.connector_double import ConnectorPrimitives

#: Where these loads write. The per-file step takes a target object now.
_TARGET = DatabaseObjectIdentity(
    connection="c", catalog="", schema="schema", name="table",
)


class _Adapter(ConnectorPrimitives):
    """A destination that exists and has two columns."""

    def supports_provider_capability(self, _conn, _capability) -> bool:
        """No native execution path, so the materialised one runs.

        Answered rather than absent: the loader asks every adapter
        this, and a double that omitted it would fail on the question
        rather than on what the test is about.
        """
        return False

    def has_table(self, *_args, **_kwargs) -> bool:
        return True

    def get_table_columns(self, *_args, **_kwargs) -> list[str]:
        return ["a", "b"]

    def record_create(self, *_args, **_kwargs) -> bool:
        raise AssertionError("a rejected file must not create a table")

    def bulk_insert(self, *_args, **_kwargs) -> int:
        raise AssertionError("a rejected file must not be inserted")

    def is_truncation_error(self, _exc) -> bool:
        return False


def _recorded(tmp_path: Path, monkeypatch, run_log, source: Path,
              file_type: str) -> dict:
    """Run one load to rejection and return what was recorded about it."""
    captured: dict = {}

    monkeypatch.setattr(load_operation, "_write_adapter", _Adapter())
    monkeypatch.setattr(load_operation, "execute_movements",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(
        load_operation, "log_validation_result",
        lambda _run_log, **kwargs: captured.update(kwargs),
    )

    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(
            handle=lambda: SimpleNamespace(
                commit=lambda: None, rollback=lambda: None,
            )
        ),
    )

    loaded = load_operation._load_one_file(
        data_file_for(source, file_type=file_type, encoding="utf-8"),
        None,
        _TARGET,
        ctx=SimpleNamespace(log_depth=0), run_log=run_log,
        transform_cfg=SimpleNamespace(file_type=file_type, encoding="utf-8"),
        load_cfg=SimpleNamespace(
            name="a_load",
            load=SimpleNamespace(destination_table="s.t"),
            movements=SimpleNamespace(failure=[], success=[])),
        paths=SimpleNamespace(),
    )

    assert loaded == 0
    return captured


class TestTheEvidenceSurvivedTheMove:
    """Same names, same messages, now raised by the file rather than the loader."""

    def test_a_header_mismatch_is_still_called_load_header(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The delimited case, whose check now lives in DelimitedHeaderFile."""
        source = tmp_path / "wrong.csv"
        source.write_text("a,WRONG\n1,x\n", encoding="utf-8")

        recorded = _recorded(tmp_path, monkeypatch, run_log, source, "CSV")

        assert recorded["validation_name"] == "load_header"
        assert recorded["message"].startswith("Header mismatch for 'wrong.csv'")
        assert recorded["status"] == "failed"

    def test_a_key_mismatch_is_still_called_load_record_keys(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The keyed case, whose check now lives in JsonlFile."""
        source = tmp_path / "wrong.jsonl"
        source.write_text(
            "".join(json.dumps(r) + "\n"
                    for r in ({"a": 1, "b": "x"}, {"a": 2, "c": "y"})),
            encoding="utf-8",
        )

        recorded = _recorded(tmp_path, monkeypatch, run_log, source, "JSONL")

        assert recorded["validation_name"] == "load_record_keys"
        assert recorded["message"].startswith(
            "Record keys do not match the destination columns"
        )

    def test_the_diagnosis_travels_on_the_error(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The column diagnosis is failure evidence, so it is the error's (rule 79).

        It used to go to the logger at ERROR while the recorded message stayed
        short; under rule 79 nothing but AppError writes ERROR, so the
        diagnosis -- column names, never values -- is on the DataStructureError
        and is what the run log records. Still one line.
        """
        source = tmp_path / "wrong.csv"
        source.write_text("a,WRONG\n1,x\n", encoding="utf-8")

        recorded = _recorded(tmp_path, monkeypatch, run_log, source, "CSV")

        assert "\n" not in recorded["message"]
        assert "WRONG" in recorded["message"]

    def test_the_rejected_file_reaches_the_failure_movements(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """A structural failure is a FILE fault, so the file is routed.

        Distinct from the missing-destination ConfigError, which is a run-level
        fault and deliberately leaves the file alone.
        """
        moved: list = []
        source = tmp_path / "wrong.jsonl"
        source.write_text('{"a": 1, "c": 2}\n', encoding="utf-8")

        monkeypatch.setattr(load_operation, "_write_adapter", _Adapter())
        monkeypatch.setattr(load_operation, "execute_movements",
                            lambda *args, **_k: moved.append(args))

        monkeypatch.setattr(
            load_operation, "shared_connection",
            lambda _ctx, _name: SimpleNamespace(
                handle=lambda: SimpleNamespace(
                    commit=lambda: None, rollback=lambda: None,
                )
            ),
        )

        loaded = load_operation._load_one_file(
            data_file_for(source, file_type="JSONL", encoding="utf-8"),
            None,
            _TARGET,
            ctx=SimpleNamespace(log_depth=0), run_log=run_log,
            transform_cfg=SimpleNamespace(file_type="JSONL", encoding="utf-8"),
            load_cfg=SimpleNamespace(
                name="a_load",
                load=SimpleNamespace(destination_table="s.t"),
                movements=SimpleNamespace(failure=["f"], success=[])),
            paths=SimpleNamespace(),
        )

        assert loaded == 0
        assert moved, "a structurally rejected file must run failure movements"
