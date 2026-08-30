"""Every migrated digest is the digest that was there before.

Ten sites moved from ``hashlib.sha256`` to the rey_lib.encryption primitives.
All ten produce values that are persisted -- a source_text_sha256 in inspection
evidence, a contract content_hash, an LLM input_hash -- so the only acceptable
outcome is that nothing changed.

Each test states the expected digest independently rather than calling the
function twice, so a test cannot pass by comparing a mistake to itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rey_lib.files.csv import read_csv_text
from rey_lib.encryption import sha256_bytes, sha256_file
from rey_lib.analysis import contract as llm_contract
from rey_lib.analysis import datasource as llm_datasource
from rey_lib.analysis import document_loader, preparation

# Text chosen to exercise what a careless migration would break: non-ASCII,
# CRLF, a NUL, and a trailing newline.
SAMPLES = (
    "",
    "abc",
    "Ünïcode café",
    "字段,値\n1,2\n",
    "a\r\nb\r\n",
    "with\x00nul",
    "trailing\n",
)


def _utf8(text: str) -> str:
    """The digest every migrated text site must still produce."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# files/file_utils.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", SAMPLES)
def test_the_file_digest_is_unchanged(tmp_path: Path, text: str) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(text.encode("utf-8"))

    assert sha256_file(target) == _utf8(text)


def test_the_file_digest_still_streams_a_file_larger_than_one_chunk(
    tmp_path: Path,
) -> None:
    """The chunked read was the part with room to go wrong."""
    payload = bytes(range(256)) * 8192  # 2 MiB, over the 1 MiB chunk
    target = tmp_path / "big.bin"
    target.write_bytes(payload)

    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("text", SAMPLES)
def test_the_bytes_digest_is_unchanged(text: str) -> None:
    assert sha256_bytes(text.encode("utf-8")) == _utf8(text)


def test_the_relevant_file_id_keeps_its_length_and_value() -> None:
    """A truncated digest of the path, not of any content."""
    from rey_lib.files.file_utils import _relevant_file_id

    path = Path("/data/Ünïcode/feed.csv")
    expected = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]

    identifier = _relevant_file_id(path)
    assert identifier == expected
    assert len(identifier) == 16


# ---------------------------------------------------------------------------
# files/csv.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["a,b\n1,2\n", "col,other\nÜnï,café\n", "x,y\r\n1,2\r\n", "a,b\n1,2"],
)
def test_the_csv_source_text_digest_is_of_the_text_as_read(text: str) -> None:
    """Hashed before any parsing, so it identifies the source exactly."""
    assert read_csv_text(text).source_text_sha256 == _utf8(text)


# ---------------------------------------------------------------------------
# analysis/
# ---------------------------------------------------------------------------
# The runner's own digest test went with the runner: rey_lib.ai owns execution
# now, and hashes nothing.

@pytest.mark.parametrize("text", SAMPLES)
def test_the_document_loader_digest_is_unchanged(text: str) -> None:
    assert document_loader._hash(text) == _utf8(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_the_preparation_digest_is_unchanged(text: str) -> None:
    assert preparation._sha256(text) == _utf8(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_the_datasource_content_hash_is_unchanged(text: str) -> None:
    source = llm_datasource.TextDataSource(text, ref="ref")

    assert source.extract().source_hash == _utf8(text)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"a": 1}],
        [{"b": 2, "a": 1}, {"a": "café"}],
        [{"path": Path("/x/y")}],
    ],
)
def test_the_datasource_row_hash_keeps_its_rendering_and_digest(
    rows: list[dict],
) -> None:
    """A legacy persisted identity: default separators, sorted keys, str coercion.

    The rendering is deliberately not migrated to canonical JSON -- only the
    digest operation moved -- so this pins both halves.
    """
    rendered = json.dumps(rows, default=str, sort_keys=True)

    assert llm_datasource._hash_rows(rows) == _utf8(rendered)


def test_the_contract_content_hash_is_of_the_file_text(tmp_path: Path) -> None:
    contract_path = tmp_path / "c.yaml"
    raw = (
        "name: analysis\n"
        "version: 1\n"
        "effective_date: 2026-01-01\n"
        "prompt: Ünïcode café\n"
    )
    contract_path.write_text(raw, encoding="utf-8")

    loaded = llm_contract.load(contract_path)

    assert loaded.hash == _utf8(raw)


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------

def test_no_migrated_module_computes_a_digest_itself() -> None:
    """The point of the increment: one owner, and these are not it."""
    import rey_lib.files.csv as csv_module
    import rey_lib.files.file_utils as file_utils_module
    import rey_lib.analysis.contract as contract_module
    import rey_lib.analysis.datasource as datasource_module
    import rey_lib.analysis.document_loader as loader_module
    import rey_lib.analysis.preparation as preparation_module

    for module in (
        csv_module,
        file_utils_module,
        contract_module,
        datasource_module,
        loader_module,
        preparation_module,
    ):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "hashlib" not in source, module.__name__
