"""Which schema is authoritative: the configuration, or the file.

Configuration wins where it declares one. The configured `columns:` block
describes the records a feed PRODUCES -- including fields with no physical
source at all, like constants, context values and hashes -- so where it
exists it is a stronger statement than the first record's keys.

Three answers, and the third is a refusal:

    columns: absent          -> infer from the records
    columns: LIST shape      -> authoritative, in that order
    columns: any other shape -> ConfigError

**Absent and unreadable must not collapse.** They did once: the older mapping
shape iterated to plain strings, every entry was skipped as "not a dict", and
a feed carrying 22 declared columns behaved exactly like one declaring none.
Two feeds drifted that way unnoticed until they were measured.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file import DataFileStructureError
from rey_lib.files.data_transform import IdentityTransform
from rey_lib.files.file_loader import _configured_columns

_RECORDS = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def _cfg(columns) -> SimpleNamespace:
    return SimpleNamespace(name="a_transform", columns=columns)


class TestReadingWhatConfigurationDeclares:
    """The three-way contract, at the point configuration is read."""

    def test_no_columns_block_is_None(self) -> None:
        """A load that declares no schema. The direct single-file path is one."""
        assert _configured_columns(SimpleNamespace(name="t")) is None

    def test_the_list_shape_gives_the_names_in_order(self) -> None:
        declared = _cfg([{"name": "a", "source": "A"},
                         {"name": "b", "transform": {"type": "constant"}}])

        assert _configured_columns(declared) == ["a", "b"]

    def test_the_old_MAPPING_shape_is_refused_by_name(self) -> None:
        """Not silently read as "no columns", which is how it hid.

        A config that declares a schema the loader cannot read is wrong, and
        the refusal has to say what it found or nobody can act on it.
        """
        with pytest.raises(ConfigError) as raised:
            _configured_columns(_cfg({"run_date": "Run Date"}))

        assert "dict" in str(raised.value)
        assert "a_transform" in str(raised.value)

    def test_a_malformed_ENTRY_is_refused_with_its_position(self) -> None:
        """A list of the wrong things is as unreadable as the wrong container."""
        with pytest.raises(ConfigError) as raised:
            _configured_columns(_cfg([{"name": "a"}, "not_a_mapping"]))

        assert "position 2" in str(raised.value)

    def test_ABSENT_and_UNREADABLE_are_different_answers(self) -> None:
        """The distinction this whole contract exists to make.

        Collapsing them is precisely the silence that let two live feeds
        carry declared columns while behaving as though they declared none.
        """
        assert _configured_columns(SimpleNamespace(name="t")) is None

        with pytest.raises(ConfigError):
            _configured_columns(_cfg({"a": "A"}))


class TestConfiguredColumnsAreAuthoritative:
    """When declared, they are the schema."""

    def test_they_decide_the_columns_and_the_order(self) -> None:
        transform = IdentityTransform({}, columns=["a", "b"])

        defs = transform.logical_schema(_RECORDS)

        assert [name for name, _type in defs] == ["a", "b"]

    def test_declared_types_still_win_over_inference(self) -> None:
        """Authority over the column LIST does not change type resolution."""
        transform = IdentityTransform({"a": {"type": "date"}}, columns=["a", "b"])

        defs = dict(transform.logical_schema(_RECORDS))

        assert defs["a"] == "DATE"

    def test_with_no_configured_columns_the_records_decide(self) -> None:
        """Unchanged, and what the direct single-file path relies on."""
        defs = IdentityTransform({}).logical_schema(_RECORDS)

        assert [name for name, _type in defs] == ["a", "b"]


class TestRecordsThatDoNotMatch:
    """Refused here, rather than as a database error after the table exists."""

    def test_a_missing_column_is_refused_and_named(self) -> None:
        """Left to the insert this arrives as "a row is missing column 'b'" --
        a database error, after DDL, for a configuration or drift problem."""
        transform = IdentityTransform({}, columns=["a", "b", "c"])

        with pytest.raises(DataFileStructureError) as raised:
            transform.logical_schema(_RECORDS)

        assert "missing ['c']" in str(raised.value)

    def test_an_unexpected_column_is_refused_and_named(self) -> None:
        transform = IdentityTransform({}, columns=["a"])

        with pytest.raises(DataFileStructureError) as raised:
            transform.logical_schema(_RECORDS)

        assert "unexpected ['b']" in str(raised.value)

    def test_the_SAME_columns_in_a_DIFFERENT_ORDER_are_refused(self) -> None:
        """The case the transform stage can actually produce.

        A hash column is computed last, so a hash declared anywhere but last
        reorders the output relative to the configured list. Order matters:
        a destination created in one order is later validated against a file
        header in another, and that mismatch would surface on the NEXT load
        rather than this one.
        """
        transform = IdentityTransform({}, columns=["b", "a"])

        with pytest.raises(DataFileStructureError) as raised:
            transform.logical_schema(_RECORDS)

        assert "different order" in str(raised.value)

    def test_the_refusal_is_a_FILE_fault_not_a_run_fault(self) -> None:
        """So the batch routes this file and continues.

        A malformed shape is reported as a structure error, which the loader
        already maps to movements.failure -- unlike an unreadable columns:
        block, which is a ConfigError because every file would fail alike.
        """
        transform = IdentityTransform({}, columns=["a"])

        with pytest.raises(DataFileStructureError):
            transform.logical_schema(_RECORDS)


class TestTheRealFeeds:
    """The measurement that made this switch safe, kept as a test.

    Both loader feeds were measured before the change: the column list
    derived from configuration equals the list the loader would have derived
    from the converted file. That is why switching authority is a no-op for
    what exists today, and this keeps it honest if a feed changes.
    """

    @pytest.mark.parametrize("feed", [
        "data_source.advantage.balance.yaml",
        "data_source.advantage.trade.yaml",
    ])
    def test_the_configured_columns_survive_the_transform_stages_ordering(
        self, feed: str
    ) -> None:
        """A hash column is deferred to the end when rows are built.

        So configured order equals produced order only while every hash is
        already last. Both feeds satisfy that; if one stops, this fails here
        rather than at a load.
        """
        from pathlib import Path

        from rey_lib.config import config_utils

        path = Path("/workspaces/installations/lupo/rey_loader/data_feeds") / feed
        if not path.exists():
            pytest.skip("installation config not present in this environment")

        data = config_utils.parse_yaml(path.read_text(encoding="utf-8"))
        for source in data["data_sources"]:
            for transform in source["transforms"]:
                names = [c["name"] for c in transform["columns"]]
                hashes = [c["name"] for c in transform["columns"]
                          if (c.get("transform") or {}).get("type") == "hash"]
                produced = [n for n in names if n not in hashes] + hashes

                assert names == produced, (
                    f"{feed}: a hash column is declared before the end, so the "
                    f"produced order differs from the configured order"
                )
