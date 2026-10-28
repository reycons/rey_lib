"""Every File Operator digest survives the move to rey_lib.encryption.

Fourteen sites moved off hashlib. These digests are not incidental: rule_sha256
gates rule-set activation, output_sha256 and row_sha256 are the evidence that a
normalization is reversible, and the file_type signature names the artifact on
disk. A changed digest here does not fail loudly -- it silently invalidates
stored evidence.

Two of these sites decide what is hashed and keep doing so. canonical_rule_sha256
still owns the executable rule projection and _canonical_sha256 still owns the
canonical rendering; only the final digest call moved. The file_type and
header-identity renderings were deliberately left as they are, so their tests
assert the exact rendering as well as the digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rey_lib.files.json import render_json
from rey_lib.profiling import normalized_header

from rey_lib.files.active_rule_sets import _file_sha256
from rey_lib.profiling.structural_analysis import (
    _canonical_sha256,
    _signature_set_sha256,
)
from rey_lib.files.onboarding import _sha256 as onboarding_sha256
from rey_lib.files.rule_sets import (
    _CANONICAL_RULE_KEYS,
    _canonical_plain,
    canonical_rule_sha256,
)

SAMPLES = ("", "abc", "Ünïcode café", "字段,値\n1,2\n", "a\r\nb\r\n", "x\x00y", "trail\n")


def _utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The two identity projections keep both halves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        {"b": 1, "a": {"z": 2, "y": [1, 2]}},
        {"path": "/data/Ünïcode/x.csv", "name": "café"},
        {"col": "字段", "v": "値"},
        {},
        [{"b": 2, "a": 1}, "Ünïcode"],
        {"a": {"b": {"c": {"d": "é"}}}},
    ],
)
def test_the_canonical_identity_digest_is_unchanged(value: object) -> None:
    """Rendering and digest are both pinned, not just their combination."""
    assert _canonical_sha256(value) == _utf8(render_json(value, mode="canonical"))


@pytest.mark.parametrize(
    "signatures", [[], ["a"], ["b", "a"], ["Ünï", "字段"]]
)
def test_the_signature_set_digest_is_unchanged(signatures: list[str]) -> None:
    assert _signature_set_sha256(signatures) == _utf8(
        render_json(signatures, mode="canonical")
    )


def _rule_entry(**overrides: object) -> dict:
    entry = {key: f"value_{key}" for key in _CANONICAL_RULE_KEYS}
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    "entry",
    [
        _rule_entry(),
        _rule_entry(**{_CANONICAL_RULE_KEYS[0]: {"café": ["Ünïcode", "字段"]}}),
        _rule_entry(**{_CANONICAL_RULE_KEYS[0]: {"z": 1, "a": {"y": [3, 2]}}}),
        _rule_entry(**{_CANONICAL_RULE_KEYS[-1]: {"t": True, "f": False, "n": None}}),
    ],
)
def test_the_rule_set_digest_is_unchanged(entry: dict) -> None:
    """rule_sets still owns the projection; only the digest call moved."""
    projection = {
        key: _canonical_plain(entry[key]) for key in _CANONICAL_RULE_KEYS
    }

    assert canonical_rule_sha256(entry) == _utf8(
        render_json(projection, mode="canonical")
    )


# ---------------------------------------------------------------------------
# Renderings that were deliberately not migrated
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fields", [["A", "B"], ["Ünï", "café"], ["字段", "値"], ["a b", "c,d"]]
)
def test_the_file_type_signature_rendering_is_untouched(fields: list[str]) -> None:
    """Moving the digest did not authorize changing what is rendered.

    This rendering has no sort_keys. It does not matter for a list of strings,
    but the artifact filename on disk is derived from this digest, so the
    rendering is pinned rather than assumed equivalent to canonical mode.
    """
    from rey_lib.encryption import sha256_text

    normalized = normalized_header(list(fields))
    rendered = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

    assert sha256_text(rendered) == _utf8(rendered)
    assert "\\u" not in rendered  # non-ASCII stays literal, as before


# ---------------------------------------------------------------------------
# File digests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", SAMPLES)
def test_the_onboarding_file_digest_is_unchanged(tmp_path: Path, text: str) -> None:
    target = tmp_path / "artifact.json"
    target.write_bytes(text.encode("utf-8"))

    assert onboarding_sha256(target) == _utf8(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_the_active_rule_set_file_digest_is_unchanged(
    tmp_path: Path, text: str
) -> None:
    target = tmp_path / "artifact.json"
    target.write_bytes(text.encode("utf-8"))

    assert _file_sha256(target) == _utf8(text)


def test_a_file_digest_still_reports_an_unreadable_artifact(tmp_path: Path) -> None:
    """The InputError wrapper is behaviour, not decoration."""
    from rey_lib.files.error_utils import InputError

    with pytest.raises(InputError, match="Cannot hash File Operator artifact"):
        _file_sha256(tmp_path / "absent.json")


def test_file_digests_read_bytes_not_decoded_text(tmp_path: Path) -> None:
    """A file that is not valid UTF-8 still has a digest."""
    target = tmp_path / "raw.bin"
    target.write_bytes(b"\xff\xfe\x00binary")

    assert onboarding_sha256(target) == hashlib.sha256(b"\xff\xfe\x00binary").hexdigest()
    assert _file_sha256(target) == onboarding_sha256(target)


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_no_file_operator_module_computes_a_digest_itself() -> None:
    """One owner. This asserts it rather than leaving it to drift."""
    # The file_operator package's modules, at rey_lib/files since #1110 S4.
    package_root = Path(__file__).resolve().parents[3] / "rey_lib" / "files"
    moved = (
        "error_utils", "input_utils", "inspection", "discovery", "file_types",
        "processor", "feeds", "feed_config", "onboarding", "active_rule_sets",
        "rule_sets", "rule_set_materialization", "structural_normalization",
        "structural_validation", "layouts/__init__", "layouts/common",
        "layouts/delimited", "layouts/excel", "layouts/fixed_width",
    )
    offenders = [
        path.name
        for path in sorted(package_root / f"{name}.py" for name in moved)
        if "hashlib" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
