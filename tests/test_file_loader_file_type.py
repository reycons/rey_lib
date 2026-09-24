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

from rey_lib.files import file_loader, file_utils
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.files.data_file import data_file_for

#: Where these loads write. The per-file step takes a target object now.
_TARGET = DatabaseObjectIdentity(
    connection="c", catalog="", schema="schema", name="table",
)


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
        SimpleNamespace(table_exists=lambda *_a, **_k: True,
                            get_table_columns=lambda *_a, **_k: ["a", "b"]),
    )

    def _reader(_path, **kwargs):
        spy.append(kwargs.get("file_type"))
        raise _Stop

    # Patched at the DEFINITION site rather than at file_loader's binding of
    # it. A migrated format now reaches the reader through its DataFile
    # subtype, an unmigrated one still through file_loader -- and this seam
    # catches both, which the call-site patch no longer could. The assertions
    # below are unchanged: what is checked is still the file_type the reader
    # was handed.
    monkeypatch.setattr(file_utils, "get_reader", _reader)
    monkeypatch.setattr(
        file_loader, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(
            handle=lambda: SimpleNamespace(
                commit=lambda: None, rollback=lambda: None,
            )
        ),
    )

    with pytest.raises(_Stop):
        file_loader._load_one_file(
            data_file_for(
                _csv(tmp_path),
                file_type=getattr(transform_cfg, "file_type", "") or "CSV",
                encoding=getattr(transform_cfg, "encoding", "utf-8-sig"),
            ),
            None,
            _TARGET,
            ctx=_ctx(), run_log=None,
            transform_cfg=transform_cfg,
            load_cfg=SimpleNamespace(
                movements=SimpleNamespace(failure=[], success=[])),
            paths=SimpleNamespace(),
        )


class TestTheConfiguredTypeReachesTheReader:
    """The defect, and the two states that prove it is gone."""

    def test_a_declared_type_selects_the_source_object(
        self, tmp_path: Path
    ) -> None:
        """The discriminating case: the load saw 'CSV' before this was fixed.

        It cannot see anything now. The defect was a literal ``"CSV"`` inside
        the per-file step, and that step no longer reads a file_type at all --
        it is HANDED a source object. So the declared type is honoured where
        the object is built, which is what this asserts.

        DELIMITED_NO_HEADER is the discriminating token because it is the one
        a wrong default would silently mis-read: as CSV, its first data row
        would become the column names.
        """
        path = _csv(tmp_path)

        declared = data_file_for(path, file_type="DELIMITED_NO_HEADER")
        inferred = data_file_for(path)

        assert declared.file_type == "DELIMITED_NO_HEADER"
        assert inferred.file_type == "CSV"
        # And the two genuinely read the file differently -- the proof the
        # token is not cosmetic. The .csv suffix says CSV; the declaration
        # says every line is data.
        assert len(declared.read()) == len(inferred.read()) + 1

    def test_the_transfer_step_names_no_format_at_all(self) -> None:
        """Why the original defect cannot recur where it happened.

        A literal format name inside the per-file step is what discarded the
        declared type. The step takes a source object now, so there is nothing
        left there to name a format -- asserted on the source text, because a
        behavioural test cannot prove the absence of a branch.
        """
        from pathlib import Path as _Path

        import rey_lib.files.file_loader as loader_module

        text = _Path(loader_module.__file__).read_text(encoding="utf-8")
        step = text[text.index("def _load_one_file("):]
        step = step[: step.index("\ndef ")]

        # CODE ONLY. The comments explain which formats were removed and have
        # to name them to do it; a guard that tripped on its own explanation
        # would read as noise and eventually be deleted.
        body = "\n".join(
            line for line in step.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

        for token in ("CSV", "JSONL", "XLSX", "DELIMITED"):
            assert token not in body, f"{token} named inside the transfer step"

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
            SimpleNamespace(table_exists=lambda *_a, **_k: True,
                            get_table_columns=lambda *_a, **_k: ["a", "b"]),
        )

        # The refusal is the REGISTRY's now: an unknown token names no
        # DataFile, so building the source is where it is caught. That is the
        # same boundary moving with the object, not a weaker check.
        with pytest.raises(ValueError) as raised:
            data_file_for(_csv(tmp_path), file_type="NO_SUCH_FORMAT")

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


class TestSupportedFileTypes:
    """What a load accepts, read from the two sources that dispatch on it.

    The dropdown a reader picks a format from is built from this, so a format
    added to either source must appear without this being maintained -- and a
    format that is NOT accepted must not be offered, which is what a
    hand-written list eventually gets wrong.
    """

    def test_it_is_the_registry_and_nothing_beside_it(self) -> None:
        """It used to be the registry UNION a deny-list. The union is gone.

        Only a data object crosses the load boundary, so a format with no
        DataFile is not loadable -- and must not be offered as though it
        were. What a load accepts and what the registry implements are now
        the same set, which is what the union was always meant to converge to.
        """
        from rey_lib.files.data_file import registered_formats
        from rey_lib.files.file_loader import supported_file_types

        assert set(supported_file_types()) == set(registered_formats())

    def test_it_is_sorted_and_free_of_duplicates(self) -> None:
        from rey_lib.files.file_loader import supported_file_types

        answered = supported_file_types()
        assert answered == sorted(set(answered))

    def test_it_holds_the_formats_the_load_path_actually_dispatches_on(self) -> None:
        """A format silently dropped from either source fails here.

        A SUBSET, not an equality. The registry is process-global and mutable:
        ``@data_file`` writes into it at import, so another module registering
        a subtype -- as a test in this suite does -- is visible to every later
        reader. Asserting equality would make this fail on somebody else's
        fixture while proving nothing more about the formats that matter.
        """
        from rey_lib.files.file_loader import supported_file_types

        assert {
            "CSV", "DELIMITED_HEADER", "DELIMITED_NO_HEADER",
            "JSON", "JSONL", "NDJSON",
        } <= set(supported_file_types())
        # XLSX IS NOT OFFERED, and that is the point rather than an omission.
        # It has no DataFile, so it cannot be loaded directly; it is converted
        # to CSV upstream and the CSV loads like any other. Offering it here
        # would advertise a format that fails when chosen.
        assert "XLSX" not in set(supported_file_types())

    def test_data_file_still_reaches_nothing_in_the_loader(self) -> None:
        """THE REASON THE ACCESSOR IS ON THIS SIDE.

        The DataFile extraction ended with "the scaffold is gone: zero edges
        from the package to file_loader". An accessor in `data_file` reaching
        back into the loader would create the first edge the wrong way and
        close a cycle, so the direction is asserted rather than left to a
        later convenience import.
        """
        from pathlib import Path

        import rey_lib.files.data_file as package

        root = Path(package.__file__).parent
        for source in root.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            assert "file_loader" not in text, f"{source.name} reaches back"
