"""The Loader's governed source-file inventory (row 589, step 1).

Copied from the legacy file_operator inventory tests and pointed at the
Loader-owned implementation: rey_lib.load.inventory and FileManifest.inventory.
Inventory reports filesystem facts only. Classification, filename
interpretation, and derived feed identity belong to later lifecycle steps and
are deliberately not exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import DatabaseError
from rey_lib.files import manifest as manifest_module
from rey_lib.files.manifest import FileManifest
from rey_lib.load.inventory import (
    InventoryError,
    enumerate_inventory_files,
    inventory_source_config,
    resolve_inventory_source_configs,
    run_source_inventory,
)


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


def _entry(tmp_path: Path, **overrides: object) -> dict:
    """Return one inventory source entry in the authoritative shape."""
    values: dict = {
        "name": "feed_inbox",
        "enabled": True,
        "source": {
            "path": f"{tmp_path}/data/*/inbox",
            "file_patterns": ["*.xlsx", "*.xls"],
            "recursive": False,
        },
        "exclusions": ["~$*", "*.tmp", "*.part", "*.crdownload", "*.lock"],
    }
    values.update(overrides)
    return values


def _process_config(*entries: object) -> dict:
    """The process: its sources. The binding that records a file is
    FileManifest.inventory's own, so the step names no insert procedure."""
    return {"sources": list(entries)}


def _inbox(tmp_path: Path, feed: str, *filenames: str) -> Path:
    """Create one feed inbox populated with the given filenames."""
    directory = tmp_path / "data" / feed / "inbox"
    directory.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        (directory / name).write_text(f"content of {name}\n", encoding="utf-8")
    return directory


def _config(tmp_path: Path, **overrides: object):
    entry = _entry(tmp_path, **overrides)
    return inventory_source_config(entry, str(entry["name"]))


def _accepted(tmp_path: Path, **overrides: object) -> list[dict]:
    return list(enumerate_inventory_files(_config(tmp_path, **overrides)).accepted)


def _rejected(tmp_path: Path, **overrides: object) -> list[dict]:
    return list(enumerate_inventory_files(_config(tmp_path, **overrides)).rejected)


class _InsertingControl:
    """A Control whose full-row inventory call records a file, as the routine does.

    The routine mints the manifest row and its baseline mutation in one call
    and answers with what it created, so this records the calls and answers the
    same way. `o_manifest_created` is a file seen for the first time;
    `o_inventory_created` is a new observation of one already governed.
    """

    def __init__(self) -> None:
        self.inserted: list[dict] = []

    def inventory_file_result(self, required=True, **values):
        self.inserted.append(dict(values))
        checksum = values.get("checksum_sha256")
        known = any(row.get("checksum_sha256") == checksum
                    for row in self.inserted[:-1])
        return {
            "o_file_manifest_id": len(self.inserted),
            "o_manifest_created": not known,
            "o_inventory_created": True,
            "o_batch_step_id": None,
        }


def _ctx(tmp_path: Path) -> SimpleNamespace:
    """A context with a durable run log and the Control inventory records through."""
    run_dir = tmp_path / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        run_log_dir=str(run_dir),
        installation=SimpleNamespace(name="test_installation"),
        app_name="rey_loader",
        name="rey_loader",
        log_depth=0,
        # The governed manifest is a table in the control database, so a
        # context that reaches it carries the Control it is reached through.
        shared_control=_InsertingControl(),
    )


