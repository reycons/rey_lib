"""The load stage reads the file type its transform declares.

The transform stage read the configured ``file_type`` and the load stage
forced the literal ``"CSV"`` at the reader boundary, so the contract existed
and was discarded one call before it was used -- on the same line where
``encoding`` was being read from that very config object. Every reader but
one was unreachable from a load, whatever the configuration said.

These assert the value ARRIVES, not that any particular format parses: what
``get_reader`` does with a type is tested beside ``get_reader``. Two of them
would pass against the old code and exist to pin the default; two would not.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files import file_loader


class _Stop(Exception):
    """Ends the load once the reader has been reached.

    ``_load_one_file`` catches DatabaseError alone, so this propagates and
    the test stops at the boundary it is about rather than stubbing staging,
    bulk insert and file movements to reach an end it does not assert.
    """


def _ctx() -> SimpleNamespace:
    """The least a shared boundary needs: log_enter increments log_depth."""
    return SimpleNamespace(log_depth=0)


def _csv(tmp_path: Path) -> Path:
    """Write a two-column file whose header matches the destination."""
    path = tmp_path / "source.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    return path


def _load(tmp_path: Path, monkeypatch, transform_cfg, *, spy: list) -> None:
    """Run _load_one_file far enough to record what the reader was given."""
    monkeypatch.setattr(
        file_loader, "_db_adapter",
        SimpleNamespace(get_table_columns=lambda *_a, **_k: ["a", "b"]),
    )

    def _reader(_path, **kwargs):
        spy.append(kwargs.get("file_type"))
        raise _Stop

    monkeypatch.setattr(file_loader, "get_reader", _reader)

    with pytest.raises(_Stop):
        file_loader._load_one_file(
            _ctx(), None, object(), _csv(tmp_path), transform_cfg,
            SimpleNamespace(movements=SimpleNamespace(failure=[], success=[])),
            SimpleNamespace(), "schema", "table",
        )


class TestTheConfiguredTypeReachesTheReader:
    """The defect, and the two states that prove it is gone."""

    def test_a_declared_type_is_the_one_the_reader_is_given(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The discriminating case: it saw 'CSV' before this was fixed.

        DELIMITED_NO_HEADER is used rather than XLSX because it needs no
        optional dependency -- openpyxl is absent in some environments and
        five test modules already fail collection on missing ones.
        """
        spy: list = []
        _load(tmp_path, monkeypatch,
              SimpleNamespace(file_type="DELIMITED_NO_HEADER"), spy=spy)

        assert spy == ["DELIMITED_NO_HEADER"]

    def test_an_unsupported_type_is_refused_by_the_reader(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """End to end, through the REAL get_reader, and it must raise.

        This is the sharpest proof the literal is gone: with it in place the
        file reads as CSV and nothing is raised at all. get_reader is not
        stubbed here -- that would test the test.
        """
        monkeypatch.setattr(
            file_loader, "_db_adapter",
            SimpleNamespace(get_table_columns=lambda *_a, **_k: ["a", "b"]),
        )

        with pytest.raises(ValueError) as raised:
            file_loader._load_one_file(
                _ctx(), None, object(), _csv(tmp_path),
                SimpleNamespace(file_type="NO_SUCH_FORMAT"),
                SimpleNamespace(movements=SimpleNamespace(failure=[], success=[])),
                SimpleNamespace(), "schema", "table",
            )

        assert "NO_SUCH_FORMAT" in str(raised.value)


class TestWhatMustNotMove:
    """Every load today declares nothing, so the default is the behaviour."""

    def test_a_config_declaring_csv_still_reads_csv(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The thing this change must not break."""
        spy: list = []
        _load(tmp_path, monkeypatch, SimpleNamespace(file_type="CSV"), spy=spy)

        assert spy == ["CSV"]

    def test_a_config_declaring_nothing_still_reads_csv(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """No config in the estate declares file_type.

        So the default is not a convenience -- it is what keeps every
        existing load on the path it was already taking, and it is why this
        restores a contract rather than changing behaviour.
        """
        spy: list = []
        _load(tmp_path, monkeypatch, SimpleNamespace(), spy=spy)

        assert spy == ["CSV"]
