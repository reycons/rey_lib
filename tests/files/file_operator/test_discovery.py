"""Focused tests for parameterized file discovery and its processing manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files.discovery import (
    resolve_file_discoveries,
    run_file_discovery,
)
from rey_lib.files.error_utils import RedactorError

RUN_TIMESTAMP = "20260729_101500"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


def _variables(**overrides: object) -> list[SimpleNamespace]:
    """Return the authoritative ordered variable declarations."""
    declared: list[dict[str, object]] = [
        {"name": "feed_source", "source": "path", "required": True},
        {"name": "client", "source": "filename", "required": True},
        {
            "name": "file_type",
            "source": "filename",
            "values": ["Hold", "Tran"],
            "required": True,
        },
        {"name": "period", "source": "filename", "format": "mmmyy", "required": True},
        {"name": "remainder", "source": "filename", "required": False},
    ]
    for item in declared:
        if item["name"] in overrides:
            replacement = overrides[item["name"]]
            if replacement is None:
                continue
            item.update(replacement)  # type: ignore[arg-type]
    return [SimpleNamespace(**item) for item in declared]


def _entry(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    """Return one discovery entry using the authoritative configuration shape."""
    values: dict[str, object] = {
        "name": "structural_source_files",
        "enabled": True,
        "source": SimpleNamespace(
            path=f"{tmp_path}/work/{{feed_source}}/work/converted_csv",
            file_pattern="*.csv",
        ),
        "outbox": str(tmp_path / "discovery"),
        "variables": _variables(),
        "filename": SimpleNamespace(
            pattern="{client}{file_type}{period}.{remainder}.csv"
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ctx(*entries: object) -> SimpleNamespace:
    return SimpleNamespace(
        file_discoveries=list(entries),
        run_timestamp=RUN_TIMESTAMP,
        pipeline_name="discovery_pipeline",
        pipeline_run_id="run-1",
    )


def _source(tmp_path: Path, feed: str, *filenames: str) -> Path:
    """Create one source directory populated with the given filenames."""
    directory = tmp_path / "work" / feed / "work" / "converted_csv"
    directory.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        (directory / name).write_text(f"content of {name}\n", encoding="utf-8")
    return directory


def _manifest(tmp_path: Path, name: str = "structural_source_files") -> dict:
    """Return the single manifest written for one discovery rule."""
    matches = sorted((tmp_path / "discovery").glob(f"{name}.file_discovery_manifest.*.json"))
    assert len(matches) == 1, f"expected one manifest for '{name}', found {matches}"
    return json.loads(matches[0].read_text(encoding="utf-8"))


def _accepted(manifest: dict) -> list[dict]:
    return manifest["files"]


def _rejected(manifest: dict) -> list[dict]:
    return manifest["rejected"]


# ---------------------------------------------------------------------------
# Section resolution
# ---------------------------------------------------------------------------


def test_section_must_be_present() -> None:
    """A missing section is unusable configuration."""
    with pytest.raises(RedactorError, match="missing required 'file_discoveries'"):
        resolve_file_discoveries(SimpleNamespace())


def test_section_must_be_a_list() -> None:
    """The section shape is validated before any rule executes."""
    with pytest.raises(RedactorError, match="must be a list"):
        resolve_file_discoveries(SimpleNamespace(file_discoveries={"name": "x"}))


def test_section_must_declare_at_least_one_discovery() -> None:
    """An empty list has nothing to execute."""
    with pytest.raises(RedactorError, match="declares no discoveries"):
        resolve_file_discoveries(SimpleNamespace(file_discoveries=[]))


def test_every_entry_requires_a_name(tmp_path: Path) -> None:
    """An unnamed entry cannot be identified in evidence or in a manifest."""
    with pytest.raises(RedactorError, match="requires a non-empty 'name'"):
        resolve_file_discoveries(_ctx(_entry(tmp_path, name="")))


def test_duplicate_discovery_names_are_rejected(tmp_path: Path) -> None:
    """Two rules with one name cannot be told apart."""
    ctx = _ctx(_entry(tmp_path), _entry(tmp_path))
    with pytest.raises(RedactorError, match="duplicate discovery"):
        resolve_file_discoveries(ctx)


def test_resolution_preserves_declared_order(tmp_path: Path) -> None:
    """Entries are returned in configured order."""
    ctx = _ctx(_entry(tmp_path, name="first"), _entry(tmp_path, name="second"))

    assert [str(e.name) for e in resolve_file_discoveries(ctx)] == ["first", "second"]


# ---------------------------------------------------------------------------
# List execution, skipping, and isolation
# ---------------------------------------------------------------------------


def test_disabled_rule_is_skipped_and_others_still_execute(run_log, tmp_path: Path) -> None:
    """A disabled rule never aborts the rules around it."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    ctx = _ctx(
        _entry(tmp_path, name="skipped_rule", enabled=False),
        _entry(tmp_path, name="live_rule"),
    )

    assert run_file_discovery(ctx, run_log) == 0
    assert not list((tmp_path / "discovery").glob("skipped_rule.*"))
    assert len(_accepted(_manifest(tmp_path, "live_rule"))) == 1