def _run_log_records(run_log) -> list[dict]:
    """The records this run log wrote, read from its own path."""
    return [
        json.loads(line)
        for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _manifest_rows(ctx: SimpleNamespace) -> list[dict]:
    """What inventory recorded, as the values it handed the insert routine.

    The manifest is a table now, so this reads the calls made against it rather
    than lines of a JSONL file.
    """
    return list(ctx.shared_control.inserted)


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


def test_sources_must_be_present() -> None:
    with pytest.raises(InventoryError):
        resolve_inventory_source_configs({})


def test_sources_must_be_a_non_empty_list() -> None:
    with pytest.raises(InventoryError):
        resolve_inventory_source_configs({"sources": []})


def test_duplicate_source_names_are_rejected(tmp_path: Path) -> None:
    config = _process_config(_entry(tmp_path), _entry(tmp_path))
    with pytest.raises(InventoryError, match="duplicate"):
        resolve_inventory_source_configs(config)


def test_invalid_enabled_source_fails_before_any_enumeration(tmp_path: Path) -> None:
    """A malformed enabled source is a configuration failure, not a rejected file."""
    broken = _entry(tmp_path, name="broken", source={"path": f"{tmp_path}/data/*/inbox"})
    with pytest.raises(InventoryError, match="no source was enumerated"):
        resolve_inventory_source_configs(_process_config(_entry(tmp_path), broken))


def test_every_invalid_source_is_reported_together(tmp_path: Path) -> None:
    first = _entry(tmp_path, name="one", source={"path": f"{tmp_path}/data/*/inbox"})
    second = _entry(tmp_path, name="two", exclusions="not-a-list")
    with pytest.raises(InventoryError) as excinfo:
        resolve_inventory_source_configs(_process_config(first, second))
    assert "one:" in str(excinfo.value)
    assert "two:" in str(excinfo.value)


def test_disabled_source_is_skipped_without_validation(tmp_path: Path) -> None:
    disabled = _entry(
        tmp_path,
        name="disabled",
        enabled=False,
        source={"path": f"{tmp_path}/data/*/inbox"},
    )
    configs = resolve_inventory_source_configs(
        _process_config(_entry(tmp_path), disabled)
    )
    assert [config.name for config in configs] == ["feed_inbox"]


# ---------------------------------------------------------------------------
# Source configuration validation
# ---------------------------------------------------------------------------


def test_path_placeholders_are_rejected(tmp_path: Path) -> None:
    """Inventory locates files by glob and captures nothing."""
    with pytest.raises(InventoryError, match="must not declare"):
        _config(
            tmp_path,
            source={"path": f"{tmp_path}/data/{{feed}}/inbox", "file_pattern": "*"},
        )


def test_declaring_both_file_pattern_forms_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InventoryError, match="exactly one"):
        _config(
            tmp_path,
            source={
                "path": f"{tmp_path}/data/*/inbox",
                "file_pattern": "*.xls",
                "file_patterns": ["*.xlsx"],
            },
        )


def test_a_single_file_pattern_is_accepted(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        source={"path": f"{tmp_path}/data/*/inbox", "file_pattern": "*.xls"},
    )
    assert config.file_patterns == ("*.xls",)


def test_no_file_pattern_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InventoryError, match="file_pattern"):
        _config(tmp_path, source={"path": f"{tmp_path}/data/*/inbox"})


