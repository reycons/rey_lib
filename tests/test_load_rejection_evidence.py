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

#: Where these loads write. The per-file step takes a target object now.
_TARGET = DatabaseObjectIdentity(
    connection="c", catalog="", schema="schema", name="table",
)


class _Adapter:
    """A destination that exists and has two columns."""

    def supports_provider_capability(self, _conn, _capability) -> bool:
        """No native execution path, so the materialised one runs.

        Answered rather than absent: the loader asks every adapter
        this, and a double that omitted it would fail on the question
        rather than on what the test is about.
        """
        return False

    def table_exists(self, *_args, **_kwargs) -> bool:
        return True

    def get_table_columns(self, *_args, **_kwargs) -> list[str]:
        return ["a", "b"]

    def create_staging_table_if_not_exists(self, *_args, **_kwargs) -> bool:
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
        assert recorded["message"] == "Header mismatch"
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
        assert recorded["message"] == (
            "Record keys do not match the destination columns"
        )

    def test_the_recorded_message_stays_SHORT(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The detail belongs in the log, not in the recorded evidence.

        Both checks emit a full diagnosis -- which columns were missing, which
        were unexpected -- through the logger, exactly as the validators did
        before they moved. Putting that text on the exception instead would
        have changed what the run log stores, which is the sort of drift a
        move like this makes easy and nobody notices.
        """
        source = tmp_path / "wrong.csv"
        source.write_text("a,WRONG\n1,x\n", encoding="utf-8")

        recorded = _recorded(tmp_path, monkeypatch, run_log, source, "CSV")

        assert "\n" not in recorded["message"]
        assert "WRONG" not in recorded["message"]

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