def test_failing_rule_does_not_stop_the_remaining_rules(run_log, tmp_path: Path) -> None:
    """One unusable rule is isolated; the others still produce manifests."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory\n", encoding="utf-8")
    ctx = _ctx(
        _entry(tmp_path, name="broken_rule", outbox=str(blocker)),
        _entry(tmp_path, name="live_rule"),
    )

    assert run_file_discovery(ctx, run_log) == 1
    assert len(_accepted(_manifest(tmp_path, "live_rule"))) == 1


def test_all_rules_succeeding_returns_zero(run_log, tmp_path: Path) -> None:
    """Every rule producing a manifest is a successful run."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    _source(tmp_path, "acme", "OtherTranJun26.Detail.csv")
    ctx = _ctx(_entry(tmp_path, name="alpha"), _entry(tmp_path, name="beta"))

    assert run_file_discovery(ctx, run_log) == 0
    assert len(_accepted(_manifest(tmp_path, "alpha"))) == 2
    assert len(_accepted(_manifest(tmp_path, "beta"))) == 2


def test_malformed_rule_is_isolated_from_valid_rules(run_log, tmp_path: Path) -> None:
    """An invalid rule configuration fails only that rule."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    broken = _entry(tmp_path, name="broken_rule")
    broken.filename = SimpleNamespace(pattern="{client}{undeclared}.csv")
    ctx = _ctx(broken, _entry(tmp_path, name="live_rule"))

    assert run_file_discovery(ctx, run_log) == 1
    assert len(_accepted(_manifest(tmp_path, "live_rule"))) == 1


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("{client}{file_type}{period}.{undeclared}.csv", "undeclared placeholder"),
        ("{client}{file_type}{period}.{feed_source}.csv", "declares source"),
        ("{client}{file_type}{period}.{client}.csv", "repeats placeholder"),
        ("{client}{file_type}{period}.{}.csv", "empty placeholder"),
        ("{client}{file_type}{period.csv", "unbalanced"),
    ],
)
def test_filename_placeholder_validation(run_log, 
    tmp_path: Path, pattern: str, expected: str
) -> None:
    """Malformed filename patterns fail with an actionable configuration error."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    entry = _entry(tmp_path)
    entry.filename = SimpleNamespace(pattern=pattern)

    assert run_file_discovery(_ctx(entry), run_log) == 1
    assert not list((tmp_path / "discovery").glob("*.json"))


def test_unused_declared_variable_is_rejected(run_log, tmp_path: Path) -> None:
    """A declaration that no pattern uses is a configuration error."""
    entry = _entry(tmp_path)
    entry.variables = _variables() + [
        SimpleNamespace(name="orphan", source="filename", required=False)
    ]

    assert run_file_discovery(_ctx(entry), run_log) == 1