def test_exclusions_are_optional(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    del entry["exclusions"]
    assert inventory_source_config(entry, "feed_inbox").exclusions == ()


def test_recursive_defaults_to_false(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        source={"path": f"{tmp_path}/data/*/inbox", "file_pattern": "*"},
    )
    assert config.recursive is False


def test_inventory_declares_no_classification_contract(tmp_path: Path) -> None:
    """The config type carries filesystem facts only."""
    config = _config(tmp_path)
    for absent in ("variables", "path_regex", "manifest_source_identity"):
        assert not hasattr(config, absent), absent


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def test_multiple_file_patterns_are_enumerated(tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx", "AcmeTranMay26.xls")
    accepted = _accepted(tmp_path)
    assert sorted(item["filename"] for item in accepted) == [
        "AcmeHoldMay26.xlsx",
        "AcmeTranMay26.xls",
    ]


def test_a_pattern_that_matches_nothing_yields_no_candidates(tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.csv")
    assert _accepted(tmp_path) == []


def test_every_matching_directory_is_enumerated(tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    _inbox(tmp_path, "trustmark", "AcmeTranMay26.xlsx")
    assert len(_accepted(tmp_path)) == 2


def test_immediate_children_only_when_recursive_is_false(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    nested = inbox / "nested"
    nested.mkdir()
    (nested / "AcmeTranMay26.xlsx").write_text("x", encoding="utf-8")

    assert [item["filename"] for item in _accepted(tmp_path)] == ["AcmeHoldMay26.xlsx"]


def test_nested_files_are_enumerated_when_recursive_is_true(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    nested = inbox / "nested"
    nested.mkdir()
    (nested / "AcmeTranMay26.xlsx").write_text("x", encoding="utf-8")

    accepted = _accepted(
        tmp_path,
        source={
            "path": f"{tmp_path}/data/*/inbox",
            "file_patterns": ["*.xlsx", "*.xls"],
            "recursive": True,
        },
    )
    assert sorted(item["filename"] for item in accepted) == [
        "AcmeHoldMay26.xlsx",
        "AcmeTranMay26.xlsx",
    ]


def test_enumeration_is_deterministically_ordered(tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "b.xlsx", "a.xlsx", "c.xlsx")
    paths = [item["source_file"] for item in _accepted(tmp_path)]
    assert paths == sorted(paths)


def test_missing_source_directory_yields_nothing(tmp_path: Path) -> None:
    assert _accepted(tmp_path) == []
    assert _rejected(tmp_path) == []


# ---------------------------------------------------------------------------
# Candidate records
# ---------------------------------------------------------------------------


def test_accepted_record_carries_filesystem_facts_only(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    record = _accepted(tmp_path)[0]
    assert record == {
        "source_file": str(inbox / "AcmeHoldMay26.xlsx"),
        "filename": "AcmeHoldMay26.xlsx",
        "status": "accepted",
    }


def test_every_configured_exclusion_rejects_its_file(tmp_path: Path) -> None:
    excluded = [
        "~$AcmeHoldMay26.xlsx",
        "AcmeHoldMay26.xlsx.tmp",
        "AcmeHoldMay26.xlsx.part",
        "AcmeHoldMay26.xlsx.crdownload",
        "AcmeHoldMay26.xlsx.lock",
    ]
    _inbox(tmp_path, "bmo", *excluded)
    source = {
        "path": f"{tmp_path}/data/*/inbox",
        "file_patterns": ["*"],
        "recursive": False,
    }
    assert _accepted(tmp_path, source=source) == []

    rejected = _rejected(tmp_path, source=source)
    assert len(rejected) == len(excluded)
    assert {item["reason_code"] for item in rejected} == {"excluded"}
    assert all(item["reason"].startswith("Filename matches") for item in rejected)


def test_a_non_excluded_filename_is_never_rejected(tmp_path: Path) -> None:
    """Without classification there is no other reason to reject a candidate."""
    _inbox(tmp_path, "bmo", "AnythingAtAll.xlsx", "1234.xls")
    assert len(_accepted(tmp_path)) == 2
    assert _rejected(tmp_path) == []


def test_excluded_candidates_do_not_block_valid_files(tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx", "~$AcmeHoldJan26.xlsx")
    assert [item["filename"] for item in _accepted(tmp_path)] == ["AcmeHoldMay26.xlsx"]
    assert len(_rejected(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Inventory lifecycle
# ---------------------------------------------------------------------------


def test_one_record_per_newly_governed_file(run_log, tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx", "AcmeTranMay26.xlsx")
    ctx = _ctx(tmp_path)

    assert run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path))) == 0
    rows = _manifest_rows(ctx)
    assert len(rows) == 2
    # No record_type on the call: the routine writes the manifest row and its
    # baseline mutation, and what kind of record each is belongs to it.
    assert all(row["source_name"] for row in rows)


def test_manifest_record_carries_every_required_field(run_log, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    ctx = _ctx(tmp_path)
    run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path)))

    row = _manifest_rows(ctx)[0]
    # The parameters the insert routine takes. Identity, timestamps and what
    # kind of record each is are the routine's -- it mints the manifest row and
    # its baseline mutation and answers with what it created.
    assert sorted(row) == [
        "base_name",
        "checksum_sha256",
        "evidence",
        "file_extension",
        "file_name",
        "path",
        "producer",
        "size_bytes",
        "source_name",
    ]
    assert row["producer"]["application"] == "rey_loader"
    assert row["file_name"] == "AcmeHoldMay26.xlsx"
    assert row["file_extension"] == "xlsx"
    assert row["path"] == str(inbox / "AcmeHoldMay26.xlsx")


@pytest.mark.parametrize(
    ("filename", "base_name", "file_extension"),
    [
        ("MissGEDCPTranMay26.csv", "MissGEDCPTranMay26", "csv"),
        ("account.positions.May26.csv", "account.positions.May26", "csv"),
        ("AcmeHoldMay26.xlsx", "AcmeHoldMay26", "xlsx"),
    ],
)
def test_base_name_is_the_file_name_without_its_final_extension(run_log, 
    tmp_path: Path,
    filename: str,
    base_name: str,
    file_extension: str,
) -> None:
    """Only the final suffix is removed, so interior dots stay in the base name."""
    _inbox(tmp_path, "bmo", filename)
    ctx = _ctx(tmp_path)
    entry = _entry(tmp_path)
    entry["source"] = {**entry["source"], "file_patterns": ["*.xlsx", "*.xls", "*.csv"]}
    run_source_inventory(ctx, run_log, _process_config(entry))

    file_object = _manifest_rows(ctx)[0]
    assert file_object["file_name"] == filename
    assert file_object["base_name"] == base_name
    assert file_object["file_extension"] == file_extension


def test_checksum_and_size_describe_the_file(run_log, tmp_path: Path) -> None:
    import hashlib

    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    source = inbox / "AcmeHoldMay26.xlsx"
    ctx = _ctx(tmp_path)
    run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path)))

    row = _manifest_rows(ctx)[0]
    assert row["checksum_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert row["size_bytes"] == source.stat().st_size


def test_file_id_is_unique_per_governed_file(run_log, tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx", "AcmeTranMay26.xlsx")
    ctx = _ctx(tmp_path)
    run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path)))

    # Two files, recorded separately. The identity itself is the routine's --
    # control.file_manifest mints file_manifest_id -- so what is asserted here
    # is that inventory offered each file on its own, keyed by its checksum.
    assert len({row["checksum_sha256"] for row in _manifest_rows(ctx)}) == 2


def test_rerun_does_not_duplicate_unchanged_files(run_log, tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    ctx = _ctx(tmp_path)
    config = _process_config(_entry(tmp_path))

    assert run_source_inventory(ctx, run_log, config) == 0
    assert run_source_inventory(ctx, run_log, config) == 0
    # Offered both times -- inventory does not decide what is already governed;
    # the routine does, and it answered that the second offer created no new
    # manifest row.
    rows = _manifest_rows(ctx)
    assert len(rows) == 2
    assert rows[0]["checksum_sha256"] == rows[1]["checksum_sha256"]


def test_changed_content_at_a_known_path_is_a_new_occurrence(run_log, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    ctx = _ctx(tmp_path)
    config = _process_config(_entry(tmp_path))
    run_source_inventory(ctx, run_log, config)

    (inbox / "AcmeHoldMay26.xlsx").write_text("different content\n", encoding="utf-8")
    run_source_inventory(ctx, run_log, config)

    rows = _manifest_rows(ctx)
    assert len(rows) == 2
    assert rows[0]["path"] == rows[1]["path"]
    # Offered twice at one path, with different content. Whether that is one
    # governed file or two is the routine's answer; what inventory owes it is
    # the second observation.
    assert rows[0]["checksum_sha256"] != rows[1]["checksum_sha256"]


def test_identical_content_at_separate_paths_stays_separate(run_log, tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    trustmark = _inbox(tmp_path, "trustmark")
    (trustmark / "AcmeHoldMay26.xlsx").write_text(
        "content of AcmeHoldMay26.xlsx\n", encoding="utf-8"
    )
    ctx = _ctx(tmp_path)
    run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path)))

    rows = _manifest_rows(ctx)
    assert len(rows) == 2
    assert rows[0]["checksum_sha256"] == rows[1]["checksum_sha256"]
    assert rows[0]["path"] != rows[1]["path"]


def test_excluded_candidates_are_not_inventoried(run_log, tmp_path: Path) -> None:
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx", "~$AcmeHoldMay26.xlsx")
    ctx = _ctx(tmp_path)
    run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path)))

    assert [row["file_name"] for row in _manifest_rows(ctx)] == [
        "AcmeHoldMay26.xlsx"
    ]


