"""One load definition, and what happens to a batch when one file fails.

`load_files` had NO test coverage before this — the suite passing said
nothing about its loop. These are the rules that only show themselves in
production, on the hundredth file of a run nobody is watching:

    a batch is an AGGREGATE and may partially complete
    a FILE fault fails one file; the rest are still attempted
    a RUN-level fault stops everything, rather than failing N files identically

The third is the one worth the most care. A missing destination table would
otherwise be reported a hundred times as a hundred separate failures, filling
a rejected folder with good files, when it is one misconfiguration.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.load.configured_load import ConfiguredLoad
from rey_lib.data.data_transform import IdentityTransform


def _files(tmp_path: Path, *names: str) -> Path:
    for name in names:
        (tmp_path / name).write_text("a,b\n1,x\n", encoding="utf-8")
    return tmp_path


#: Where this definition writes. Held by the definition now, so every file in
#: the feed is written to one object rather than each resolving its own.
_TARGET = DatabaseObjectIdentity(
    connection="c", catalog="", schema="s", name="t",
)


def _configured(tmp_path: Path, load_one, **kwargs) -> ConfiguredLoad:
    return ConfiguredLoad(
        source_dir=tmp_path,
        pattern="*.csv",
        load_one_file=load_one,
        transform=IdentityTransform(),
        loader=SimpleNamespace(),          # not reached: load_one is injected
        target=_TARGET,
        name="a_feed.a_load",
        **kwargs,
    )


class TestSelection:
    """Which files this definition takes, and which it merely saw."""

    def test_it_takes_the_files_matching_its_pattern(self, tmp_path: Path) -> None:
        _files(tmp_path, "a.csv", "b.csv")
        (tmp_path / "c.txt").write_text("no", encoding="utf-8")

        selected = _configured(tmp_path, lambda *_a, **_k: 0).select_files()

        assert [path.name for path in selected] == ["a.csv", "b.csv"]

    def test_the_cap_limits_what_is_TAKEN(self, tmp_path: Path) -> None:
        """max_files_per_run belongs to the definition, not to the caller."""
        _files(tmp_path, "a.csv", "b.csv", "c.csv")

        selected = _configured(tmp_path, lambda *_a, **_k: 0,
                               max_files=2).select_files()

        assert len(selected) == 2

    def test_discovery_records_every_file_FOUND_not_every_file_taken(
        self, tmp_path: Path
    ) -> None:
        """What was there and what was processed are different facts.

        A file left behind by the cap was still delivered, and a run log that
        only recorded the taken ones would make it look as though it never
        arrived.
        """
        _files(tmp_path, "a.csv", "b.csv", "c.csv")
        seen: list = []

        taken = _configured(tmp_path, lambda *_a, **_k: 0,
                            max_files=1).select_files(seen.append)

        assert len(seen) == 3
        assert len(taken) == 1


class TestTheBatchIsAnAggregate:
    """Partial completion is a valid outcome, not a failure of the batch."""

    def test_every_selected_file_is_loaded_and_the_rows_are_summed(
        self, tmp_path: Path
    ) -> None:
        _files(tmp_path, "a.csv", "b.csv", "c.csv")
        loaded: list = []

        total = _configured(
            tmp_path,
            lambda source, *_a, **_k: (loaded.append(source.path.name), 10)[1],
        ).load(object())

        assert total == 30
        assert loaded == ["a.csv", "b.csv", "c.csv"]

    def test_a_FILE_fault_fails_one_file_and_the_rest_still_run(
        self, tmp_path: Path
    ) -> None:
        """The per-file step reports 0 rather than raising, and that is why.

        96 succeeded, 3 failed, 1 rejected is the outcome a real run has. The
        96 stay loaded and the files after the failure are still attempted.
        """
        _files(tmp_path, "a.csv", "b.csv", "c.csv")
        attempted: list = []

        def _load_one(source, *_args, **_kwargs) -> int:
            attempted.append(source.path.name)
            return 0 if source.path.name == "b.csv" else 5

        total = _configured(tmp_path, _load_one).load(object())

        assert attempted == ["a.csv", "b.csv", "c.csv"]
        assert total == 10                      # a and c, not b

    def test_a_RUN_level_fault_stops_the_batch(self, tmp_path: Path) -> None:
        """A ConfigError is NOT caught, and that is the whole point.

        A missing destination would fail every file identically. Letting it
        through the loop would turn one misconfiguration into a hundred
        per-file failures and route a hundred good files to rejected.
        """
        _files(tmp_path, "a.csv", "b.csv", "c.csv")
        attempted: list = []

        def _load_one(source, *_args, **_kwargs) -> int:
            attempted.append(source.path.name)
            raise ConfigError("destination s.t does not exist")

        with pytest.raises(ConfigError):
            _configured(tmp_path, _load_one).load(object())

        assert attempted == ["a.csv"], "the batch continued past a run-level fault"

    def test_an_unexpected_database_error_also_stops_the_batch(
        self, tmp_path: Path
    ) -> None:
        """Only a fault the per-file step CHOSE to absorb becomes a 0.

        Anything reaching the loop unhandled is not a per-file verdict, and
        swallowing it here would invent one.
        """
        _files(tmp_path, "a.csv", "b.csv")

        def _load_one(_conn, _log, _path, **_kwargs) -> int:
            raise DatabaseError("connection lost")

        with pytest.raises(DatabaseError):
            _configured(tmp_path, _load_one).load(object())


class TestOneConstructionPath:
    """One definition means one graph, and every file executes through it."""

    def test_every_file_is_handed_the_SAME_transform_and_loader(
        self, tmp_path: Path
    ) -> None:
        """Not rebuilt per file, and not rebuilt by the per-file step.

        This object held a transform and a loader that nothing read: the
        per-file step re-read the same configuration and built its own. Two
        construction paths from one definition, looking identical and free to
        drift. Identity is asserted, because equality would pass for two
        objects built the same way and miss exactly that.
        """
        _files(tmp_path, "a.csv", "b.csv", "c.csv")
        handed: list = []

        def _load_one(_source, transform, _target, **objects) -> int:
            handed.append((transform, objects["loader"]))
            return 0

        configured = _configured(tmp_path, _load_one)
        configured.load(object())

        assert len(handed) == 3
        assert all(t is configured.transform for t, _l in handed)
        assert all(l is configured.loader for _t, l in handed)

    def test_the_SOURCE_OBJECT_is_handed_over_built(self, tmp_path: Path) -> None:
        """Built here, from the definition's rule -- not the rule itself.

        THE PREMISE INVERTED. This definition used to hand over a BUILDER, so
        the per-file step constructed the endpoint it was supposed to be
        given. Now the endpoint arrives as an object, and the definition's
        format settings are what built it -- which is still why every file in
        a feed is read the same way.
        """
        _files(tmp_path, "a.csv")
        handed: list = []

        configured = _configured(
            tmp_path,
            lambda source, *_a, **_k: handed.append(source) or 0,
            file_type="JSONL", encoding="utf-8",
        )
        configured.load(object())

        assert handed[0].path.name == "a.csv"
        assert handed[0].file_type == "JSONL"
        # The definition's setting, not the suffix's -- a .csv read as JSONL
        # proves the rule that built it was this definition's.
        assert handed[0].encoding == "utf-8"

    def test_the_TARGET_is_the_definition_s_and_the_same_for_every_file(
        self, tmp_path: Path
    ) -> None:
        """One destination object per definition, not one per file.

        Identity, not equality: two identities built from the same config
        would compare equal and hide a second resolution per file.
        """
        _files(tmp_path, "a.csv", "b.csv")
        handed: list = []

        configured = _configured(
            tmp_path,
            lambda _s, _t, target, **_k: handed.append(target) or 0,
        )
        configured.load(object())

        assert len(handed) == 2
        assert all(target is configured.target for target in handed)


class TestTheBoundary:
    """What this object interprets, and what it refuses to."""

    def test_it_builds_data_files_from_the_definitions_settings(
        self, tmp_path: Path
    ) -> None:
        """Every file in a feed is read the same way.

        The format comes from the definition, so nothing re-reads
        configuration per file — which is what "config is resolved once" has
        to mean in practice.
        """
        _files(tmp_path, "a.csv")
        configured = _configured(tmp_path, lambda *_a, **_k: 0,
                                 file_type="JSONL", encoding="utf-8")

        built = configured.data_file_for(tmp_path / "a.csv")

        assert built.file_type == "JSONL"
        assert built.encoding == "utf-8"

    def test_it_interprets_no_configuration_itself(self) -> None:
        """It RECEIVES resolved values; it does not read ctx or parse one.

        The construction boundary is above this object. Asserted structurally
        because reaching for config here is a one-import mistake that would
        put interpretation back below the boundary.
        """
        from pathlib import Path as _Path

        import rey_lib.load.configured_load as module

        source = _Path(module.__file__).read_text(encoding="utf-8")

        assert "_parse_destination" not in source
        assert "rey_lib.config" not in source
        assert "ctx." not in source

    def test_it_does_not_import_the_procedural_loader(self) -> None:
        """The per-file step is INJECTED, not imported.

        That step still owns movements, run logging and failure routing —
        application concerns belonging to the caller. Importing them would
        close a cycle and put this object in charge of things it does not own.
        """
        from pathlib import Path as _Path

        import rey_lib.load.configured_load as module

        assert "file_loader" not in _Path(module.__file__).read_text(
            encoding="utf-8"
        )
