"""SHA-256 has one owner, and these are the digests it must keep producing.

Every value here is already persisted somewhere -- in a manifest, a rule set, an
artifact record. The purpose of centralizing the digest is that no caller writes
``hashlib.sha256`` again; the purpose of these tests is that centralizing it
changed nothing about what a digest is.

The published NIST vectors are used deliberately in place of values captured
from our own code: a test that compares an implementation against itself proves
only that it is consistent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rey_lib.encryption import sha256_bytes, sha256_file, sha256_text

# From FIPS 180-2. Independent of anything in this codebase.
NIST_VECTORS = (
    (b"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    (b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    (
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
    ),
)


@pytest.mark.parametrize(("data", "digest"), NIST_VECTORS)
def test_bytes_match_the_published_vectors(data: bytes, digest: str) -> None:
    assert sha256_bytes(data) == digest


@pytest.mark.parametrize(("data", "digest"), NIST_VECTORS)
def test_text_matches_the_published_vectors(data: bytes, digest: str) -> None:
    assert sha256_text(data.decode("ascii")) == digest


@pytest.mark.parametrize(("data", "digest"), NIST_VECTORS)
def test_a_file_matches_the_published_vectors(
    tmp_path: Path, data: bytes, digest: str
) -> None:
    target = tmp_path / "vector.bin"
    target.write_bytes(data)

    assert sha256_file(target) == digest


# ---------------------------------------------------------------------------
# The three agree with each other and with the standard library
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["", "abc", "Ünïcode", "字段", "line\nline\r\n", " leading and trailing ", "\x00"],
)
def test_all_three_routes_reach_the_same_digest(tmp_path: Path, text: str) -> None:
    """Text, its bytes, and a file holding them are one identity."""
    encoded = text.encode("utf-8")
    target = tmp_path / "same.txt"
    target.write_bytes(encoded)

    expected = hashlib.sha256(encoded).hexdigest()

    assert sha256_text(text) == expected
    assert sha256_bytes(encoded) == expected
    assert sha256_file(target) == expected


# ---------------------------------------------------------------------------
# The encoding is part of the identity
# ---------------------------------------------------------------------------

def test_the_named_encoding_changes_the_digest() -> None:
    """Not an implementation detail: the same string is two different digests."""
    text = "café"

    assert sha256_text(text, encoding="utf-8") != sha256_text(text, encoding="utf-16")
    assert sha256_text(text, encoding="utf-8") == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def test_utf_8_is_the_default_every_current_caller_relies_on() -> None:
    assert sha256_text("café") == sha256_text("café", encoding="utf-8")


def test_text_that_cannot_be_encoded_fails_rather_than_substituting() -> None:
    """A silent replacement character would be a wrong digest, not an error."""
    with pytest.raises(UnicodeEncodeError):
        sha256_text("café", encoding="ascii")


# ---------------------------------------------------------------------------
# No normalization: the caller's representation is hashed as given
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("left", "right"),
    [
        ('{"a":1,"b":2}', '{"b":2,"a":1}'),   # key order
        ('{"a": 1}', '{"a":1}'),              # spacing
        ("line\n", "line\r\n"),               # line endings
        ("text", "text "),                    # trailing space
        ("café", "café"),               # unicode composition
    ],
)
def test_representations_that_differ_produce_different_digests(
    left: str, right: str
) -> None:
    """The digest function must not tidy up its input.

    Deciding that these pairs are equivalent is a format decision -- canonical
    rendering, newline policy, unicode normalization -- and belongs to whatever
    produced the text. If this function normalized anything, it would silently
    change digests that are already stored.
    """
    assert sha256_text(left) != sha256_text(right)


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def test_chunking_is_invisible_in_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file larger than one chunk hashes as the whole byte sequence."""
    import rey_lib.encryption as encryption

    payload = bytes(range(256)) * 40
    target = tmp_path / "big.bin"
    target.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    assert sha256_file(target) == expected
    # Same bytes, a chunk size that forces many reads: same digest.
    monkeypatch.setattr(encryption, "_FILE_CHUNK_BYTES", 7)
    assert sha256_file(target) == expected


def test_an_empty_file_is_the_empty_digest(tmp_path: Path) -> None:
    target = tmp_path / "empty.bin"
    target.write_bytes(b"")

    assert sha256_file(target) == NIST_VECTORS[0][1]


def test_bytes_are_hashed_as_stored_without_newline_translation(
    tmp_path: Path,
) -> None:
    """Opened binary, so a CRLF file does not depend on the platform."""
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"a\r\nb\r\n")

    assert sha256_file(target) == hashlib.sha256(b"a\r\nb\r\n").hexdigest()


def test_a_missing_file_raises_rather_than_returning_a_digest(
    tmp_path: Path,
) -> None:
    """An empty-string or empty-file digest here would be a false identity."""
    with pytest.raises(OSError):
        sha256_file(tmp_path / "absent.bin")