def test_inventory_never_modifies_a_source_file(run_log, tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    source = inbox / "AcmeHoldMay26.xlsx"
    before = (source.read_text(encoding="utf-8"), source.stat().st_size)

    run_source_inventory(_ctx(tmp_path), run_log, _process_config(_entry(tmp_path)))

    assert (source.read_text(encoding="utf-8"), source.stat().st_size) == before
    assert sorted(item.name for item in inbox.iterdir()) == ["AcmeHoldMay26.xlsx"]


def test_missing_source_directory_yields_no_records(run_log, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path))) == 0
    assert _manifest_rows(ctx) == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_file_that_changes_during_inventory_is_failed(run_log, tmp_path: Path, monkeypatch) -> None:
    """Checksum and size must describe one stable file state."""
    inbox = _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    source = inbox / "AcmeHoldMay26.xlsx"
    ctx = _ctx(tmp_path)

    def _growing(path):
        source.write_text(source.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
        return "0" * 64

    monkeypatch.setattr(manifest_module, "sha256_file", _growing)

    assert run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path))) == 1
    assert _manifest_rows(ctx) == []


def test_a_context_without_a_control_is_a_structured_failure(run_log, tmp_path: Path) -> None:
    """The manifest is in the control database; a context reaching it holds one.

    This asked about an unconfigured manifest *path* -- the JSONL file that
    inventory used to append to. There is no such path: the routine writes the
    manifest row, and what a context must carry to reach it is the Control.
    """
    _inbox(tmp_path, "bmo", "AcmeHoldMay26.xlsx")
    ctx = _ctx(tmp_path)
    ctx.shared_control = None

    # Structured, not raised: one file that cannot be recorded is a failed
    # file, and the run reports it rather than stopping the sources after it.
    assert run_source_inventory(ctx, run_log, _process_config(_entry(tmp_path))) == 1
    failures = [
        record for record in _run_log_records(run_log)
        if record.get("record_type") == "VALIDATION_RESULT"
        and record.get("status") == "failed"
    ]
    assert any("control database" in str(record.get("message")) for record in failures)