def test_duplicate_variable_declaration_is_rejected(run_log, tmp_path: Path) -> None:
    """A repeated variable name cannot be resolved to one capture."""
    entry = _entry(tmp_path)
    entry.variables = _variables() + [
        SimpleNamespace(name="client", source="filename", required=True)
    ]

    assert run_file_discovery(_ctx(entry), run_log) == 1


def test_invalid_variable_source_is_rejected(run_log, tmp_path: Path) -> None:
    """Only path and filename are valid variable sources."""
    entry = _entry(tmp_path)
    entry.variables = _variables(client={"source": "environment"})

    assert run_file_discovery(_ctx(entry), run_log) == 1


def test_unknown_format_is_rejected(run_log, tmp_path: Path) -> None:
    """The format set is closed; configuration cannot introduce one."""
    entry = _entry(tmp_path)
    entry.variables = _variables(period={"format": "yyyymmdd"})

    assert run_file_discovery(_ctx(entry), run_log) == 1


def test_adjacent_unanchored_variables_are_rejected(run_log, tmp_path: Path) -> None:
    """Ambiguity is refused when configuration is resolved, not per file."""
    entry = _entry(tmp_path)
    entry.variables = _variables(file_type={"values": None})
    entry.variables = [
        SimpleNamespace(
            name=v.name,
            source=v.source,
            required=v.required,
            **({"format": v.format} if getattr(v, "format", None) else {}),
        )
        if v.name == "file_type"
        else v
        for v in _variables()
    ]

    assert run_file_discovery(_ctx(entry), run_log) == 1