# ---------------------------------------------------------------------------
# FileManifest.inventory: one candidate -> persistence -> outcome
# ---------------------------------------------------------------------------


def test_a_new_file_is_inventoried_with_both_created_flags(tmp_path: Path) -> None:
    source = _inbox(tmp_path, "bmo", "a.csv") / "a.csv"

    outcome = FileManifest(_InsertingControl()).inventory(source, source_name="feed_inbox")

    assert (outcome.status, outcome.file_manifest_id) == ("inventoried", 1)
    assert (outcome.manifest_created, outcome.inventory_created) == (True, True)
    assert outcome.reason is None


def test_a_known_file_with_a_new_observation_is_inventory_recorded(tmp_path: Path) -> None:
    source = _inbox(tmp_path, "bmo", "a.csv") / "a.csv"
    manifest = FileManifest(_InsertingControl())
    manifest.inventory(source, source_name="feed_inbox")

    assert manifest.inventory(source, source_name="feed_inbox").status == "inventory_recorded"


def test_neither_flag_is_already_inventoried(tmp_path: Path) -> None:
    source = _inbox(tmp_path, "bmo", "a.csv") / "a.csv"

    class _Known(_InsertingControl):
        def inventory_file_result(self, required=True, **values):
            return {"o_file_manifest_id": 9, "o_manifest_created": False,
                    "o_inventory_created": False, "o_batch_step_id": None}

    outcome = FileManifest(_Known()).inventory(source)

    assert (outcome.status, outcome.file_manifest_id) == ("already_inventoried", 9)


def test_a_missing_file_is_a_failed_outcome_not_an_exception(tmp_path: Path) -> None:
    outcome = FileManifest(_InsertingControl()).inventory(tmp_path / "gone.csv")

    assert outcome.status == "failed"
    assert outcome.reason


def test_a_routine_failure_is_a_failed_outcome(tmp_path: Path) -> None:
    source = _inbox(tmp_path, "bmo", "a.csv") / "a.csv"

    class _Refusing(_InsertingControl):
        def inventory_file_result(self, required=True, **values):
            raise DatabaseError("routine refused")

    outcome = FileManifest(_Refusing()).inventory(source)

    assert (outcome.status, outcome.reason) == ("failed", "routine refused")


def test_no_row_is_a_failed_outcome(tmp_path: Path) -> None:
    source = _inbox(tmp_path, "bmo", "a.csv") / "a.csv"

    class _Silent(_InsertingControl):
        def inventory_file_result(self, required=True, **values):
            return None

    outcome = FileManifest(_Silent()).inventory(source)

    assert outcome.status == "failed"
    assert "no result" in outcome.reason