def test_source_path_placeholder_must_be_declared(run_log, tmp_path: Path) -> None:
    """A braced path segment with no declaration is a configuration error."""
    entry = _entry(tmp_path)
    entry.source = SimpleNamespace(
        path=f"{tmp_path}/work/{{unknown_segment}}/work/converted_csv",
        file_pattern="*.csv",
    )

    assert run_file_discovery(_ctx(entry), run_log) == 1


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def test_path_and_filename_variables_are_captured(run_log, tmp_path: Path) -> None:
    """Every declared variable is captured from its declared source."""
    _source(tmp_path, "bmo", "ClientHoldMay26.AccountSummary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    record = _accepted(_manifest(tmp_path))[0]
    assert record["variables"] == {
        "feed_source": "bmo",
        "client": "Client",
        "file_type": "Hold",
        "period": "May26",
        "remainder": "AccountSummary",
    }


def test_each_matching_directory_supplies_its_own_path_capture(run_log, tmp_path: Path) -> None:
    """The parameterized path captures a different value per directory."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    _source(tmp_path, "acme", "OtherTranJun26.Detail.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    feeds = {r["variables"]["feed_source"] for r in _accepted(_manifest(tmp_path))}
    assert feeds == {"bmo", "acme"}


def test_optional_variable_may_be_present_or_absent(run_log, tmp_path: Path) -> None:
    """The optional trailing variable absorbs its preceding separator."""
    _source(
        tmp_path,
        "bmo",
        "ClientHoldMay26.AccountSummary.csv",
        "ClientHoldMay26.csv",
    )

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    remainders = {
        r["filename"]: r["variables"]["remainder"] for r in _accepted(_manifest(tmp_path))
    }
    assert remainders == {
        "ClientHoldMay26.AccountSummary.csv": "AccountSummary",
        "ClientHoldMay26.csv": None,
    }


# ---------------------------------------------------------------------------
# Variable constraint enforcement
# ---------------------------------------------------------------------------


def test_configured_values_are_enforced(run_log, tmp_path: Path) -> None:
    """A file type outside the configured values is rejected, not accepted."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv", "ClientOtherMay26.Summary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    manifest = _manifest(tmp_path)
    assert [r["filename"] for r in _accepted(manifest)] == ["ClientHoldMay26.Summary.csv"]
    assert [r["filename"] for r in _rejected(manifest)] == ["ClientOtherMay26.Summary.csv"]


@pytest.mark.parametrize(
    "period", ["Jan26", "Feb26", "May26", "Dec26"]
)
def test_valid_mmmyy_periods_are_accepted(run_log, tmp_path: Path, period: str) -> None:
    """Canonical month abbreviations with two year digits are accepted."""
    _source(tmp_path, "bmo", f"ClientHold{period}.Summary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    assert _accepted(_manifest(tmp_path))[0]["variables"]["period"] == period


@pytest.mark.parametrize(
    "period", ["may26", "MAY26", "Mar2026", "Foo26", "May2"]
)
def test_invalid_mmmyy_periods_are_rejected(run_log, tmp_path: Path, period: str) -> None:
    """Non-canonical or free-form dates never satisfy the configured format."""
    _source(tmp_path, "bmo", f"ClientHold{period}.Summary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    manifest = _manifest(tmp_path)
    assert _accepted(manifest) == []
    assert len(_rejected(manifest)) == 1


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_accepted_record_fields(run_log, tmp_path: Path) -> None:
    """An accepted record carries the original path, filename, and variables."""
    directory = _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    record = _accepted(_manifest(tmp_path))[0]
    assert record["source_file"] == str(directory / "ClientHoldMay26.Summary.csv")
    assert record["filename"] == "ClientHoldMay26.Summary.csv"
    assert record["status"] == "accepted"


def test_unmatched_filename_is_rejected_with_a_stable_code(run_log, tmp_path: Path) -> None:
    """A file the pattern cannot match is recorded, not silently dropped."""
    _source(tmp_path, "bmo", "unmatched.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    rejected = _rejected(_manifest(tmp_path))[0]
    assert rejected["status"] == "rejected"
    assert rejected["reason_code"] == "filename_pattern_mismatch"
    assert rejected["reason"]


def test_rejected_record_retains_reliable_captures(run_log, tmp_path: Path) -> None:
    """A rejection keeps what was captured before it and nulls the rest."""
    _source(tmp_path, "bmo", "unmatched.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    rejected = _rejected(_manifest(tmp_path))[0]
    assert rejected["variables"]["feed_source"] == "bmo"
    assert rejected["variables"]["client"] is None
    assert list(rejected["variables"]) == [
        "feed_source", "client", "file_type", "period", "remainder"
    ]


def test_filename_value_violation_is_a_pattern_mismatch(run_log, tmp_path: Path) -> None:
    """Filename values are compiled into the matcher, so a violation cannot match.

    Configured values anchor the pattern — that is what makes an adjacent
    unanchored variable unambiguous — so a disallowed filename value is refused
    by the matcher itself rather than by the later value check.
    """
    _source(tmp_path, "bmo", "ClientOtherMay26.Summary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    assert _rejected(_manifest(tmp_path))[0]["reason_code"] == "filename_pattern_mismatch"


def test_path_value_violation_carries_its_reason_code(run_log, tmp_path: Path) -> None:
    """A path variable's configured values are enforced after capture."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    _source(tmp_path, "unlisted", "ClientHoldMay26.Summary.csv")
    entry = _entry(tmp_path)
    entry.variables = _variables(feed_source={"values": ["bmo"]})

    assert run_file_discovery(_ctx(entry), run_log) == 0
    manifest = _manifest(tmp_path)
    assert [r["variables"]["feed_source"] for r in _accepted(manifest)] == ["bmo"]
    rejected = _rejected(manifest)[0]
    assert rejected["reason_code"] == "value_not_allowed"
    # The offending variable itself is not projected — only variables validated
    # before it are. Its captured value is carried in the reason instead.
    assert rejected["variables"]["feed_source"] is None
    assert "unlisted" in rejected["reason"]


def test_rejected_candidates_do_not_block_valid_files(run_log, tmp_path: Path) -> None:
    """Valid independent files are still recorded alongside rejections."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv", "unmatched.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    manifest = _manifest(tmp_path)
    assert len(_accepted(manifest)) == 1
    assert len(_rejected(manifest)) == 1


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_run_metadata(run_log, tmp_path: Path) -> None:
    """Run metadata describes the rule that produced the manifest."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    manifest = _manifest(tmp_path)
    assert manifest["discovery_name"] == "structural_source_files"
    assert manifest["generated_at"] == RUN_TIMESTAMP
    assert manifest["file_pattern"] == "*.csv"
    assert manifest["filename_pattern"] == "{client}{file_type}{period}.{remainder}.csv"
    assert manifest["declared_variables"] == [
        "feed_source", "client", "file_type", "period", "remainder"
    ]


def test_manifest_filename_leads_with_the_rule_name(run_log, tmp_path: Path) -> None:
    """A shared outbox stays self-describing and cannot collide."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    ctx = _ctx(_entry(tmp_path, name="alpha"), _entry(tmp_path, name="beta"))

    assert run_file_discovery(ctx, run_log) == 0
    names = sorted(p.name for p in (tmp_path / "discovery").glob("*.json"))
    assert names == [
        f"alpha.file_discovery_manifest.{RUN_TIMESTAMP}.json",
        f"beta.file_discovery_manifest.{RUN_TIMESTAMP}.json",
    ]


def test_manifest_never_contains_file_contents(run_log, tmp_path: Path) -> None:
    """The manifest is metadata only."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    raw = json.dumps(_manifest(tmp_path))
    assert "content of" not in raw


def test_ordering_is_deterministic_and_repeatable(run_log, tmp_path: Path) -> None:
    """The same filesystem state yields the same manifest content."""
    _source(tmp_path, "bmo", "BHoldMay26.Two.csv", "AHoldMay26.One.csv")
    _source(tmp_path, "acme", "CTranJun26.Three.csv")

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    first = _manifest(tmp_path)
    ordered = [r["source_file"] for r in _accepted(first)]
    assert ordered == sorted(ordered)

    for path in (tmp_path / "discovery").glob("*.json"):
        path.unlink()
    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0
    assert _manifest(tmp_path) == first


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


def test_outbox_is_created_with_parents(run_log, tmp_path: Path) -> None:
    """A missing outbox is created rather than failing the rule."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    entry = _entry(tmp_path, outbox=str(tmp_path / "nested" / "deep" / "discovery"))

    assert run_file_discovery(_ctx(entry), run_log) == 0
    assert (tmp_path / "nested" / "deep" / "discovery").is_dir()


def test_outbox_that_is_a_file_fails_the_rule(run_log, tmp_path: Path) -> None:
    """A configured outbox that is not a directory is unusable."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    blocker = tmp_path / "blocked"
    blocker.write_text("x\n", encoding="utf-8")

    assert run_file_discovery(_ctx(_entry(tmp_path, outbox=str(blocker))), run_log) == 1


def test_missing_outbox_configuration_fails_the_rule(run_log, tmp_path: Path) -> None:
    """outbox is mandatory; there is no fallback output location."""
    _source(tmp_path, "bmo", "ClientHoldMay26.Summary.csv")
    entry = _entry(tmp_path)
    del entry.outbox

    assert run_file_discovery(_ctx(entry), run_log) == 1


# ---------------------------------------------------------------------------
# Source immutability
# ---------------------------------------------------------------------------


def test_discovery_leaves_every_source_file_unchanged(run_log, tmp_path: Path) -> None:
    """Discovery reads no content and moves nothing."""
    directory = _source(
        tmp_path, "bmo", "ClientHoldMay26.Summary.csv", "unmatched.csv"
    )
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }

    assert run_file_discovery(_ctx(_entry(tmp_path)), run_log) == 0

    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }
    assert after == before


def test_no_source_specific_values_exist_in_application_logic() -> None:
    """Configured values are configuration, never business logic in code."""
    source = Path(__file__).resolve().parents[3] / "rey_lib" / "files" / "discovery.py"
    text = source.read_text(encoding="utf-8")
    for literal in ("Hold", "Tran", "client", "period", "remainder", "feed_source"):
        assert literal not in text, f"{literal} must not appear in discovery logic"
